"""/v1/ws handshake.

1. Origin must be an allowed chrome-extension:// origin, else close *before* accept (uvicorn answers HTTP 403;
   the browser sees 1006 and the service worker probes /healthz to explain it).
2. The first frame must arrive within settings.hello_timeout_s (else 4401) and be a text frame (else 4400) holding
   a valid event (else 4400) that is a `hello` with the right token and the Origin's extension ID (else 4401).
3. A client that disconnects during the handshake is never sent a close frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from clicky import __version__
from clicky.api.health import WS_PATH
from clicky.brain.budget import page_budget
from clicky.protocol.events import Hello, Welcome, parse_client_event
from clicky.security import extension_id_matches, origin_allowed, token_matches

log = logging.getLogger(__name__)
router = APIRouter()

CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403


async def _close(websocket: WebSocket, code: int, reason: str = "") -> None:
    with contextlib.suppress(WebSocketDisconnect, OSError):
        await websocket.close(code=code, reason=reason)


@router.websocket(WS_PATH)
async def ws_endpoint(websocket: WebSocket) -> None:
    app = websocket.app
    settings = app.state.settings
    origin = websocket.headers.get("origin")
    if not origin_allowed(origin, settings.allowed_extension_ids):
        log.warning("rejected WebSocket from origin %r", origin)
        await _close(websocket, CLOSE_FORBIDDEN)
        return
    await websocket.accept()
    try:
        message = await asyncio.wait_for(websocket.receive(), settings.hello_timeout_s)
    except TimeoutError:
        await _close(websocket, CLOSE_UNAUTHORIZED, "hello required")
        return
    if message["type"] == "websocket.disconnect":
        return
    if message.get("text") is None:
        await _close(websocket, CLOSE_BAD_REQUEST, "text frame required")
        return
    try:
        hello = parse_client_event(message["text"])
    except ValidationError:
        await _close(websocket, CLOSE_BAD_REQUEST, "malformed hello")
        return
    if (not isinstance(hello, Hello)
            or not token_matches(hello.token, app.state.tokens.current())
            or not extension_id_matches(hello.extension_id, origin)):
        log.warning("unauthorized hello from %r", origin)
        await _close(websocket, CLOSE_UNAUTHORIZED, "unauthorized")
        return

    sessions = app.state.sessions
    capabilities = app.state.capabilities
    session = sessions.open(websocket, capabilities, client=hello, config=getattr(app.state, "config", None))
    try:
        await session.send(Welcome(session_id=session.id, server_version=__version__,
                                   profile=capabilities.profile.value, capabilities=capabilities.describe(),
                                   page_budget=page_budget(capabilities),
                                   conversation_id=session.conversation.id,
                                   conversation_turns=session.conversation.answered))
        await session.run()
    finally:
        sessions.discard(session)
