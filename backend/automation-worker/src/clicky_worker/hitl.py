"""ask_user: the agent pauses before an irreversible step until the user answers in their tab.

The question goes out as `confirm.request`; the answer comes back as `confirm.response` through the command loop
(__main__.py), which resolves the waiting future. No answer within the limit counts as "no".
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from clicky_worker.ipc import WorkerConfirmRequest, WorkerConfirmResponse

# What the agent is told about asking (added to browser-use's system prompt).
HITL_RULES = """
You are acting in the user's own account, with their login. Before any step that commits them to something or
cannot be undone - placing or paying for an order, booking, sending a message or email, posting, deleting,
changing account settings, accepting terms - call the ask_user action with one short, specific question (what you are
about to do, and the price or recipient when there is one) and wait for the answer. Only continue if the user
approved. Never ask about reading, browsing, searching, filling a form you have not submitted, or adding to a cart.
Stay on the sites you were given. If a page asks for a password, a one-time code or a captcha, stop and report it.
""".strip()


@dataclass
class Answer:
    approved: bool
    note: str | None = None


class Confirmations:
    def __init__(self, emit: Callable[[object], None], timeout_s: float) -> None:
        self._emit = emit
        self._timeout_s = timeout_s
        self._waiting: dict[str, asyncio.Future[Answer]] = {}

    async def ask(self, task_id: str, question: str) -> Answer:
        request_id = uuid.uuid4().hex[:16]
        future: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        text = " ".join(question.split())[:300]
        self._emit(WorkerConfirmRequest(task_id=task_id, request_id=request_id, question=text))
        try:
            return await asyncio.wait_for(future, self._timeout_s)
        except TimeoutError:
            return Answer(False, "no answer from the user")
        finally:
            self._waiting.pop(request_id, None)

    def resolve(self, response: WorkerConfirmResponse) -> None:
        future = self._waiting.get(response.request_id)
        if future is not None and not future.done():
            future.set_result(Answer(response.approved, response.note))

    def cancel_all(self) -> None:
        for future in self._waiting.values():
            if not future.done():
                future.set_result(Answer(False, "the task was stopped"))
