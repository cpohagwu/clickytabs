"""One authenticated WebSocket connection.

run() = anyio task group (reader, writer) - anyio, not asyncio.TaskGroup, so Starlette/uvicorn cancel
scopes propagate cleanly:
- the reader parses frames; malformed frames become `error` events (the connection stays open); it raises
  WebSocketDisconnect when the peer goes away, which tears the group down;
- the writer drains an outbound queue; a failed send is treated as a disconnect (closed flag, no further sends).
At most one turn runs per session: a new `turn.start` cancels the running turn first (barge-in). Turn tasks never
propagate exceptions into the group - run_text_turn / run_voice_turn convert every failure into `error` +
`turn.done`. A voice turn gets its `audio.chunk`s through an inbox queue; chunks for any other turn are ignored.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import uuid

import anyio
from fastapi import WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from clicky.automation.tasks import TaskManager
from clicky.brain.directives import SceneRequest, TaskRequest
from clicky.brain.history import Conversation, ConversationStore
from clicky.brain.prompts import system_prompt
from clicky.brain.scenes import SceneManager
from clicky.capabilities.registry import Capabilities
from clicky.protocol.events import (
    AudioChunkEvent,
    AudioEnd,
    ConfigGet,
    ConfigSet,
    ConsentResponse,
    ConversationReset,
    ConversationStarted,
    ErrorEvent,
    Event,
    Hello,
    Ping,
    PlaybackEvent,
    Pong,
    SceneResult,
    SessionBundle,
    TaskCancel,
    TaskConfirmResponse,
    TurnCancel,
    TurnDone,
    TurnStart,
    parse_client_event,
)
from clicky.protocol.page import PageContext
from clicky.session.config_service import ConfigService
from clicky.session.text_turn import run_text_turn
from clicky.session.turn import TurnContext
from clicky.session.voice_turn import AudioItem, run_voice_turn

log = logging.getLogger(__name__)

OUTBOX_LIMIT = 1024
_STOP = object()


MAX_TURN_AUDIO_BYTES = 8_000_000      # ~30 min of 32 kbit/s opus; push-to-talk stops at 60 s


class _ActiveTurn:
    def __init__(self, ctx: TurnContext, task: asyncio.Task[None],
                 inbox: asyncio.Queue[AudioItem] | None = None) -> None:
        self.ctx = ctx
        self.task = task
        self.inbox = inbox
        self.audio_bytes = 0
        self.audio_ended = False


class Session:
    def __init__(self, websocket: WebSocket, capabilities: Capabilities, client: Hello,
                 conversations: ConversationStore | None = None, *, config: ConfigService | None = None) -> None:
        self.id = uuid.uuid4().hex
        self.client = client
        self._ws = websocket
        self._capabilities = capabilities
        self._conversations = conversations if conversations is not None else ConversationStore()   # empty is falsy
        self.conversation: Conversation = self._conversations.get(client.conversation_id)
        self._outbox: asyncio.Queue[object] = asyncio.Queue(maxsize=OUTBOX_LIMIT)
        self._turn: _ActiveTurn | None = None
        self._closed = False
        self._task: asyncio.Task[None] | None = None
        self.scenes = SceneManager(capabilities, self.emit)      # outlives turns: codegen takes a while
        self.tasks = TaskManager(capabilities, capabilities.settings, self.emit)   # browser tasks
        self._config = config                                    # options page: config.get / config.set
        self._jobs: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------------------------------------ sending

    async def send(self, event: Event) -> None:
        if not self._closed:
            await self._outbox.put(event)

    def emit(self, event: Event) -> None:
        """Non-blocking send used by turn tasks; drops (with a warning) only if the client stopped reading."""
        if self._closed:
            return
        try:
            self._outbox.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("session %s outbox full; dropping %s", self.id, getattr(event, "type", event))

    # ------------------------------------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        self._task = asyncio.current_task()
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(self._reader, name=f"ws-reader-{self.id}")
                group.start_soon(self._writer, name=f"ws-writer-{self.id}")
        except* WebSocketDisconnect:
            pass
        except* (OSError, RuntimeError) as group_error:      # transport died mid-send
            log.debug("session %s transport error: %s", self.id, group_error.exceptions)
        finally:
            await self.close()

    async def close(self, code: int = 1001, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        for job in list(self._jobs):
            job.cancel()
        await self._cancel_turn(reason=None)
        await self.scenes.cancel()
        await self.tasks.cancel()                  # kills the worker and its browser (the user's cookies with it)
        if self._ws.application_state == WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await self._ws.close(code=code, reason=reason)

    async def shutdown(self) -> None:
        """Server shutdown: stop the run() task (which closes the socket and cancels any turn)."""
        await self.close()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------------------------------------------ loops

    async def _writer(self) -> None:
        while True:
            item = await self._outbox.get()
            if item is _STOP or self._closed:
                return
            assert isinstance(item, Event)
            try:
                await self._ws.send_text(item.model_dump_json())
            except (WebSocketDisconnect, RuntimeError, OSError) as exc:
                self._closed = True
                raise WebSocketDisconnect(code=1006) from exc

    async def _reader(self) -> None:
        while True:
            message = await self._ws.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(code=message.get("code", 1000))
            text = message.get("text")
            if text is None:
                self.emit(ErrorEvent(code="bad_request", message="binary frames are not supported"))
                continue
            try:
                event = parse_client_event(text)
            except ValidationError as exc:
                turn_id = _rejected_turn_id(text)
                self.emit(ErrorEvent(code="bad_request", message=_brief(exc), turn_id=turn_id))
                if turn_id is not None:     # every turn.start ends with turn.done, even a malformed one
                    self.emit(TurnDone(turn_id=turn_id, reason="error", detail=_brief(exc)))
                continue
            await self._dispatch(event)

    # ------------------------------------------------------------------------------------------------ dispatch

    async def _dispatch(self, event: Event) -> None:
        if isinstance(event, Ping):
            self.emit(Pong(t=event.t))
        elif isinstance(event, TurnStart):
            await self._start_turn(event)
        elif isinstance(event, TurnCancel):
            if self._turn is not None and self._turn.ctx.turn_id == event.turn_id:
                await self._cancel_turn(reason="cancelled")
        elif isinstance(event, AudioChunkEvent | AudioEnd):
            await self._route_audio(event)
        elif isinstance(event, PlaybackEvent):
            log.debug("playback %s seq %s %s", event.turn_id, event.seq, event.state)
        elif isinstance(event, SceneResult):
            self.scenes.on_result(event)
        elif isinstance(event, ConsentResponse):
            self.tasks.on_consent(event)
        elif isinstance(event, SessionBundle):
            self.tasks.on_bundle(event)
        elif isinstance(event, TaskConfirmResponse):
            await self.tasks.on_confirm(event)
        elif isinstance(event, TaskCancel):
            await self.tasks.on_cancel(event)
        elif isinstance(event, ConversationReset):
            await self._cancel_turn(reason="cancelled")
            await self.scenes.cancel("Stopped: a new conversation started.")
            await self.tasks.cancel("Stopped: a new conversation started.")
            self.conversation = self._conversations.new()
            self.emit(ConversationStarted(conversation_id=self.conversation.id))
        elif isinstance(event, ConfigGet | ConfigSet):
            if self._config is None:
                self.emit(ErrorEvent(code="unsupported", message="runtime configuration is not available"))
            else:   # a switch health-checks providers: never block the reader on it
                job = asyncio.create_task(self._answer_config(self._config, event), name=f"config-{event.request_id}")
                self._jobs.add(job)
                job.add_done_callback(self._jobs.discard)
        elif isinstance(event, Hello):
            self.emit(ErrorEvent(code="bad_request", message="already authenticated"))
        else:
            self.emit(ErrorEvent(code="unsupported", message=f"{event.type} is not supported by this backend"))

    async def _answer_config(self, config: ConfigService, event: ConfigGet | ConfigSet) -> None:
        try:
            state = await (config.apply(event) if isinstance(event, ConfigSet) else config.state(event.request_id))
        except Exception as exc:
            log.exception("config request failed")
            self.emit(ErrorEvent(code="internal_error", message=f"configuration failed: {exc}"))
            return
        self.emit(state)

    async def _start_turn(self, event: TurnStart) -> None:
        await self._cancel_turn(reason="cancelled")          # barge-in: at most one active turn
        ctx = TurnContext(turn_id=event.turn_id, emit=self.emit)
        inbox: asyncio.Queue[AudioItem] | None = None
        on_scene = on_task = None
        if event.tab_id is not None:              # scenes and tasks need a tab (not the popup's debug box)
            def on_scene(request: SceneRequest, utterance: str, page: PageContext | None) -> str:
                return self.scenes.start(event.turn_id, request, utterance, page)

            def on_task(request: TaskRequest, page: PageContext | None) -> str:
                return self.tasks.start(event.turn_id, request, page.url if page is not None else None)
        if event.mode == "voice":
            # Speech models load and the brain's prompt cache warms while the user is still speaking.
            self._capabilities.prewarm_speech()
            self._capabilities.prewarm_brain(system_prompt)
            inbox = asyncio.Queue()
            work = run_voice_turn(event, ctx, self._capabilities, inbox, self.conversation, on_scene=on_scene,
                                  on_task=on_task)
        else:
            work = run_text_turn(event, ctx, self._capabilities, self.conversation, on_scene=on_scene,
                                 on_task=on_task)
        task = asyncio.create_task(work, name=f"turn-{event.turn_id}")
        self._turn = _ActiveTurn(ctx, task, inbox)
        task.add_done_callback(lambda _t, turn=self._turn: self._on_turn_done(turn))

    async def _route_audio(self, event: AudioChunkEvent | AudioEnd) -> None:
        turn = self._turn
        if turn is None or turn.inbox is None or turn.ctx.turn_id != event.turn_id or turn.audio_ended:
            return                                   # late audio of a cancelled or finished turn: ignore
        if isinstance(event, AudioEnd):
            turn.audio_ended = True
            turn.inbox.put_nowait(None)
            return
        try:
            chunk = base64.b64decode(event.data_b64, validate=True)
        except (binascii.Error, ValueError):
            self.emit(ErrorEvent(code="bad_request", message="audio.chunk data_b64 is not base64",
                                 turn_id=event.turn_id))
            return
        turn.audio_bytes += len(chunk)
        if turn.audio_bytes > MAX_TURN_AUDIO_BYTES:
            await self._cancel_turn(reason=None)
            turn.ctx.fail("bad_request", "too much audio for one turn")
            return
        turn.inbox.put_nowait((chunk, event.mime))

    def _on_turn_done(self, turn: _ActiveTurn) -> None:
        if self._turn is turn:
            self._turn = None
        if not turn.task.cancelled() and (exc := turn.task.exception()) is not None:
            log.error("turn %s crashed: %r", turn.ctx.turn_id, exc)
            turn.ctx.fail("internal_error", "the turn failed unexpectedly")

    async def _cancel_turn(self, *, reason: str | None) -> None:
        turn, self._turn = self._turn, None
        if turn is None:
            return
        turn.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await turn.task
        if reason is not None:
            turn.ctx.finish("cancelled")


def _rejected_turn_id(text: str) -> str | None:
    """The turn_id of a turn.start that failed validation (e.g. an oversized page), if it has a usable one."""
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("type") != "turn.start":
        return None
    turn_id = raw.get("turn_id")
    return turn_id if isinstance(turn_id, str) and 0 < len(turn_id) <= 128 else None


def _brief(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))
    return f"invalid event: {first.get('msg', 'validation error')}" + (f" at {location}" if location else "")
