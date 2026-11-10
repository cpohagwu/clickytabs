"""The options page's runtime configuration (config.get / config.set) and live pairing-token rotation."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from clicky import main
from clicky.capabilities.kinds import Capability, Profile
from clicky.capabilities.registry import build_capabilities
from clicky.logging import get_redaction_filter
from clicky.main import create_app
from clicky.protocol.events import ProviderChoiceWire
from clicky.ui_settings import UiSettings, load_ui_settings, ui_settings_path
from tests.conftest import ORIGIN, hello, make_settings

UNREACHABLE_OLLAMA = {"host": "http://127.0.0.1:9"}     # no test may touch a real Ollama


def _connect(client: TestClient, token: str | None = None):
    ws = client.websocket_connect("/v1/ws", headers={"origin": ORIGIN})
    return ws, (hello(token) if token else hello())


def _request(ws: Any, event: dict[str, Any]) -> dict[str, Any]:
    ws.send_json(event)
    while True:
        message = ws.receive_json()
        if message["type"] == "config.state" and message.get("request_id") == event["request_id"]:
            return message
        if message["type"] == "error":
            return message


@pytest.fixture
def app_client() -> TestClient:
    with TestClient(create_app(make_settings(ollama=UNREACHABLE_OLLAMA))) as test_client:
        yield test_client


def test_config_get_lists_capabilities_providers_and_voices(app_client: TestClient) -> None:
    ws_ctx, first = _connect(app_client)
    with ws_ctx as ws:
        ws.send_json(first)
        assert ws.receive_json()["type"] == "welcome"
        state = _request(ws, {"type": "config.get", "request_id": "get-00000001"})
    assert state["profile"] == "fake" and state["profile_pinned"] is False
    assert state["profiles"] == ["local", "cloud", "hybrid", "fake"]
    caps = {c["capability"]: c for c in state["capabilities"]}
    assert set(caps) == {c.value for c in Capability}
    tts = caps["tts"]
    assert tts["voice_key"] == "voice"
    assert [v["id"] for v in tts["voices"]] == ["fake-bright", "fake-calm"]
    names = {p["name"]: p for p in tts["providers"]}
    assert {"elevenlabs", "openai", "kokoro", "ollama_audio", "fake"} <= set(names)
    assert names["elevenlabs"]["available"] is False and "ELEVENLABS_API_KEY" in names["elevenlabs"]["reason"]
    assert state["page_budget"] is not None


def test_config_set_switches_a_voice_and_persists_it(app_client: TestClient) -> None:
    ws_ctx, first = _connect(app_client)
    with ws_ctx as ws:
        ws.send_json(first)
        ws.receive_json()
        state = _request(ws, {"type": "config.set", "request_id": "set-00000001", "capability": "tts",
                              "choice": {"provider": "fake", "options": {"voice": "fake-calm"}}})
        assert state["error"] is None, state["error"]
        tts = next(c for c in state["capabilities"] if c["capability"] == "tts")
        assert tts["choice"] == {"provider": "fake", "model": None, "options": {"voice": "fake-calm"}}
        assert tts["status"]["ok"] is True
        caps = app_client.app.state.capabilities
        assert caps.provider(Capability.TTS).options.voice == "fake-calm"
        stored = json.loads(ui_settings_path(caps.settings).read_text(encoding="utf-8"))
        assert stored["capabilities"]["fake"]["tts"]["options"] == {"voice": "fake-calm"}

        reset = _request(ws, {"type": "config.set", "request_id": "set-00000002", "capability": "tts", "reset": True})
        assert next(c for c in reset["capabilities"] if c["capability"] == "tts")["choice"] is None
        assert caps.provider(Capability.TTS).options.voice == "fake-bright"


@pytest.mark.parametrize(("choice", "fragment"), [
    ({"provider": "nope"}, "unknown provider"),
    ({"provider": "fake", "options": {"voice": "nobody"}}, "invalid options"),
])
def test_config_set_refuses_bad_choices_and_changes_nothing(app_client: TestClient, choice: dict[str, Any],
                                                            fragment: str) -> None:
    ws_ctx, first = _connect(app_client)
    with ws_ctx as ws:
        ws.send_json(first)
        ws.receive_json()
        state = _request(ws, {"type": "config.set", "request_id": "bad-00000001", "capability": "tts",
                              "choice": choice})
    assert fragment in (state["error"] or "")
    assert not ui_settings_path(app_client.app.state.capabilities.settings).exists()


def test_the_socket_cannot_set_hosts_or_urls(app_client: TestClient) -> None:
    """base_url / host options would send the provider's API key elsewhere: only clicky.toml may set them."""
    ws_ctx, first = _connect(app_client)
    with ws_ctx as ws:
        ws.send_json(first)
        ws.receive_json()
        reply = _request(ws, {"type": "config.set", "request_id": "url-00000001", "capability": "tts",
                              "choice": {"provider": "elevenlabs", "options": {"base_url": "https://evil.example"}}})
    assert reply["type"] == "error" and reply["code"] == "bad_request"
    with pytest.raises(ValueError):
        ProviderChoiceWire(provider="openai", options={"base_url": "https://evil.example"})


