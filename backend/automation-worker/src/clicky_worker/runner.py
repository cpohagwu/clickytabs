"""One browser task: browser-use in an ephemeral profile seeded with the user's consented session.

    task.start  ->  storage_state (cookies + localStorage of the user's site), allowed_domains pinned, the user's
                    user agent and language, a fresh temp profile (deleted afterwards), no auto-installed extensions
                -> Agent steps -> task.step (the agent's next goal + a small screenshot) for the tracker
                -> ask_user before irreversible steps -> confirm.request / confirm.response
                -> task.done (success, the agent's own summary, the last URL)

Cancel: the agent checks a stop flag before every step. The scripted run (provider "fake") replays the same events
without a browser, for the e2e suite.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clicky_worker.guard import Gate, guarded_tools
from clicky_worker.hitl import HITL_RULES, Confirmations
from clicky_worker.ipc import WorkerTaskDone, WorkerTaskStart, WorkerTaskStep

THUMB_WIDTH = 360
# browser-use prints the whole storage_state (cookie values included) in a warning when the profile directory's path
# has no "tmp" in it; our directory always has, and every log record is scrubbed anyway.
PROFILE_PREFIX = "clicky-agent-tmp-"
_COOKIE_VALUE = re.compile(r"""(['"]value['"]\s*:\s*)(['"])(?:(?!\2).)*\2""")


class _ScrubCookies(logging.Filter):
    """Masks cookie values and drops storage_state dumps in anything browser-use logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if "storage_state=" in message:
            return False
        if "'value'" in message or '"value"' in message:
            record.msg, record.args = _COOKIE_VALUE.sub(r"\1'[REDACTED]'", message), None
        return True


def guard_logs() -> None:
    """Install the scrubber on every handler there is (browser-use sets up its own)."""
    scrub = _ScrubCookies()
    loggers = [logging.getLogger(), *(logging.getLogger(name) for name in list(logging.root.manager.loggerDict))]
    for logger in loggers:
        for handler in getattr(logger, "handlers", []):
            if not any(isinstance(f, _ScrubCookies) for f in handler.filters):
                handler.addFilter(scrub)
# web search leaves the approved sites; no local files; no JavaScript in the page (it could click past the gate)
EXCLUDED_ACTIONS = ["search", "upload_file", "save_as_pdf", "evaluate"]


def thumbnail(png_b64: str | None) -> str | None:
    """A small JPEG of the agent's viewport for the tracker."""
    if not png_b64:
        return None
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(base64.b64decode(png_b64)))
        image.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 2))
        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=60)
        return base64.b64encode(out.getvalue()).decode("ascii")
    except Exception:
        return None


def task_prompt(start: WorkerTaskStart, start_url: str | None) -> str:
    # "https://*.shop.example", "https://shop.example/", "*.shop.example" -> "shop.example"
    sites = sorted({pattern.split("://", 1)[-1].replace("*.", "").strip("/")
                    for pattern in start.browser.allowed_domains})
    lines = [start.goal.strip()]
    if start_url:
        lines.append(f"Start at {start_url}.")
    if start.storage_state and start.storage_state.get("cookies"):
        lines.append("The browser already has the user's login for this site, so you are signed in as them.")
    if sites:
        lines.append(f"Stay on these sites: {', '.join(sites)}.")
    lines.append("When you finish, say in one or two plain sentences what you did and anything the user should "
                 "check.")
    return "\n".join(lines)


def _remove_profile(path: Path) -> None:
    for _ in range(10):                       # Chrome can hold files for a moment after it exits
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.3)


