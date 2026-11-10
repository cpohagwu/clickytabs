"""Live sessions, so server shutdown can cancel their turns and close their sockets, plus the conversations they
continue: conversations outlive sockets, so a reconnect or a service-worker restart resumes one."""

from __future__ import annotations

import asyncio

from fastapi import WebSocket

from clicky.brain.history import ConversationStore
from clicky.capabilities.registry import Capabilities
from clicky.protocol.events import Event, Hello
from clicky.session.config_service import ConfigService
from clicky.session.session import Session


class SessionRegistry:
    def __init__(self, conversations: ConversationStore | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        self.conversations = conversations if conversations is not None else ConversationStore()

    def open(self, websocket: WebSocket, capabilities: Capabilities, *, client: Hello,
             config: ConfigService | None = None) -> Session:
        session = Session(websocket, capabilities, client, self.conversations, config=config)
        self._sessions[session.id] = session
        return session

    def discard(self, session: Session) -> None:
        self._sessions.pop(session.id, None)

    def __len__(self) -> int:
        return len(self._sessions)

    def broadcast(self, event: Event) -> None:
        for session in list(self._sessions.values()):
            session.emit(event)

    async def revoke_all(self, reason: str) -> int:
        """Close every session with 4401 (the pairing token changed). Returns how many were open."""
        sessions = list(self._sessions.values())
        await asyncio.gather(*(s.close(code=4401, reason=reason) for s in sessions), return_exceptions=True)
        return len(sessions)

    async def close_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(*(s.shutdown() for s in sessions), return_exceptions=True)
        self.conversations.close()
