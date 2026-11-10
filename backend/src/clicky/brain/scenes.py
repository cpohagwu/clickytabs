"""Scenes: things the brain builds in front of the page - 3D models, 2D animations, diagrams, cards.

    brain reply  <scene kind="three" title="Solar system">a brief: what to show, labels, motion</scene>
    SceneManager.start()   scene.building -> the stage opens with a placeholder while the voice keeps talking
    codegen capability     writes the code for that kind (prompts/codegen_{full,compact}.md)
    scene.render           the extension runs it in the sandbox (three/canvas in a Worker, svg/html sanitized)
    scene.result           ok -> done; an error -> ONE repair turn (the code + the error back to codegen) -> render
                           again; a second failure (or no stage) -> scene.failed

The brain never writes scene code itself: its prompt stays short and cacheable, its effort stays low for the voice,
and the speech isn't held up by a long code block. One scene job runs per session; a new request replaces it. The
code of recent scenes is kept, so "make it spin faster" (the page context names the open scene) edits the scene on
screen instead of starting over.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from importlib import resources

from clicky.brain.directives import SceneRequest
from clicky.capabilities.base import ChatMessage, ChatRequest, ModelInfo, TextPart
from clicky.capabilities.fallback import NoUsableProvider, complete_chat, describe_error
from clicky.capabilities.kinds import Capability
from clicky.capabilities.registry import Capabilities, ChainMember
from clicky.protocol.events import Event, SceneBuilding, SceneFailed, SceneRender, SceneResult
from clicky.protocol.page import PageContext

log = logging.getLogger(__name__)

SCENE_KINDS = ("three", "canvas", "svg", "html")
MAX_CODE_CHARS = 60_000               # what the protocol and the sandbox accept
MAX_BRIEF_CHARS = 4_000
# compact: small local models write slowly (7-20 tokens/s) and may run with a small context (prompt ~1.2k + answer)
CODEGEN_MAX_TOKENS = {"full": 8_000, "compact": 1_800}
CODEGEN_TIMEOUT_S = 300.0             # a small local model on a 4 GB GPU writes ~70 lines at 6-8 tokens/s
RESULT_TIMEOUT_S = 45.0               # the browser renders, then watches the first frames
MAX_ATTEMPTS = 2                      # the first render plus one automatic repair
KEPT_SCENES = 8
_FENCE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\n(.*?)(?:\n```|\Z)", re.DOTALL)
_SVG = re.compile(r"<svg\b.*</svg\s*>", re.DOTALL | re.IGNORECASE)


class SceneCodeError(ValueError):
    """The codegen model's answer holds no usable code."""


@dataclass(frozen=True)
class SceneJob:
    scene_id: str
    turn_id: str
    kind: str
    title: str
    brief: str
    utterance: str
    base: SceneRecord | None = None       # the scene on screen, when this request may be an edit of it


@dataclass(frozen=True)
class SceneRecord:
    scene_id: str
    kind: str
    title: str
    code: str


# ------------------------------------------------------------------------------------------------------- prompts


@cache
def codegen_system(variant: str) -> str:
    return resources.files("clicky.brain.prompts").joinpath(f"codegen_{variant}.md").read_text(encoding="utf-8")


def _variant(info: ModelInfo | None) -> str:
    return "full" if info is not None and info.prompt == "full" else "compact"


def job_message(job: SceneJob) -> str:
    lines = [f"Kind: {job.kind}"]
    if job.title:
        lines.append(f"Title: {job.title}")
    if job.utterance:
        lines.append(f'The user said: "{" ".join(job.utterance.split())[:600]}"')
    lines.append(f"Brief: {job.brief[:MAX_BRIEF_CHARS] or job.title}")
    if job.base is not None:
        lines += [
            "",
            f'The scene on screen now is "{job.base.title}" (kind {job.base.kind}). If this request changes it, '
            "start from its code below and keep what wasn't asked to change; if it asks for something different, "
            "write a new scene.",
            "Its code:",
            job.base.code,
        ]
    lines += ["", f"Write the {job.kind} scene now. Reply with the code only."]
    return "\n".join(lines)


def codegen_request(job: SceneJob, info: ModelInfo | None) -> ChatRequest:
    variant = _variant(info)
    return ChatRequest(system=codegen_system(variant), messages=(ChatMessage("user", (TextPart(job_message(job)),)),),
                       max_tokens=CODEGEN_MAX_TOKENS[variant], purpose="codegen")


