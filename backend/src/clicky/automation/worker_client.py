"""Spawn and talk to the browser-use automation worker (JSON lines over stdio).

Why not asyncio subprocesses: uvicorn --reload on Windows runs a SelectorEventLoop, which cannot spawn asyncio
subprocesses. A Popen plus two reader threads works on every event loop.

Safety:
- the pipes are UTF-8 (PYTHONUTF8=1 + encoding="utf-8"), so non-ASCII page titles and goals survive;
- the worker routes stray prints to stderr (see clicky_worker.__main__), and stderr is drained to a log file so a full
  pipe can never block it;
- on Windows the child is placed in a Job Object with KILL_ON_JOB_CLOSE, so a headed Chromium holding the user's
  session cookies dies with the backend, even if the backend crashes; elsewhere close() tree-kills via psutil;
- API keys are passed through the child environment only, never on the command line or over IPC.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Mapping, Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil
from pydantic import BaseModel, ValidationError

from clicky.logging import get_redaction_filter
from clicky.protocol.worker_ipc import WorkerEvent, WorkerShutdown, parse_worker_event

log = logging.getLogger(__name__)

WORKER_PROJECT = Path(__file__).resolve().parents[3] / "automation-worker"   # backend/automation-worker
WORKER_SRC = WORKER_PROJECT / "src"
WORKER_PYTHON = WORKER_PROJECT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
_EOF = object()
# Never inherited by the worker: it gets only the one key its LLM needs (AgentLLMConfigOut.env).
SECRET_ENV = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY", "CLICKY_AUTH_TOKEN")
# browser-use phones home unless told not to: telemetry, cloud sync, a PyPI version check, extension downloads.
QUIET_ENV = {"ANONYMIZED_TELEMETRY": "false", "BROWSER_USE_CLOUD_SYNC": "false", "BROWSER_USE_VERSION_CHECK": "false",
             "BROWSER_USE_DISABLE_EXTENSIONS": "1"}


def worker_installed() -> bool:
    """`uv sync` has been run in backend/automation-worker (browser-use is installed there)."""
    return WORKER_PYTHON.is_file()


def worker_command(*, stub: bool = False) -> list[str]:
    """Live: the worker's own environment (its python directly when synced). Stub: this interpreter, no browser-use."""
    if stub:
        return [sys.executable, "-m", "clicky_worker", "--stub"]
    if worker_installed():
        return [str(WORKER_PYTHON), "-m", "clicky_worker"]
    return ["uv", "run", "--project", str(WORKER_PROJECT), "--quiet", "python", "-m", "clicky_worker"]


# --------------------------------------------------------------------------------------------- Windows job object


