"""FastAPI app factory. `clicky serve` runs `uvicorn.run("clicky.main:create_app", factory=True, ...)`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from clicky import __version__
from clicky.api import health, ws
from clicky.brain.prompts import system_prompt
from clicky.capabilities.registry import Capabilities, build_capabilities
from clicky.config import Settings, get_settings
from clicky.logging import configure_logging, get_redaction_filter
from clicky.security import TokenStore
from clicky.session.config_service import ConfigService
from clicky.session.registry import SessionRegistry
from clicky.ui_settings import load_ui_settings, with_ui_profile

log = logging.getLogger("clicky")

SECRET_FIELDS = ("anthropic_api_key", "openai_api_key", "deepgram_api_key", "elevenlabs_api_key")
TOKEN_WATCH_S = 2.0         # how often a running backend checks whether `clicky token rotate` issued a new token


def secrets_of(settings: Settings, token: str | None = None) -> list[str | None]:
    """Values the log redaction filter must mask."""
    return [token, *(settings.secret_value(name) for name in SECRET_FIELDS)]


async def _warm_up(capabilities: Capabilities) -> None:
    """Brain first (the GPU model, with its system prompt pre-filled), then the speech models, so the first voice
    turn pays no load time."""
    await capabilities.warm_up(system_prompt)
    await capabilities.warm_up_speech()


async def _watch_token(app: FastAPI) -> None:
    """`clicky token rotate` on a running backend: the new token is redacted from logs and every session made
    with the old one is closed with 4401 (the extension then asks to be re-paired)."""
    tokens: TokenStore = app.state.tokens
    known = tokens.current()
    while True:
        await asyncio.sleep(TOKEN_WATCH_S)
        token = tokens.current()
        if token == known:
            continue
        known = token
        get_redaction_filter().add_secret(token)
        closed = await app.state.sessions.revoke_all("the pairing token was rotated")
        log.warning("pairing token rotated: closed %d session(s); re-pair the extension with the new token", closed)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.tokens = TokenStore(settings)                        # CLICKY_HOME/.clicky/auth_token, created once
    configure_logging(settings.log_level, secrets_of(settings, app.state.tokens.current()))   # root + uvicorn
    ui = load_ui_settings(settings)                                # the options page's choices
    effective = with_ui_profile(settings, ui)
    app.state.capabilities = await build_capabilities(effective, ui=ui)   # health-checked chains, lazy imports
    app.state.sessions = SessionRegistry()
    app.state.config = ConfigService(app.state.capabilities, app.state.sessions.broadcast)
    warmup = asyncio.create_task(_warm_up(app.state.capabilities), name="warm-up")
    watcher = asyncio.create_task(_watch_token(app), name="token-watch") if app.state.tokens.rotatable else None
    log.info("ClickyTabs %s on ws://%s:%s/v1/ws (profile=%s, home=%s)",
             __version__, settings.host, settings.port, effective.profile.value, settings.home)
    try:
        yield
    finally:
        for task in (warmup, watcher):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await app.state.sessions.close_all()                       # cancel turns, kill automation workers
        await app.state.capabilities.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="ClickyTabs Orchestrator", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)   # DNS-rebinding guard
    app.include_router(health.router)
    app.include_router(ws.router)
    return app