def repair_request(job: SceneJob, code: str, error: str, info: ModelInfo | None) -> ChatRequest:
    """Full models get their code back with the error; compact ones write it afresh with the error as a warning
    (replaying the failed code would not fit a small local context window)."""
    variant = _variant(info)
    problem = " ".join(error.split())[:1500]
    if variant == "compact":
        retry = (f"{job_message(job)}\n\nA first attempt failed in the scene sandbox with: {problem}\n"
                 "Write it again, simpler, and avoid that mistake.")
        return ChatRequest(system=codegen_system(variant), messages=(ChatMessage("user", (TextPart(retry),)),),
                           max_tokens=CODEGEN_MAX_TOKENS[variant], purpose="codegen")
    fix = (f"That code failed in the scene sandbox:\n{problem}\n\n"
           f"Reply with the whole corrected {job.kind} code only, following every rule.")
    messages = (
        ChatMessage("user", (TextPart(job_message(job)),)),
        ChatMessage("assistant", (TextPart(code),)),
        ChatMessage("user", (TextPart(fix),)),
    )
    return ChatRequest(system=codegen_system(variant), messages=messages, max_tokens=CODEGEN_MAX_TOKENS[variant],
                       purpose="codegen")


def extract_code(kind: str, text: str) -> str:
    """The code in a codegen answer: fenced or bare; svg keeps just the <svg> element. Raises SceneCodeError."""
    body = text.strip()
    fences = _FENCE.findall(body)
    if fences:
        body = max(fences, key=len).strip()      # the main block (a model sometimes adds a usage snippet)
    if kind == "svg":
        match = _SVG.search(body)
        if match is None:
            raise SceneCodeError("the answer has no <svg> element")
        body = match.group(0)
    if not body:
        raise SceneCodeError("the answer is empty")
    if len(body) > MAX_CODE_CHARS:
        raise SceneCodeError(f"the code is too long ({len(body)} characters)")
    return body


# ------------------------------------------------------------------------------------------------------- manager


def _member_info(member: ChainMember) -> ModelInfo | None:
    info = getattr(member.provider, "info", None)
    return info() if callable(info) else None