async def run_live(start: WorkerTaskStart, emit: Callable[[object], None], confirmations: Confirmations,
                   stop: asyncio.Event) -> WorkerTaskDone:
    from browser_use import Agent, BrowserProfile
    from browser_use.agent.views import ActionResult

    from clicky_worker.llm_factory import agent_settings, build_llm

    guard_logs()
    task_id = start.task_id
    start_url = start.start_url
    browser = start.browser
    profile_dir = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX))
    args = list(browser.args)
    if browser.locale:
        args.append(f"--lang={browser.locale}")
    profile_kwargs: dict[str, Any] = {
        "headless": browser.headless,
        "user_data_dir": profile_dir,
        "storage_state": start.storage_state,
        "allowed_domains": browser.allowed_domains or None,
        "user_agent": browser.user_agent,
        "args": args,
        "enable_default_extensions": False,     # they would be downloaded, and auto-click cookie banners
        "keep_alive": False,
        "highlight_elements": False,
        "accept_downloads": False,
    }
    if browser.viewport:
        width, height = browser.viewport
        profile_kwargs["viewport"] = {"width": width, "height": height}
        profile_kwargs["window_size"] = {"width": width, "height": height + 120}
    if browser.executable_path:
        profile_kwargs["executable_path"] = browser.executable_path

    gate = Gate(confirmations, task_id)              # irreversible clicks need the user's OK, whatever the model
    tools = guarded_tools(gate, EXCLUDED_ACTIONS)

    @tools.action("Ask the user to approve an irreversible step (an order, a payment, a booking, sending, posting, "
                  "deleting) before doing it. Pass one short question. Returns the user's answer.")
    async def ask_user(question: str) -> ActionResult:
        answer = await confirmations.ask(task_id, question)
        if answer.approved:
            gate.grant_once()                    # the click this question was about goes ahead without a second ask
            return ActionResult(extracted_content=f"The user approved: {question}",
                                long_term_memory=f"User approved: {question}")
        reason = f" ({answer.note})" if answer.note else ""
        return ActionResult(extracted_content=f"The user did not approve{reason}. Do not do it: finish now and "
                                              "report what you did so far.",
                            long_term_memory=f"User declined: {question}")

    last_url: list[str | None] = [start_url]

    async def on_step(state: Any, output: Any, n: int) -> None:
        last_url[0] = getattr(state, "url", None) or last_url[0]
        text = (getattr(output, "next_goal", None) or "").strip() or _describe_actions(output)
        emit(WorkerTaskStep(task_id=task_id, n=n, action_text=text[:300], url=last_url[0],
                            thumb_b64=await asyncio.to_thread(thumbnail, getattr(state, "screenshot", None))))

    async def should_stop() -> bool:
        return stop.is_set()

    agent = None
    try:
        profile = BrowserProfile(**profile_kwargs)
        guard_logs()                                   # handlers browser-use added while it set up
        agent = Agent(
            task=task_prompt(start, start_url),
            llm=build_llm(start.llm),
            browser_profile=profile,
            tools=tools,
            extend_system_message=HITL_RULES,
            register_new_step_callback=on_step,
            register_should_stop_callback=should_stop,
            use_judge=False,
            generate_gif=False,
            calculate_cost=False,
            enable_signal_handler=False,
            max_failures=3,
            step_timeout=int(start.limits.step_timeout_s),
            file_system_path=str(profile_dir / "files"),
            **agent_settings(start.llm),
        )
        history = await agent.run(max_steps=start.limits.max_steps)
        urls = [url for url in history.urls() if url]
        final_url = urls[-1] if urls else last_url[0]
        if stop.is_set():
            return WorkerTaskDone(task_id=task_id, success=False, summary="Stopped.", final_url=final_url)
        summary = (history.final_result() or "").strip()
        success = bool(history.is_done() and history.is_successful())
        if not summary:
            summary = "The task did not finish." if not history.is_done() else "Done."
        return WorkerTaskDone(task_id=task_id, success=success, summary=summary[:2000], final_url=final_url)
    except Exception as exc:
        return WorkerTaskDone(task_id=task_id, success=False, summary=f"The browser task failed: {exc}"[:500],
                              final_url=last_url[0])
    finally:
        if agent is not None:
            with contextlib.suppress(Exception):
                await agent.close()
        await asyncio.to_thread(_remove_profile, profile_dir)


def _describe_actions(output: Any) -> str:
    actions = getattr(output, "action", None) or []
    names = []
    for action in actions:
        data = action.model_dump(exclude_none=True) if hasattr(action, "model_dump") else {}
        names.extend(name.replace("_", " ") for name in data)
    return ", ".join(names) or "Working"


async def run_scripted(start: WorkerTaskStart, emit: Callable[[object], None], confirmations: Confirmations,
                       stop: asyncio.Event) -> WorkerTaskDone:
    """No browser, no model: the events of a typical shopping task (the fake agent, for tests)."""
    task_id = start.task_id
    url = start.start_url
    cookies = len((start.storage_state or {}).get("cookies") or [])
    steps = [f"Opening {url} with {cookies} cookie(s) from your session", "Finding the item", "Adding it to the cart"]
    for n, text in enumerate(steps, 1):
        if stop.is_set():
            return WorkerTaskDone(task_id=task_id, success=False, summary="Stopped.", final_url=url)
        emit(WorkerTaskStep(task_id=task_id, n=n, action_text=text, url=url))
        await asyncio.sleep(0.4)
    answer = await confirmations.ask(task_id, "Place the order for $12.00?")
    if stop.is_set():
        return WorkerTaskDone(task_id=task_id, success=False, summary="Stopped.", final_url=url)
    if not answer.approved:
        return WorkerTaskDone(task_id=task_id, success=False, summary="Stopped before placing the order: you said no.",
                              final_url=url)
    emit(WorkerTaskStep(task_id=task_id, n=len(steps) + 1, action_text="Placing the order", url=url))
    return WorkerTaskDone(task_id=task_id, success=True, summary="Added the item to your cart and placed the order.",
                          final_url=url)