def test_profile_switch_is_refused_when_the_environment_pins_it(app_client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("CLICKY_PROFILE", "fake")
    ws_ctx, first = _connect(app_client)
    with ws_ctx as ws:
        ws.send_json(first)
        ws.receive_json()
        state = _request(ws, {"type": "config.set", "request_id": "pin-00000001", "profile": "cloud"})
    assert state["profile_pinned"] is True and "CLICKY_PROFILE" in state["error"]
    assert state["profile"] == "fake"


def test_profile_switch_applies_and_the_next_start_keeps_it(app_client: TestClient) -> None:
    ws_ctx, first = _connect(app_client)
    with ws_ctx as ws:
        ws.send_json(first)
        ws.receive_json()
        state = _request(ws, {"type": "config.set", "request_id": "pro-00000001", "profile": "cloud"})
    assert state["error"] is None and state["profile"] == "cloud"
    brain = next(c for c in state["capabilities"] if c["capability"] == "brain")
    assert brain["status"]["provider"] == "anthropic" and brain["status"]["ok"] is False   # no key in tests
    assert load_ui_settings(app_client.app.state.capabilities.settings).profile is Profile.CLOUD
    with TestClient(create_app(make_settings(ollama=UNREACHABLE_OLLAMA))) as again:
        assert again.app.state.capabilities.profile is Profile.CLOUD
        assert again.get("/v1/info").json()["profile"] == "cloud"


async def test_reconfigure_rebuilds_only_what_changed() -> None:
    caps = await build_capabilities(make_settings(ollama=UNREACHABLE_OLLAMA))
    try:
        brain, tts = caps.provider(Capability.BRAIN), caps.provider(Capability.TTS)
        ui = UiSettings(capabilities={Profile.FAKE: {
            Capability.TTS: ProviderChoiceWire(provider="fake", options={"voice": "fake-calm"})}})
        changed = await caps.reconfigure(caps.settings, ui)
        assert changed == [Capability.TTS]
        assert caps.provider(Capability.BRAIN) is brain                  # still loaded, untouched
        assert caps.provider(Capability.TTS) is not tts
        assert tts in caps._retired                                     # closed after the grace period
        assert caps.get(Capability.TTS).source.startswith("options page")
    finally:
        await caps.aclose()


def test_rotating_the_token_closes_sessions_and_only_the_new_token_works(monkeypatch) -> None:
    monkeypatch.setattr(main, "TOKEN_WATCH_S", 0.05)
    settings = make_settings(auth_token=None)
    old = settings.resolved_token()
    with TestClient(create_app(settings)) as test_client:
        with test_client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
            ws.send_json(hello(old))
            assert ws.receive_json()["type"] == "welcome"
            time.sleep(0.05)
            new = settings.rotate_token()
            with pytest.raises(WebSocketDisconnect) as closed:
                for _ in range(100):
                    ws.receive_json()
            assert closed.value.code == 4401
        with pytest.raises(WebSocketDisconnect) as refused:
            with test_client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
                ws.send_json(hello(old))
                ws.receive_json()
        assert refused.value.code == 4401
        with test_client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
            ws.send_json(hello(new))
            assert ws.receive_json()["type"] == "welcome"
    assert new not in get_redaction_filter().redact(f"token={new}")
