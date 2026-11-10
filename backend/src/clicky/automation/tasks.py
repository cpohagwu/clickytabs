"""Browser tasks: the brain proposes, the user consents, the automation worker acts.

    brain reply  <task start_url="..." session="yes">goal</task>          (validated: http(s) URL, goal length)
    TaskManager.start()  consent.request -> the extension's consent window (sites worked out from the real tab URL)
    consent.response     deny -> task.done(false); allow -> the sites the user approved
    session.bundle       (when the task wants the login) cookies of the tab's site + its localStorage
    worker               one process per task (Windows Job Object, ephemeral browser profile), WorkerTaskStart with
                         storage_state, allowed_domains for the approved sites only, the user's UA / language / viewport
    relay                task.started, task.step (+ thumbnail), task.confirm.request <-> task.confirm.response,
                         task.cancel -> the worker's stop flag, task.done (summary, final URL)

One task per session at a time; a new one is refused while another runs. The start URL must be on an approved site,
whatever the model wrote. API keys reach the worker only through its environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from clicky.automation.session_bridge import allowed_domain_patterns, in_site, storage_state_from_bundle
from clicky.automation.worker_client import WORKER_SRC, WorkerClient, worker_installed
from clicky.brain.directives import TaskRequest
from clicky.capabilities.kinds import Capability
from clicky.capabilities.registry import Capabilities
from clicky.config import Settings
from clicky.protocol.events import (
    ConsentRequest,
    ConsentResponse,
    Event,
    SessionBundle,
    TaskCancel,
    TaskConfirmRequest,
    TaskConfirmResponse,
    TaskDone,
    TaskStarted,
    TaskStep,
)
from clicky.protocol.worker_ipc import (
    AgentLLMConfig,
    BrowserConfig,
    TaskLimits,
    WorkerConfirmRequest,
    WorkerConfirmResponse,
    WorkerLog,
    WorkerReady,
    WorkerTaskCancel,
    WorkerTaskDone,
    WorkerTaskError,
    WorkerTaskStart,
    WorkerTaskStep,
)

log = logging.getLogger(__name__)

CONSENT_TIMEOUT_S = 180.0
BUNDLE_TIMEOUT_S = 30.0
WORKER_READY_TIMEOUT_S = 60.0
TASK_TIMEOUT_S = 30 * 60.0


class TaskFailure(Exception):
    """Ends the task with this message for the user."""


@dataclass
class _Job:
    task_id: str
    turn_id: str
    request: TaskRequest
    page_url: str | None
    consent: asyncio.Future[ConsentResponse]
    bundle: asyncio.Future[SessionBundle]
    worker: WorkerClient | None = None
    pending_confirm: str | None = None
    cancelled: bool = False


class TaskManager:
    def __init__(self, capabilities: Capabilities, settings: Settings, emit: Callable[[Event], None]) -> None:
        self._capabilities = capabilities
        self._settings = settings
        self._emit = emit
        self._job: _Job | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing: set[asyncio.Task[None]] = set()        # finished tasks still shutting their worker down
        self.finished: list[tuple[str, bool, str]] = []        # (task_id, success, summary): tests, logs

    @property
    def busy(self) -> bool:
        """A task is underway (its result not reported yet; a reported one may still be closing its worker)."""
        job, task = self._job, self._task
        return (task is not None and not task.done() and job is not None
                and not any(entry[0] == job.task_id for entry in self.finished))

    def start(self, turn_id: str, request: TaskRequest, page_url: str | None) -> str:
        task_id = uuid.uuid4().hex
        if self.busy:
            self._emit(TaskDone(task_id=task_id, success=False,
                                summary="Another browser task is still running. Stop it first."))
            return task_id
        loop = asyncio.get_running_loop()
        job = _Job(task_id=task_id, turn_id=turn_id, request=request, page_url=page_url,
                   consent=loop.create_future(), bundle=loop.create_future())
        if self._task is not None and not self._task.done():            # the previous one is closing its worker
            self._closing.add(self._task)
            self._task.add_done_callback(self._closing.discard)
        self._job = job
        # Sent now, before this turn's turn.done: the extension maps the turn to its tab only while the turn is open.
        self._emit(ConsentRequest(turn_id=turn_id, task_id=task_id, goal=request.goal, start_url=request.start_url,
                                  session=request.session))
        self._task = asyncio.create_task(self._run(job), name=f"task-{task_id}")
        return task_id

    # ------------------------------------------------------------------------------------------- client events

    def on_consent(self, event: ConsentResponse) -> None:
        job = self._job
        if job is not None and event.task_id == job.task_id and not job.consent.done():
            job.consent.set_result(event)

    def on_bundle(self, event: SessionBundle) -> None:
        job = self._job
        if job is not None and event.task_id == job.task_id and not job.bundle.done():
            job.bundle.set_result(event)

    async def on_confirm(self, event: TaskConfirmResponse) -> None:
        job = self._job
        if job is None or event.task_id != job.task_id or job.worker is None or event.request_id != job.pending_confirm:
            return
        job.pending_confirm = None
        with contextlib.suppress(RuntimeError):
            await job.worker.send(WorkerConfirmResponse(task_id=job.task_id, request_id=event.request_id,
                                                        approved=event.approved, note=event.note))

    async def on_cancel(self, event: TaskCancel) -> None:
        job = self._job
        if job is None or event.task_id != job.task_id:
            return
        job.cancelled = True
        if job.worker is not None:
            with contextlib.suppress(RuntimeError):
                await job.worker.send(WorkerTaskCancel(task_id=job.task_id))     # the agent stops at its next step
        elif self._task is not None:                  # still waiting for consent (the task may not even have run yet)
            self._done(job, False, "Stopped.")
            self._task.cancel()

    async def cancel(self, reason: str | None = None) -> None:
        """Stop everything now (session closed, new conversation): kill the worker and its browser."""
        job, task = self._job, self._task
        for closing in list(self._closing):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await closing
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        if reason is not None and job is not None:
            self._done(job, False, reason)

    # ------------------------------------------------------------------------------------------------ job

    def _done(self, job: _Job, success: bool, summary: str, final_url: str | None = None) -> None:
        if any(entry[0] == job.task_id for entry in self.finished):
            return
        self.finished.append((job.task_id, success, summary))
        # The summary describes the user's account on the site (orders, names): DEBUG only.
        log.info("task %s %s (%d-character summary)", job.task_id, "done" if success else "failed", len(summary))
        log.debug("task %s summary: %s", job.task_id, summary[:200])
        self._emit(TaskDone(task_id=job.task_id, success=success, summary=summary[:2000], final_url=final_url))

    async def _run(self, job: _Job) -> None:
        try:
            await asyncio.wait_for(self._consent_and_run(job), TASK_TIMEOUT_S)
        except asyncio.CancelledError:
            if job.cancelled:
                self._done(job, False, "Stopped.")
            raise
        except TaskFailure as exc:
            self._done(job, False, str(exc))
        except TimeoutError:
            self._done(job, False, "The task took too long and was stopped.")
        except Exception as exc:
            log.exception("task %s crashed", job.task_id)
            self._done(job, False, f"The task failed: {exc}")
        finally:
            if job.worker is not None:
                await job.worker.close()
                job.worker = None

    async def _consent_and_run(self, job: _Job) -> None:
        request = job.request
        try:
            consent = await asyncio.wait_for(job.consent, CONSENT_TIMEOUT_S)
        except TimeoutError as exc:
            raise TaskFailure("No answer to the consent request, so nothing was done.") from exc
        if consent.decision == "deny":
            raise TaskFailure("You declined the task, so nothing was done.")
        sites = sorted(set(consent.sites))
        start_url = request.start_url or consent.tab_url or job.page_url
        parts = urlsplit(start_url or "")
        if parts.scheme not in ("http", "https") or not any(in_site(parts.hostname, site) for site in sites):
            raise TaskFailure("The task's address is not on a site you allowed, so nothing was done.")

        bundle: SessionBundle | None = None
        if request.session:
            try:
                bundle = await asyncio.wait_for(job.bundle, BUNDLE_TIMEOUT_S)
            except TimeoutError as exc:
                raise TaskFailure("Your login for the site never arrived, so nothing was done.") from exc
        storage_state = storage_state_from_bundle(bundle, allowed_sites=set(sites)) if bundle else None

        agent = self._capabilities.get(Capability.AGENT_LLM).active
        config = agent.provider.worker_config()     # type: ignore[union-attr]
        stub = config.provider == "fake"
        if not stub and not worker_installed():
            raise TaskFailure("Browser tasks need the automation worker: run `uv sync` in backend/automation-worker.")
        worker = await WorkerClient.spawn(
            stub=stub, env={**config.env, "BROWSER_USE_CONFIG_DIR": str(self._settings.data_dir / "browser-use")},
            pythonpath=(WORKER_SRC,) if stub else (), log_path=self._settings.data_dir / "logs" / "worker.log")
        job.worker = worker
        ready = await worker.next_event(WORKER_READY_TIMEOUT_S)
        if not isinstance(ready, WorkerReady):
            raise TaskFailure("The browser worker did not start.")

        cookies = len((storage_state or {}).get("cookies") or [])
        self._emit(TaskStarted(task_id=job.task_id, goal=request.goal, sites=sites, cookies=cookies))
        browser = BrowserConfig(
            headless=self._settings.automation_headless,
            user_agent=bundle.user_agent if bundle else None, locale=bundle.locale if bundle else None,
            timezone=bundle.timezone if bundle else None, viewport=bundle.viewport if bundle else None,
            allowed_domains=allowed_domain_patterns(sites, [start_url, consent.tab_url, job.page_url]),
            executable_path=self._settings.automation_browser, args=list(self._settings.automation_browser_args))
        await worker.send(WorkerTaskStart(
            task_id=job.task_id, goal=request.goal, start_url=start_url, storage_state=storage_state,
            llm=AgentLLMConfig(provider=config.provider, model=config.model, base_url=config.base_url,  # type: ignore[arg-type]
                               options=dict(config.options)),
            browser=browser, limits=TaskLimits(max_steps=self._settings.automation_max_steps)))
        log.info("task %s started on %s (%d cookies, %s)", job.task_id, ", ".join(sites), cookies, config.provider)
        await self._relay(job, worker)

    async def _relay(self, job: _Job, worker: WorkerClient) -> None:
        async for event in worker.events():
            if isinstance(event, WorkerTaskStep) and event.task_id == job.task_id:
                self._emit(TaskStep(task_id=job.task_id, n=event.n, action_text=event.action_text, url=event.url,
                                    thumb_b64=event.thumb_b64))
            elif isinstance(event, WorkerConfirmRequest) and event.task_id == job.task_id:
                job.pending_confirm = event.request_id
                self._emit(TaskConfirmRequest(task_id=job.task_id, request_id=event.request_id,
                                              question=event.question))
            elif isinstance(event, WorkerTaskDone) and event.task_id == job.task_id:
                self._done(job, event.success, event.summary, event.final_url)
                return
            elif isinstance(event, WorkerTaskError):
                raise TaskFailure(f"The browser task failed: {event.message}")
            elif isinstance(event, WorkerLog):
                log.log(logging.WARNING if event.level in ("warning", "error") else logging.DEBUG,
                        "worker: %s", event.message)
        raise TaskFailure("The browser worker stopped unexpectedly.")
