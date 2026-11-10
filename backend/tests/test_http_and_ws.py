"""HTTP endpoints, the /v1/ws handshake matrix, and text turns end to end through the real app (fake providers)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from clicky import __version__
from clicky.capabilities.kinds import Capability
from tests.conftest import ORIGIN, hello

OTHER_ID = "a" * 32

# ------------------------------------------------------------------------------------------------------- HTTP


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"ok": True, "version": __version__}


def test_info_is_minimal(client: TestClient) -> None:
    assert set(client.get("/v1/info").json()) == {"name", "version", "ws_path", "profile"}


def test_trusted_host_rejects_foreign_host(client: TestClient) -> None:
    assert client.get("/healthz", headers={"host": "evil.example"}).status_code == 400


def test_no_openapi_docs(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


# -------------------------------------------------------------------------------------------------- handshake


def _closed_with(client: TestClient, code: int, send: Callable[[Any], None], origin: str = ORIGIN) -> None:
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect("/v1/ws", headers={"origin": origin}) as ws:
            send(ws)
            ws.receive_json()
    assert info.value.code == code


def test_good_hello_gets_welcome(client: TestClient) -> None:
    with client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_json(hello())
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome" and welcome["profile"] == "fake"
        assert {c["capability"] for c in welcome["capabilities"]} == {c.value for c in Capability}
        ws.send_json({"type": "ping", "t": 1.5})
        assert ws.receive_json() == {"type": "pong", "t": 1.5}


def test_bad_token(client: TestClient) -> None:
    _closed_with(client, 4401, lambda ws: ws.send_json(hello(token="wrong")))


def test_foreign_origin_rejected_before_accept(client: TestClient) -> None:
    _closed_with(client, 4403, lambda ws: None, origin="https://evil.example")


def test_unlisted_extension_rejected(client: TestClient) -> None:
    _closed_with(client, 4403, lambda ws: None, origin=f"chrome-extension://{OTHER_ID}")


def test_binary_first_frame(client: TestClient) -> None:
    _closed_with(client, 4400, lambda ws: ws.send_bytes(b"\x00\x01"))


def test_malformed_hello(client: TestClient) -> None:
    _closed_with(client, 4400, lambda ws: ws.send_text("{not json"))


def test_hello_timeout(client: TestClient) -> None:
    _closed_with(client, 4401, lambda ws: None)


def test_first_frame_must_be_hello(client: TestClient) -> None:
    _closed_with(client, 4401, lambda ws: ws.send_json({"type": "ping"}))


def test_extension_id_must_match_origin(client: TestClient, settings) -> None:
    settings.allowed_extension_ids.append(OTHER_ID)
    _closed_with(client, 4401, lambda ws: ws.send_json(hello(extension_id=OTHER_ID)))


def test_bad_frames_after_handshake_keep_the_connection(client: TestClient) -> None:
    with client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_json(hello())
        ws.receive_json()
        ws.send_text("{nope")
        assert ws.receive_json()["code"] == "bad_request"
        ws.send_bytes(b"x")
        assert ws.receive_json()["code"] == "bad_request"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


# ------------------------------------------------------------------------------------------------- text turns


def _until_done(ws: Any, turn_id: str) -> list[dict[str, Any]]:
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] == "turn.done" and event["turn_id"] == turn_id:
            return events


@pytest.fixture
def ws(client: TestClient):
    with client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as socket:
        socket.send_json(hello())
        assert socket.receive_json()["type"] == "welcome"
        yield socket


def test_text_turn_streams_deltas_then_done(ws) -> None:
    ws.send_json({"type": "turn.start", "turn_id": "t1", "mode": "text", "text": "hello there"})
    events = _until_done(ws, "t1")
    assert events[0] == {"type": "turn.state", "turn_id": "t1", "state": "thinking"}
    assert "".join(e["delta"] for e in events if e["type"] == "assistant.text.delta").startswith(
        "You said: hello there.")
    assert events[-1]["reason"] == "end"
    assert sum(e["type"] == "turn.done" for e in events) == 1


def test_cancel_stops_the_stream_and_closes_the_provider(client: TestClient, ws) -> None:
    ws.send_json({"type": "turn.start", "turn_id": "slow", "mode": "text", "text": "[slow] count"})
    assert ws.receive_json()["type"] == "turn.state"
    assert ws.receive_json()["type"] == "assistant.text.delta"
    ws.send_json({"type": "turn.cancel", "turn_id": "slow"})
    events = _until_done(ws, "slow")
    assert events[-1]["reason"] == "cancelled"
    assert sum(e["type"] == "assistant.text.delta" for e in events) < 60
    brain = client.app.state.capabilities.provider(Capability.BRAIN)
    assert brain.stats.cancelled == 1 and brain.stats.closed == brain.stats.started


def test_new_turn_barges_in(ws) -> None:
    ws.send_json({"type": "turn.start", "turn_id": "a", "mode": "text", "text": "[slow] one"})
    assert ws.receive_json()["type"] == "turn.state"
    ws.send_json({"type": "turn.start", "turn_id": "b", "mode": "text", "text": "two"})
    events = _until_done(ws, "b")
    done = [e for e in events if e["type"] == "turn.done"]
    assert done[0] == {"type": "turn.done", "turn_id": "a", "reason": "cancelled", "detail": None, "metrics": None}
    assert done[-1]["turn_id"] == "b" and done[-1]["reason"] == "end"


def test_error_refusal_and_voice_paths(ws) -> None:
    ws.send_json({"type": "turn.start", "turn_id": "e", "mode": "text", "text": "[error] boom"})
    errored = _until_done(ws, "e")
    ws.send_json({"type": "turn.start", "turn_id": "r", "mode": "text", "text": "[refusal] no"})
    refused = _until_done(ws, "r")
    ws.send_json({"type": "turn.start", "turn_id": "v", "mode": "voice"})
    ws.send_json({"type": "audio.end", "turn_id": "v"})          # key released without a sound: no STT call
    voice = _until_done(ws, "v")
    assert any(e["type"] == "error" for e in errored) and errored[-1]["reason"] == "error"
    assert refused[-1]["reason"] == "refusal"
    assert any(e.get("code") == "no_speech" for e in voice) and voice[-1]["reason"] == "error"