class _WindowsJob:
    """A Job Object that kills every process in it when its last handle closes (i.e. when we exit)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32 = kernel32
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        job_object_extended_limit_information = 9
        if not kernel32.SetInformationJobObject(self.handle, job_object_extended_limit_information,
                                                ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, pid: int) -> None:
        process_set_quota, process_terminate = 0x0100, 0x0001
        handle = self._kernel32.OpenProcess(process_set_quota | process_terminate, False, pid)
        if not handle:
            raise OSError(self._ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not self._kernel32.AssignProcessToJobObject(self.handle, handle):
                raise OSError(self._ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            self._kernel32.CloseHandle(handle)

    def close(self) -> None:
        if self.handle:
            self._kernel32.CloseHandle(self.handle)     # kills anything still in the job
            self.handle = None


# --------------------------------------------------------------------------------------------------- client


class WorkerClient:
    def __init__(self, process: subprocess.Popen[str], loop: asyncio.AbstractEventLoop,
                 job: _WindowsJob | None, log_path: Path | None) -> None:
        self.process = process
        self._loop = loop
        self._job = job
        self._events: asyncio.Queue[object] = asyncio.Queue()
        self._write_lock = threading.Lock()
        self._closed = False
        self._stderr_log = _stderr_logger(log_path) if log_path else None
        threading.Thread(target=self._pump_stdout, name="worker-stdout", daemon=True).start()
        threading.Thread(target=self._pump_stderr, name="worker-stderr", daemon=True).start()

    @classmethod
    async def spawn(cls, *, stub: bool = False, env: Mapping[str, str] | None = None,
                    pythonpath: Sequence[Path] = (), log_path: Path | None = None,
                    command: Sequence[str] | None = None) -> WorkerClient:
        inherited = {key: value for key, value in os.environ.items() if key.upper() not in SECRET_ENV}
        child_env = {**inherited, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **QUIET_ENV, **(env or {})}
        if pythonpath:
            child_env["PYTHONPATH"] = os.pathsep.join([*map(str, pythonpath), child_env.get("PYTHONPATH", "")])
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        process = subprocess.Popen(
            list(command or worker_command(stub=stub)), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, encoding="utf-8", errors="replace", bufsize=1, env=child_env,
            creationflags=creationflags, start_new_session=sys.platform != "win32")
        job = None
        if sys.platform == "win32":
            try:
                job = _WindowsJob()
                job.assign(process.pid)
            except OSError as exc:      # e.g. already inside a job that forbids nesting; close() still tree-kills
                log.warning("could not put the worker in a Job Object: %s", exc)
                job = None
        return cls(process, asyncio.get_running_loop(), job, log_path)

    # ------------------------------------------------------------------------------------------------ threads

    def _pump_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event: object = parse_worker_event(line)
            except ValidationError as exc:
                log.warning("worker sent an invalid IPC line (%s): %.200s", exc.error_count(), line)
                continue
            self._loop.call_soon_threadsafe(self._events.put_nowait, event)
        self._loop.call_soon_threadsafe(self._events.put_nowait, _EOF)

    def _pump_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            if self._stderr_log is not None:
                self._stderr_log.info(line.rstrip())
            else:
                log.debug("worker: %s", line.rstrip())

    # ------------------------------------------------------------------------------------------------ API

    async def send(self, command: BaseModel) -> None:
        if self._closed or self.process.stdin is None:
            raise RuntimeError("worker is closed")
        line = command.model_dump_json() + "\n"
        await asyncio.to_thread(self._write, line)

    def _write(self, line: str) -> None:
        assert self.process.stdin is not None
        with self._write_lock:
            self.process.stdin.write(line)
            self.process.stdin.flush()

    async def next_event(self, timeout: float | None = None) -> WorkerEvent | None:
        """The next event, or None once the worker's stdout has closed."""
        item = await asyncio.wait_for(self._events.get(), timeout)
        if item is _EOF:
            self._events.put_nowait(_EOF)      # stay at EOF for later callers
            return None
        return item  # type: ignore[return-value]

    async def events(self) -> AsyncIterator[WorkerEvent]:
        while (event := await self.next_event()) is not None:
            yield event

    async def close(self, *, grace_s: float = 3.0) -> int | None:
        """Ask the worker to shut down, then kill its whole process tree if it lingers."""
        if self._closed:
            return self.process.returncode
        with contextlib.suppress(Exception):
            await self.send(WorkerShutdown())
        self._closed = True
        with contextlib.suppress(Exception):
            if self.process.stdin is not None:
                self.process.stdin.close()
        try:
            return await asyncio.to_thread(self.process.wait, grace_s)
        except subprocess.TimeoutExpired:
            _kill_tree(self.process.pid)
            return await asyncio.to_thread(self.process.wait, 5.0)
        finally:
            if self._job is not None:
                self._job.close()


def _kill_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for child in parent.children(recursive=True):
        with contextlib.suppress(psutil.Error):
            child.kill()
    with contextlib.suppress(psutil.Error):
        parent.kill()


def _stderr_logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"clicky.worker.stderr.{path}")
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        handler.addFilter(get_redaction_filter())     # the token and API keys, as in the main log
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
