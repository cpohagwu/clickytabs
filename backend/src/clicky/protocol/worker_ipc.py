"""JSON-lines IPC between the orchestrator and the automation worker (one UTF-8 JSON object per line).

The worker runs in its own uv environment (browser-use pins pydantic==2.13.5), so it cannot import this package.
`automation-worker/src/clicky_worker/ipc.py` mirrors these models and tests/test_worker_ipc.py asserts both
produce identical JSON Schemas. API keys never travel over IPC - only via the child process environment.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class Msg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentLLMConfig(Msg):
    provider: Literal["anthropic", "openai_compat", "ollama", "fake"]
    model: str
    base_url: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class BrowserConfig(Msg):
    headless: bool = False
    user_agent: str | None = None
    locale: str | None = None
    timezone: str | None = None
    viewport: tuple[int, int] | None = None
    # browser-use URL patterns ("https://*.shop.example"); navigation anywhere else is blocked
    allowed_domains: list[str] = Field(default_factory=list)
    executable_path: str | None = None          # None: browser-use finds Chrome / Chromium
    args: list[str] = Field(default_factory=list)   # extra Chromium flags, from the backend's settings only


class TaskLimits(Msg):
    max_steps: int = 40
    step_timeout_s: float = 300.0
    confirm_timeout_s: float = 120.0            # how long ask_user waits for the user before telling the agent no


# ------------------------------------------------------------------------------------- orchestrator -> worker


class WorkerPing(Msg):
    type: Literal["ping"] = "ping"
    nonce: str
    payload: str = ""


class WorkerTaskStart(Msg):
    type: Literal["task.start"] = "task.start"
    task_id: str
    goal: str
    start_url: str | None = None
    storage_state: dict[str, Any] | None = None     # Playwright-style {"cookies": [...], "origins": [...]}
    llm: AgentLLMConfig
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    limits: TaskLimits = Field(default_factory=TaskLimits)


class WorkerTaskCancel(Msg):
    type: Literal["task.cancel"] = "task.cancel"
    task_id: str


class WorkerConfirmResponse(Msg):
    type: Literal["confirm.response"] = "confirm.response"
    task_id: str
    request_id: str
    approved: bool
    note: str | None = None


class WorkerShutdown(Msg):
    type: Literal["shutdown"] = "shutdown"


WorkerCommand = Annotated[
    WorkerPing | WorkerTaskStart | WorkerTaskCancel | WorkerConfirmResponse | WorkerShutdown,
    Field(discriminator="type"),
]

# ------------------------------------------------------------------------------------- worker -> orchestrator


class WorkerReady(Msg):
    type: Literal["ready"] = "ready"
    version: str
    mode: Literal["stub", "live"]


class WorkerPong(Msg):
    type: Literal["pong"] = "pong"
    nonce: str
    payload: str = ""


class WorkerTaskStep(Msg):
    type: Literal["task.step"] = "task.step"
    task_id: str
    n: int
    action_text: str
    url: str | None = None
    thumb_b64: str | None = None


class WorkerConfirmRequest(Msg):
    type: Literal["confirm.request"] = "confirm.request"
    task_id: str
    request_id: str
    question: str


class WorkerTaskDone(Msg):
    type: Literal["task.done"] = "task.done"
    task_id: str
    success: bool
    summary: str
    final_url: str | None = None


class WorkerTaskError(Msg):
    type: Literal["task.error"] = "task.error"
    task_id: str | None = None
    message: str


class WorkerLog(Msg):
    type: Literal["log"] = "log"
    level: Literal["debug", "info", "warning", "error"] = "info"
    message: str


WorkerEvent = Annotated[
    WorkerReady | WorkerPong | WorkerTaskStep | WorkerConfirmRequest | WorkerTaskDone | WorkerTaskError | WorkerLog,
    Field(discriminator="type"),
]

WORKER_COMMAND_ADAPTER: TypeAdapter[WorkerCommand] = TypeAdapter(WorkerCommand)
WORKER_EVENT_ADAPTER: TypeAdapter[WorkerEvent] = TypeAdapter(WorkerEvent)


def parse_worker_event(line: str | bytes) -> WorkerEvent:
    return WORKER_EVENT_ADAPTER.validate_json(line)


def dump_command(cmd: BaseModel) -> str:
    """One JSON line (no trailing newline). Non-ASCII stays UTF-8; the pipe must be opened with encoding='utf-8'."""
    return cmd.model_dump_json()
