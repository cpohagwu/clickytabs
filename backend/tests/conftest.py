"""Hermetic test setup: a temp CLICKY_HOME, no provider keys, no OLLAMA_HOST, fake providers."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from clicky.config import Settings
from clicky.main import create_app

DEV_ID = "fhgliaahcafninaodeelekpdemjkifmf"
ORIGIN = f"chrome-extension://{DEV_ID}"
TOKEN = "test-token-0123456789abcdef"
SCRUBBED = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY",
            "OLLAMA_HOST")


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch) -> Iterator[None]:
    for name in list(os.environ):
        if name.upper().startswith("CLICKY_") or name.upper() in SCRUBBED:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLICKY_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = dict(profile="fake", auth_token=TOKEN, hello_timeout_s=0.2,
                                     trusted_hosts=["testserver", "127.0.0.1", "localhost"],
                                     allowed_extension_ids=[DEV_ID])
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def hello(token: str = TOKEN, extension_id: str = DEV_ID) -> dict[str, object]:
    return {"type": "hello", "token": token, "client_version": "test", "extension_id": extension_id}
