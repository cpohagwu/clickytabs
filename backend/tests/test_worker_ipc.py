"""Automation worker IPC: the stub worker round-trips UTF-8 JSON lines, and both model copies stay identical."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from pydantic import TypeAdapter

from clicky.automation.worker_client import WorkerClient
from clicky.protocol import worker_ipc
from clicky.protocol.worker_ipc import WorkerPing, WorkerPong, WorkerReady

WORKER_SRC = Path(__file__).resolve().parents[1] / "automation-worker" / "src"


def _load_worker_ipc():
    spec = importlib.util.spec_from_file_location("clicky_worker_ipc_mirror", WORKER_SRC / "clicky_worker" / "ipc.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_worker_ipc_mirror_matches_backend_schema() -> None:
    mirror = _load_worker_ipc()
    for union in ("WorkerCommand", "WorkerEvent"):
        ours = TypeAdapter(getattr(worker_ipc, union)).json_schema()
        theirs = TypeAdapter(getattr(mirror, union)).json_schema()
        assert ours == theirs, f"{union} drifted between backend and automation-worker"


async def test_stub_worker_round_trip_with_unicode(tmp_path) -> None:
    payload = "héllo – 你好 🎤"
    client = await WorkerClient.spawn(stub=True, pythonpath=[WORKER_SRC], log_path=tmp_path / "worker.log")
    try:
        ready = await client.next_event(timeout=20)
        assert isinstance(ready, WorkerReady) and ready.mode == "stub"
        await client.send(WorkerPing(nonce="n1", payload=payload))
        pong = await client.next_event(timeout=10)
        assert isinstance(pong, WorkerPong) and pong.nonce == "n1" and pong.payload == payload
    finally:
        code = await client.close()
    assert code == 0
    assert await client.next_event(timeout=5) is None
