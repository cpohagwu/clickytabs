"""Browser tasks - the session bridge, the consent / bundle / worker flow (stub worker), and the wire."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clicky.automation.session_bridge import allowed_domain_patterns, in_site, storage_state_from_bundle
from clicky.automation.tasks import TaskManager
from clicky.automation.worker_client import QUIET_ENV, SECRET_ENV, WorkerClient
from clicky.brain.directives import TaskRequest
from clicky.capabilities.registry import build_capabilities
from clicky.protocol.events import (
    ConsentRequest,
    ConsentResponse,
    CookieRecord,
    Event,
    OriginStorage,
    SessionBundle,
    TaskCancel,
    TaskConfirmRequest,
    TaskConfirmResponse,
    TaskDone,
    TaskStarted,
    TaskStep,
)
from tests.conftest import ORIGIN, hello, make_settings

TAB = "http://shop.test/products"


def bundle(task_id: str, **extra: Any) -> SessionBundle:
    return SessionBundle(task_id=task_id, site="shop.test", cookies=[
        CookieRecord(name="session", value="alice-token", domain="shop.test", host_only=True, http_only=True,
                     same_site="Lax"),
        CookieRecord(name="pref", value="dark", domain=".shop.test", path="/", expires=4_102_444_800.0),
        CookieRecord(name="chips", value="1", domain=".cdn.shop.test", secure=True, same_site="None",
                     partition_key="https://shop.test"),
        CookieRecord(name="tracker", value="x", domain=".evil.example"),
    ], origins=[OriginStorage(origin="http://shop.test", local_storage={"cart": "[]"}),
                OriginStorage(origin="https://evil.example", local_storage={"x": "y"})],
        user_agent="Mozilla/5.0 Test", locale="en-US", viewport=(1280, 800), **extra)


# ---------------------------------------------------------------------------------------------- the bridge


def test_the_bridge_keeps_only_the_sites_cookies_in_cdp_form() -> None:
    state = storage_state_from_bundle(bundle("t" * 12), allowed_sites={"shop.test"})
    names = [cookie["name"] for cookie in state["cookies"]]
    assert names == ["session", "pref", "chips"]                      # the other site's cookie is dropped
    session, pref, chips = state["cookies"]
    assert session["url"] == "http://shop.test/" and "domain" not in session and session["sameSite"] == "Lax"
    assert pref["domain"] == ".shop.test" and pref["expires"] == 4_102_444_800.0
    assert chips["partitionKey"] == {"topLevelSite": "https://shop.test", "hasCrossSiteAncestor": False}
    assert state["origins"] == [{"origin": "http://shop.test", "localStorage": [{"name": "cart", "value": "[]"}]}]
    # A site the user did not approve gets nothing at all.
    assert storage_state_from_bundle(bundle("t" * 12), allowed_sites={"other.test"}) == {"cookies": [], "origins": []}


def test_sites_and_navigation_patterns() -> None:
    assert in_site("shop.test", "shop.test") and in_site(".cdn.shop.test", "shop.test")
    assert not in_site("notshop.test", "shop.test") and not in_site(None, "shop.test")
    assert allowed_domain_patterns(["shop.test"], ["http://shop.test/x"]) == ["*.shop.test"]
    assert allowed_domain_patterns(["bank.example"], ["https://bank.example/"]) == ["https://bank.example/",
                                                                                    "https://*.bank.example"]


def test_the_worker_gets_no_inherited_secrets(monkeypatch) -> None:
    assert "ANTHROPIC_API_KEY" in SECRET_ENV and QUIET_ENV["ANONYMIZED_TELEMETRY"] == "false"


# --------------------------------------------------------------------------------------------- the manager


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.arrived = asyncio.Event()

    def __call__(self, event: Event) -> None:
        self.events.append(event)
        self.arrived.set()

    async def next(self, kind: type[Event], timeout: float = 15.0) -> Any:
        async with asyncio.timeout(timeout):
            while True:
                for event in self.events:
                    if isinstance(event, kind):
                        self.events.remove(event)
                        return event
                self.arrived.clear()
                await self.arrived.wait()


async def manager() -> tuple[TaskManager, Recorder, Any]:
    settings = make_settings()
    caps = await build_capabilities(settings)
    emit = Recorder()
    return TaskManager(caps, settings, emit), emit, caps


REQUEST = TaskRequest(goal="Add the Blue Mug to my cart and check out", start_url=None, session=True,
                      cross_site=False)


async def test_a_consented_task_runs_with_the_session_and_asks_before_ordering() -> None:
    tasks, emit, caps = await manager()
    try:
        task_id = tasks.start("turn-1", REQUEST, TAB)
        consent = await emit.next(ConsentRequest)
        assert (consent.task_id, consent.goal, consent.session, consent.start_url) == (
            task_id, REQUEST.goal, True, None)
        tasks.on_consent(ConsentResponse(task_id=task_id, decision="allow_once", sites=["shop.test"], tab_url=TAB))
        tasks.on_bundle(bundle(task_id))
        started = await emit.next(TaskStarted)
        assert started.sites == ["shop.test"] and started.cookies == 3
        first = await emit.next(TaskStep)
        assert first.action_text == f"Opening {TAB} with 3 cookie(s) from your session"
        question = await emit.next(TaskConfirmRequest)
        assert question.question == "Place the order for $12.00?"
        await tasks.on_confirm(TaskConfirmResponse(task_id=task_id, request_id="someone-else", approved=True))
        await tasks.on_confirm(TaskConfirmResponse(task_id=task_id, request_id=question.request_id, approved=True))
        done = await emit.next(TaskDone)
        assert done.success and done.final_url == TAB and "placed the order" in done.summary
        assert tasks.finished == [(task_id, True, done.summary)]
    finally:
        await tasks.cancel()
        await caps.aclose()


async def test_declined_offsite_and_cancelled_tasks_do_nothing() -> None:
    tasks, emit, caps = await manager()
    try:
        declined = tasks.start("turn-1", REQUEST, TAB)
        tasks.on_consent(ConsentResponse(task_id=declined, decision="deny"))
        assert "declined" in (await emit.next(TaskDone)).summary

        await asyncio.sleep(0)
        offsite = tasks.start("turn-2", TaskRequest(goal="Pay my bill", start_url="https://bank.example/pay",
                                                     session=True, cross_site=True), TAB)
        tasks.on_consent(ConsentResponse(task_id=offsite, decision="allow_once", sites=["shop.test"], tab_url=TAB))
        assert "not on a site you allowed" in (await emit.next(TaskDone)).summary

        await asyncio.sleep(0)
        waiting = tasks.start("turn-3", REQUEST, TAB)
        busy = tasks.start("turn-4", REQUEST, TAB)                      # one task at a time
        assert busy != waiting and "still running" in (await emit.next(TaskDone)).summary
        await tasks.on_cancel(TaskCancel(task_id=waiting))
        stopped = await emit.next(TaskDone)
        assert stopped.task_id == waiting and stopped.summary == "Stopped."
    finally:
        await tasks.cancel()
        await caps.aclose()


async def test_stop_reaches_a_running_task_and_a_closed_session_kills_it() -> None:
    tasks, emit, caps = await manager()
    try:
        task_id = tasks.start("turn-1", REQUEST, TAB)
        tasks.on_consent(ConsentResponse(task_id=task_id, decision="always", sites=["shop.test"], tab_url=TAB))
        tasks.on_bundle(bundle(task_id))
        await emit.next(TaskConfirmRequest)                             # the worker is waiting for the user
        await tasks.on_cancel(TaskCancel(task_id=task_id))
        done = await emit.next(TaskDone)
        assert not done.success and done.summary == "Stopped."

        second = tasks.start("turn-2", REQUEST, TAB)
        tasks.on_consent(ConsentResponse(task_id=second, decision="allow_once", sites=["shop.test"], tab_url=TAB))
        tasks.on_bundle(bundle(second))
        await emit.next(TaskStep)
        await tasks.cancel("Stopped: a new conversation started.")
        assert (await emit.next(TaskDone)).summary.startswith("Stopped: a new conversation")
        assert not tasks.busy
    finally:
        await tasks.cancel()
        await caps.aclose()


async def test_the_stub_worker_reports_steps_for_a_scripted_task(tmp_path) -> None:
    from clicky.automation.worker_client import WORKER_SRC
    from clicky.protocol.worker_ipc import (
        AgentLLMConfig,
        WorkerConfirmRequest,
        WorkerConfirmResponse,
        WorkerReady,
        WorkerTaskDone,
        WorkerTaskStart,
    )

    worker = await WorkerClient.spawn(stub=True, pythonpath=(WORKER_SRC,), log_path=tmp_path / "worker.log")
    try:
        assert isinstance(await worker.next_event(20), WorkerReady)
        await worker.send(WorkerTaskStart(task_id="task-0001", goal="Buy a mug", start_url="http://shop.test/",
                                          llm=AgentLLMConfig(provider="fake", model="fake")))
        seen: list[str] = []
        async for event in worker.events():
            seen.append(event.type)
            if isinstance(event, WorkerConfirmRequest):
                await worker.send(WorkerConfirmResponse(task_id="task-0001", request_id=event.request_id,
                                                        approved=False))
            if isinstance(event, WorkerTaskDone):
                assert not event.success and "you said no" in event.summary
                break
        assert seen == ["task.step"] * 3 + ["confirm.request", "task.done"]
    finally:
        await worker.close()


# ----------------------------------------------------------------------------------------------- the wire


def receive(ws: Any, kind: str) -> dict[str, Any]:
    while True:
        event = ws.receive_json()
        if event["type"] == kind:
            return event


@pytest.mark.timeout(60)
def test_a_task_over_the_wire(client: TestClient) -> None:
    with client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_json(hello())
        ws.receive_json()
        ws.send_json({"type": "turn.start", "turn_id": "k1", "mode": "text", "tab_id": 3,
                      "text": "[task] add the blue mug to my cart"})
        consent = receive(ws, "consent.request")
        assert consent["goal"] == "add the blue mug to my cart" and consent["turn_id"] == "k1"
        task_id = consent["task_id"]
        ws.send_json({"type": "consent.response", "task_id": task_id, "decision": "allow_once",
                      "sites": ["shop.test"], "tab_url": TAB})
        ws.send_json(bundle(task_id).model_dump(mode="json"))
        assert receive(ws, "task.started")["cookies"] == 3
        question = receive(ws, "task.confirm.request")
        ws.send_json({"type": "task.confirm.response", "task_id": task_id, "request_id": question["request_id"],
                      "approved": True})
        done = receive(ws, "task.done")
        assert done["success"] and done["final_url"] == TAB
        # A consent answer naming a bad site is a protocol error.
        ws.send_json({"type": "consent.response", "task_id": task_id, "decision": "allow_once",
                      "sites": ["Not A Site!"]})
        assert receive(ws, "error")["code"] == "bad_request"


def test_the_worker_never_logs_cookie_values() -> None:
    import logging
    import sys

    from clicky.automation.worker_client import WORKER_SRC

    sys.path.insert(0, str(WORKER_SRC))
    try:
        from clicky_worker.runner import PROFILE_PREFIX, _ScrubCookies
    finally:
        sys.path.remove(str(WORKER_SRC))
    assert "tmp" in PROFILE_PREFIX            # browser-use dumps storage_state when the profile path lacks "tmp"
    scrub = _ScrubCookies()
    record = logging.LogRecord("x", logging.WARNING, "", 0, "[{'name': 'session', 'value': 'alice-token'}]", None, None)
    assert scrub.filter(record) and "alice-token" not in record.getMessage()
    dump = logging.LogRecord("x", logging.WARNING, "", 0, "passed both storage_state={'cookies': []}", None, None)
    assert not scrub.filter(dump)


def test_the_gate_recognises_irreversible_clicks() -> None:
    import sys

    from clicky.automation.worker_client import WORKER_SRC

    sys.path.insert(0, str(WORKER_SRC))
    try:
        from clicky_worker.guard import element_label, irreversible
        from clicky_worker.runner import EXCLUDED_ACTIONS
    finally:
        sys.path.remove(str(WORKER_SRC))
    for label in ["Place order", "PLACE MY ORDER", "Buy now", "Pay $12.00", "Confirm and pay", "Send message",
                  "Delete account", "Book now", "Subscribe", "Complete purchase", "Post"]:
        assert irreversible(label), label
    for label in ["Add to cart", "View cart", "Search", "Next", "Continue shopping", "Sign in", "Remove from cart",
                  "Postcode", "Checkout"]:
        assert not irreversible(label), label
    assert "evaluate" in EXCLUDED_ACTIONS            # page JavaScript could click past the gate

    class Node:
        def __init__(self) -> None:
            self.attributes = {"value": "Place order", "type": "submit"}

        def get_all_children_text(self, max_depth: int = -1) -> str:
            return ""

    assert element_label(Node()) == "Place order"


# ------------------------------------------------------------------------------------------- Enter gate


class _Node:
    def __init__(self, tag: str, text: str = "", children: list[_Node] | None = None, **attributes: str) -> None:
        self.tag_name = tag
        self.attributes = attributes
        self.text = text
        self.children_nodes = children or []
        self.parent_node: _Node | None = None
        for child in self.children_nodes:
            child.parent_node = self

    def get_all_children_text(self, max_depth: int = -1) -> str:
        return self.text


class _Session:
    def __init__(self, node: _Node | None, probe: dict[str, Any] | Exception) -> None:
        self.node, self.probe = node, probe
        session = self

        class _Runtime:
            async def evaluate(self, params: dict[str, Any], session_id: str) -> dict[str, Any]:
                if isinstance(session.probe, Exception):
                    raise session.probe
                return {"result": {"value": session.probe}}

        class _Cdp:
            session_id = "s1"
            cdp_client = type("Client", (), {"send": type("Send", (), {"Runtime": _Runtime()})()})()

        self._cdp = _Cdp()

    async def get_element_by_index(self, index: int) -> _Node | None:
        return self.node

    async def get_current_page_url(self) -> str:
        return "http://shop.test/checkout"

    async def get_or_create_cdp_session(self, target_id: Any = None, focus: bool = True) -> Any:
        return self._cdp


class _Action:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def model_dump(self, exclude_unset: bool = True) -> dict[str, Any]:
        return self.data


@pytest.fixture
def worker_guard(monkeypatch):
    import sys
    import types

    from clicky.automation.worker_client import WORKER_SRC

    views = types.ModuleType("browser_use.agent.views")

    class ActionResult:
        def __init__(self, error: str | None = None, **_: Any) -> None:
            self.error = error

    views.ActionResult = ActionResult
    for name, module in (("browser_use", types.ModuleType("browser_use")),
                         ("browser_use.agent", types.ModuleType("browser_use.agent")),
                         ("browser_use.agent.views", views)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.syspath_prepend(str(WORKER_SRC))
    from clicky_worker import guard
    from clicky_worker.hitl import Answer

    class Confirm:
        def __init__(self) -> None:
            self.questions: list[str] = []

        async def ask(self, task_id: str, question: str) -> Answer:
            self.questions.append(question)
            return Answer(False)

    return guard, Confirm


async def test_enter_that_would_submit_an_order_is_asked_about_first(worker_guard) -> None:
    guard, Confirm = worker_guard
    card = _Node("input", type="text", placeholder="Card number")
    _Node("form", children=[card, _Node("button", "Place order", type="submit")])
    confirm = Confirm()
    gate = guard.Gate(confirm, "task-1")
    checkout = {"inForm": True, "submits": ["Place order"], "editable": True, "field": "Card number"}

    refused = await gate.check(_Action({"send_keys": {"keys": "Enter"}}), _Session(card, checkout))
    assert refused is not None and "pressing Enter" in refused.error
    refused = await gate.check(_Action({"input": {"index": 3, "text": "4242 4242\n"}}), _Session(card, checkout))
    assert refused is not None
    assert confirm.questions == ["Press Enter to submit “Place order” on shop.test?"] * 2

    gate.grant_once()                     # the agent's own ask_user was approved: one such step goes ahead
    assert await gate.check(_Action({"send_keys": {"keys": "Control+Enter"}}), _Session(card, checkout)) is None
    assert len(confirm.questions) == 2


async def test_enter_in_a_search_box_goes_ahead_but_a_message_box_or_an_unknown_field_asks(worker_guard) -> None:
    guard, Confirm = worker_guard
    confirm = Confirm()
    gate = guard.Gate(confirm, "task-1")
    search = {"inForm": True, "submits": ["Search"], "editable": True, "field": "q"}
    assert await gate.check(_Action({"send_keys": {"keys": "Enter"}}), _Session(None, search)) is None
    assert await gate.check(_Action({"input": {"index": 1, "text": "mugs"}}), _Session(None, search)) is None
    assert await gate.check(_Action({"send_keys": {"keys": "PageDown"}}), _Session(None, search)) is None
    assert confirm.questions == []

    chat = {"inForm": False, "submits": [], "editable": True, "field": "Write a message"}
    assert await gate.check(_Action({"send_keys": {"keys": "Enter"}}), _Session(None, chat)) is not None
    assert await gate.check(_Action({"send_keys": {"keys": "Enter"}}), _Session(None, RuntimeError("gone"))) \
        is not None
    assert confirm.questions == ["Press Enter to send what I typed on shop.test?",
                                 "Press Enter on shop.test? (I couldn't tell what it would submit.)"]
