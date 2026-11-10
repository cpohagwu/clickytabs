"""Worker entry point: `python -m clicky_worker [--stub]`.

The very first thing it does is take fd 1 for IPC and point fd 1 at stderr, so any later print() - ours or a
dependency's - lands in the (drained) stderr log instead of corrupting the JSON-lines stream.

Commands are read on a thread and handled on an asyncio loop, so `task.cancel` and `confirm.response` reach a task
while it runs. One task at a time. `--stub` never imports browser-use: it answers pings and runs the scripted (fake)
task, which is what the backend's tests and the fake profile use.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading


def _claim_stdout() -> object:
    ipc_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = os.fdopen(1, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)
    return os.fdopen(ipc_fd, "w", encoding="utf-8", buffering=1, newline="\n")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    ipc_out = _claim_stdout()
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    return asyncio.run(_serve(ipc_out, stub="--stub" in args))


async def _serve(ipc_out: object, *, stub: bool) -> int:
    from clicky_worker import __version__
    from clicky_worker.hitl import Confirmations
    from clicky_worker.ipc import (
        WORKER_COMMAND_ADAPTER,
        WorkerConfirmResponse,
        WorkerPing,
        WorkerPong,
        WorkerReady,
        WorkerShutdown,
        WorkerTaskCancel,
        WorkerTaskDone,
        WorkerTaskError,
        WorkerTaskStart,
    )

    loop = asyncio.get_running_loop()
    write_lock = threading.Lock()

    def emit(message) -> None:  # type: ignore[no-untyped-def]
        with write_lock:
            ipc_out.write(message.model_dump_json() + "\n")   # type: ignore[attr-defined]
            ipc_out.flush()                                     # type: ignore[attr-defined]

    lines: asyncio.Queue[str | None] = asyncio.Queue()

    def read_stdin() -> None:
        for raw in sys.stdin:
            loop.call_soon_threadsafe(lines.put_nowait, raw)
        loop.call_soon_threadsafe(lines.put_nowait, None)

    threading.Thread(target=read_stdin, name="ipc-stdin", daemon=True).start()
    emit(WorkerReady(version=__version__, mode="stub" if stub else "live"))

    running: dict[str, tuple[asyncio.Task[None], asyncio.Event, Confirmations]] = {}

    async def run(start: WorkerTaskStart, stop: asyncio.Event, confirmations: Confirmations) -> None:
        from clicky_worker.runner import run_live, run_scripted

        try:
            if start.llm.provider == "fake":
                done = await run_scripted(start, emit, confirmations, stop)
            elif stub:
                done = WorkerTaskDone(task_id=start.task_id, success=False,
                                      summary="This worker runs in stub mode and can't drive a browser.")
            else:
                done = await run_live(start, emit, confirmations, stop)
            emit(done)
        except Exception as exc:          # never leave the backend waiting
            emit(WorkerTaskError(task_id=start.task_id, message=f"{type(exc).__name__}: {exc}"[:500]))
        finally:
            running.pop(start.task_id, None)

    while (raw := await lines.get()) is not None:
        raw = raw.strip()
        if not raw:
            continue
        try:
            command = WORKER_COMMAND_ADAPTER.validate_json(raw)
        except Exception as exc:
            emit(WorkerTaskError(task_id=None, message=f"invalid command: {exc}"[:500]))
            continue
        if isinstance(command, WorkerPing):
            emit(WorkerPong(nonce=command.nonce, payload=command.payload))
        elif isinstance(command, WorkerShutdown):
            break
        elif isinstance(command, WorkerTaskStart):
            if running:
                emit(WorkerTaskError(task_id=command.task_id, message="a task is already running"))
                continue
            stop = asyncio.Event()
            confirmations = Confirmations(emit, command.limits.confirm_timeout_s)
            task = asyncio.create_task(run(command, stop, confirmations), name=f"task-{command.task_id}")
            running[command.task_id] = (task, stop, confirmations)
        elif isinstance(command, WorkerTaskCancel):
            if (entry := running.get(command.task_id)) is not None:
                entry[1].set()
                entry[2].cancel_all()
        elif isinstance(command, WorkerConfirmResponse):
            if (entry := running.get(command.task_id)) is not None:
                entry[2].resolve(command)

    # Shutdown (or stdin closed): stop what is running and give it a moment to close its browser.
    for _task, stop, confirmations in list(running.values()):
        stop.set()
        confirmations.cancel_all()
    if running:
        await asyncio.wait([entry[0] for entry in running.values()], timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
