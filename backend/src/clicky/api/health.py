"""Unauthenticated endpoints. Deliberately minimal: capability details are only sent in the authenticated welcome."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from clicky import __version__

router = APIRouter()

WS_PATH = "/v1/ws"


class Healthz(BaseModel):
    ok: bool
    version: str


class Info(BaseModel):
    name: str
    version: str
    ws_path: str
    profile: str


@router.get("/healthz")
async def healthz() -> Healthz:
    return Healthz(ok=True, version=__version__)


@router.get("/v1/info")
async def info(request: Request) -> Info:
    return Info(name="clickytabs", version=__version__, ws_path=WS_PATH,
                profile=request.app.state.capabilities.profile.value)