class SceneManager:
    """One per session: runs at most one scene job, routes scene.result to it, remembers recent scenes' code."""

    def __init__(self, capabilities: Capabilities, emit: Callable[[Event], None], *,
                 codegen_timeout_s: float = CODEGEN_TIMEOUT_S, result_timeout_s: float = RESULT_TIMEOUT_S) -> None:
        self._capabilities = capabilities
        self._emit = emit
        self._codegen_timeout_s = codegen_timeout_s
        self._result_timeout_s = result_timeout_s
        self._task: asyncio.Task[None] | None = None
        self._job: SceneJob | None = None
        self._waiting: tuple[str, int, asyncio.Future[SceneResult]] | None = None
        self._scenes: OrderedDict[str, SceneRecord] = OrderedDict()
        self.finished: list[tuple[str, str, int]] = []      # (scene_id, "shown" | "failed", attempts): tests, logs

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def record(self, scene_id: str) -> SceneRecord | None:
        return self._scenes.get(scene_id)

    def start(self, turn_id: str, request: SceneRequest, utterance: str, page: PageContext | None) -> str:
        """Start building a scene (replacing any job still running); returns its id."""
        self._stop_task()
        base = self._scenes.get(page.scene.scene_id) if page is not None and page.scene is not None else None
        job = SceneJob(scene_id=uuid.uuid4().hex, turn_id=turn_id, kind=request.kind,
                       title=request.title or "Scene", brief=request.brief, utterance=utterance, base=base)
        self._emit(SceneBuilding(turn_id=turn_id, scene_id=job.scene_id, kind=job.kind, title=job.title))  # type: ignore[arg-type]
        self._job = job
        self._task = asyncio.create_task(self._run(job), name=f"scene-{job.scene_id}")
        return job.scene_id

    def on_result(self, result: SceneResult) -> None:
        waiting = self._waiting
        if waiting is None:
            return
        scene_id, attempt, future = waiting
        if result.scene_id == scene_id and result.attempt == attempt and not future.done():
            future.set_result(result)

    def _stop_task(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._waiting = None

    async def cancel(self, reason: str | None = None) -> None:
        """Stop the running job; with a reason, the stage is told (it would otherwise wait for the scene)."""
        task, job = self._task, self._job
        self._stop_task()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            if reason is not None and job is not None and not any(job.scene_id == done[0] for done in self.finished):
                self._fail(job, reason, 0)

    # ------------------------------------------------------------------------------------------------ job

    async def _write(self, build: Callable[[ChainMember], ChatRequest]) -> tuple[str, float]:
        """The codegen model's whole answer and how long it took. Refusals and provider errors end the job."""
        started = time.perf_counter()
        text, outcome = await asyncio.wait_for(
            complete_chat(self._capabilities.get(Capability.CODEGEN), build), self._codegen_timeout_s)
        took = round((time.perf_counter() - started) * 1000, 1)
        finish = outcome.finish
        if finish.stop_reason == "refusal":
            raise SceneCodeError("the codegen model declined this scene")
        if finish.stop_reason == "error":
            raise SceneCodeError(finish.error or "the codegen model reported an error")
        usage = finish.usage
        log.info("scene code from %s: %d chars in %.1f s (stop %s, output tokens %s)", outcome.provider, len(text),
                 took / 1000, finish.stop_reason, usage.output_tokens if usage else None)
        return text, took

    async def _render(self, job: SceneJob, code: str, attempt: int, codegen_ms: float) -> SceneResult:
        future: asyncio.Future[SceneResult] = asyncio.get_running_loop().create_future()
        self._waiting = (job.scene_id, attempt, future)
        log.debug("scene %s attempt %d code:\n%s", job.scene_id, attempt, code)
        self._emit(SceneRender(turn_id=job.turn_id, scene_id=job.scene_id, kind=job.kind, title=job.title,  # type: ignore[arg-type]
                               code=code, attempt=attempt, codegen_ms=codegen_ms))
        try:
            return await asyncio.wait_for(future, self._result_timeout_s)
        except TimeoutError:
            return SceneResult(scene_id=job.scene_id, attempt=attempt, ok=False, unavailable=True,
                               error="the browser did not report back")
        finally:
            self._waiting = None

    async def _run(self, job: SceneJob) -> None:
        attempt = 0
        try:
            text, took = await self._write(lambda member: codegen_request(job, _member_info(member)))
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    code = extract_code(job.kind, text)
                except SceneCodeError as exc:            # no usable code in the answer: that's what gets repaired
                    code, error = text.strip()[:MAX_CODE_CHARS], str(exc)
                else:
                    result = await self._render(job, code, attempt, took)
                    if result.ok:
                        self._remember(SceneRecord(job.scene_id, job.kind, job.title, code))
                        self.finished.append((job.scene_id, "shown", attempt))
                        log.info("scene %s shown after %d attempt(s)", job.scene_id, attempt)
                        return
                    error = result.error or "the scene did not render"
                    if result.unavailable:
                        self._fail(job, error, attempt)
                        return
                if attempt == MAX_ATTEMPTS:
                    self._fail(job, error, attempt)
                    return
                log.info("scene %s failed (%s); asking for a repair", job.scene_id, error[:200])
                failed = code
                text, took = await self._write(
                    lambda member, failed=failed, error=error: repair_request(job, failed, error, _member_info(member)))
        except asyncio.CancelledError:
            raise
        except NoUsableProvider as exc:
            self._fail(job, f"no scene model is available: {exc}", attempt)
        except TimeoutError:
            self._fail(job, "writing the scene took too long", attempt)
        except SceneCodeError as exc:
            self._fail(job, str(exc), attempt)
        except Exception as exc:
            log.warning("scene %s failed: %s", job.scene_id, describe_error(exc))
            self._fail(job, describe_error(exc), attempt)

    def _fail(self, job: SceneJob, error: str, attempts: int) -> None:
        self.finished.append((job.scene_id, "failed", attempts))
        log.info("scene %s failed: %s", job.scene_id, error[:300])
        self._emit(SceneFailed(turn_id=job.turn_id, scene_id=job.scene_id, error=error[:500]))

    def _remember(self, record: SceneRecord) -> None:
        self._scenes[record.scene_id] = record
        while len(self._scenes) > KEPT_SCENES:
            self._scenes.popitem(last=False)
