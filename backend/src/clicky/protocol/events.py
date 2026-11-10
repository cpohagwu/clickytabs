"""WebSocket wire protocol (/v1/ws, JSON text frames).

Single source of truth: these models -> backend/protocol.schema.json -> extension/src/lib/protocol.gen.ts.
Every event carries a `type` discriminator; visual directives carry a `kind` discriminator.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from clicky.protocol.page import PageBudget, PageContext


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")


TurnStateName = Literal["listening", "transcribing", "thinking", "speaking", "acting", "idle"]
TurnDoneReason = Literal["end", "cancelled", "error", "refusal", "max_tokens"]

# ------------------------------------------------------------------------------------------------- directives


class PointDirective(Event):
    kind: Literal["point"] = "point"
    to: str | None = None               # element mark id ("e17"); preferred
    x: float | None = None              # screenshot px fallback
    y: float | None = None
    label: str | None = None


class MarkDirective(Event):
    kind: Literal["mark"] = "mark"
    to: str
    style: Literal["circle", "box", "underline", "glow"] = "circle"
    label: str | None = None


class ArrowDirective(Event):
    kind: Literal["arrow"] = "arrow"
    source: str                         # element id or "x,y"
    target: str
    label: str | None = None


class PathDirective(Event):
    kind: Literal["path"] = "path"
    points: list[tuple[float, float]]   # screenshot px
    label: str | None = None


class ScrollDirective(Event):
    kind: Literal["scroll"] = "scroll"
    to: str


class ClearDirective(Event):
    kind: Literal["clear"] = "clear"


Directive = Annotated[
    PointDirective | MarkDirective | ArrowDirective | PathDirective | ScrollDirective | ClearDirective,
    Field(discriminator="kind"),
]


class TimedDirective(Event):
    at_char: int = Field(ge=0)          # offset into the segment's spoken text
    directive: Directive


# --------------------------------------------------------------------------------------------- client -> server


CONVERSATION_ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"


class Hello(Event):
    type: Literal["hello"] = "hello"
    token: str
    client_version: str
    extension_id: str
    features: list[str] = Field(default_factory=list)
    # The conversation to continue (kept in chrome.storage.session); unknown ids start a new one with that id
    conversation_id: str | None = Field(default=None, pattern=CONVERSATION_ID_PATTERN)


class Ping(Event):
    type: Literal["ping"] = "ping"
    t: float | None = None


class TurnStart(Event):
    type: Literal["turn.start"] = "turn.start"
    turn_id: str
    mode: Literal["text", "voice"]
    text: str | None = None
    tab_id: int | None = None
    page: PageContext | None = None


class TurnCancel(Event):
    type: Literal["turn.cancel"] = "turn.cancel"
    turn_id: str


class AudioChunkEvent(Event):
    type: Literal["audio.chunk"] = "audio.chunk"
    turn_id: str
    seq: int = Field(ge=0)
    data_b64: str = Field(max_length=1_000_000)   # one MediaRecorder slice (100 ms of opus is ~1 KB)
    mime: str = "audio/webm;codecs=opus"


class AudioEnd(Event):
    type: Literal["audio.end"] = "audio.end"
    turn_id: str


class PlaybackEvent(Event):
    type: Literal["playback.event"] = "playback.event"
    turn_id: str
    seq: int
    state: Literal["started", "ended", "interrupted"]
    at_ms: float


SCENE_ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"
SceneKind = Literal["three", "canvas", "svg", "html"]


class SceneResult(Event):
    """The sandbox ran a scene.render: it drew something (ok), or why not. A failure gets one repair attempt."""

    type: Literal["scene.result"] = "scene.result"
    scene_id: str = Field(pattern=SCENE_ID_PATTERN)
    attempt: int = Field(default=1, ge=1, le=10)
    ok: bool
    error: str | None = Field(default=None, max_length=2000)
    # The tab can't show it (no stage, dismissed while building): no repair, no retry
    unavailable: bool = False


TASK_ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"
SITE_PATTERN = r"^[a-z0-9-]+(\.[a-z0-9-]+)+$"     # a registrable domain, lowercase ("shop.example")


class ConsentResponse(Event):
    """The user's answer in the consent window. `sites` are the registrable domains the task may use, as the
    extension worked them out from the real tab URL and the start URL (the model never decides them)."""

    type: Literal["consent.response"] = "consent.response"
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    decision: Literal["allow_once", "always", "deny"]
    sites: list[Annotated[str, Field(pattern=SITE_PATTERN, max_length=253)]] = Field(default_factory=list)
    tab_url: str | None = Field(default=None, max_length=4096)    # where the task starts when it names no URL

    @field_validator("sites")
    @classmethod
    def _few_sites(cls, sites: list[str]) -> list[str]:
        if len(sites) > 4:                  # the tab's site and the task's (a list keeps the schema simple)
            raise ValueError("at most 4 sites")
        return sites


class CookieRecord(Event):
    name: str
    value: str
    domain: str
    path: str = "/"
    expires: float | None = None        # unix seconds; None = session cookie
    http_only: bool = False
    secure: bool = False
    same_site: Literal["Strict", "Lax", "None"] | None = None
    host_only: bool = False
    partition_key: str | None = None    # top-level site for partitioned (CHIPS) cookies


class OriginStorage(Event):
    origin: str
    local_storage: dict[str, str] = Field(default_factory=dict)


class SessionBundle(Event):
    """The user's login for the task's site, collected by the extension after consent: cookies of the tab's
    registrable domain only, plus the tab origin's localStorage."""

    type: Literal["session.bundle"] = "session.bundle"
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    site: str = Field(pattern=SITE_PATTERN, max_length=253)
    cookies: list[CookieRecord] = Field(default_factory=list, max_length=500)
    origins: list[OriginStorage] = Field(default_factory=list)
    user_agent: str | None = None
    locale: str | None = None
    timezone: str | None = None
    viewport: tuple[int, int] | None = None

    @field_validator("origins")
    @classmethod
    def _few_origins(cls, origins: list[OriginStorage]) -> list[OriginStorage]:
        if len(origins) > 4:
            raise ValueError("at most 4 origins")
        return origins


class TaskConfirmResponse(Event):
    type: Literal["task.confirm.response"] = "task.confirm.response"
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    request_id: str = Field(max_length=64)
    approved: bool
    note: str | None = Field(default=None, max_length=300)


class TaskCancel(Event):
    type: Literal["task.cancel"] = "task.cancel"
    task_id: str = Field(pattern=TASK_ID_PATTERN)


class ConversationReset(Event):
    """Forget the conversation so far; the server answers with conversation.started."""

    type: Literal["conversation.reset"] = "conversation.reset"


REQUEST_ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"
PROVIDER_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
# What the options page may set besides provider and model. Hosts, base URLs and keys stay in clicky.toml / .env:
# the socket can never point a provider (and the API key it sends) at another server.
UI_OPTION_KEYS = frozenset({"voice", "voice_id", "effort", "speed"})


class ProviderChoiceWire(Event):
    """One provider for one capability, as the options page sets it."""

    provider: str = Field(pattern=PROVIDER_NAME_PATTERN)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    options: dict[str, str | float | bool] = Field(default_factory=dict)

    @field_validator("options")
    @classmethod
    def _safe_options(cls, value: dict[str, str | float | bool]) -> dict[str, str | float | bool]:
        unknown = sorted(set(value) - UI_OPTION_KEYS)
        if unknown:
            raise ValueError(f"only {', '.join(sorted(UI_OPTION_KEYS))} can be set here (not {', '.join(unknown)}); "
                             "set other options in clicky.toml")
        if any(isinstance(v, str) and len(v) > 200 for v in value.values()):
            raise ValueError("option values are at most 200 characters")
        return value


class ConfigGet(Event):
    """The options page asks for the profile, every capability's provider and what it could switch to."""

    type: Literal["config.get"] = "config.get"
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)


class ConfigSet(Event):
    """Switch the profile and/or one capability's provider (for the profile then active) without a restart.
    `reset` drops the options page's choice for `capability` (back to clicky.toml or the preset)."""

    type: Literal["config.set"] = "config.set"
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    profile: Literal["local", "cloud", "hybrid"] | None = None
    capability: Literal["brain", "codegen", "agent_llm", "stt", "tts", "summarizer", "embeddings"] | None = None
    choice: ProviderChoiceWire | None = None
    reset: bool = False


ClientEvent = Annotated[
    Hello | Ping | TurnStart | TurnCancel | AudioChunkEvent | AudioEnd | PlaybackEvent | SceneResult
    | ConsentResponse | SessionBundle | TaskConfirmResponse | TaskCancel | ConversationReset | ConfigGet | ConfigSet,
    Field(discriminator="type"),
]

# --------------------------------------------------------------------------------------------- server -> client


class CapabilityStatus(Event):
    capability: str
    provider: str
    model: str | None = None
    kind: Literal["paid", "open", "test"]
    source: Literal["api", "ollama", "huggingface", "local"]
    ok: bool
    reason: str = ""
    hint: str = ""
    chain: list[str] = Field(default_factory=list)   # "provider:model" in fallback order, active first


class Welcome(Event):
    type: Literal["welcome"] = "welcome"
    session_id: str
    server_version: str
    profile: str
    capabilities: list[CapabilityStatus] = Field(default_factory=list)
    page_budget: PageBudget | None = None   # how much page context to capture for the active brain
    conversation_id: str | None = None      # the conversation this session continues (store it for hello)
    conversation_turns: int = 0             # answered turns it already holds


class Pong(Event):
    type: Literal["pong"] = "pong"
    t: float | None = None


class ErrorEvent(Event):
    type: Literal["error"] = "error"
    code: str                           # "bad_request", "busy", "provider_error", "unsupported", ...
    message: str
    turn_id: str | None = None
    retryable: bool = False


class TurnState(Event):
    type: Literal["turn.state"] = "turn.state"
    turn_id: str
    state: TurnStateName


class AssistantTextDelta(Event):
    type: Literal["assistant.text.delta"] = "assistant.text.delta"
    turn_id: str
    delta: str


class TurnDone(Event):
    type: Literal["turn.done"] = "turn.done"
    turn_id: str
    reason: TurnDoneReason
    detail: str | None = None
    # Latency breakdown in ms, e.g. stt_ms, brain_first_token_ms, first_audio_ms (voice: from audio.end)
    metrics: dict[str, float] | None = None


class CapabilityStatusEvent(Event):
    type: Literal["capability.status"] = "capability.status"
    status: CapabilityStatus


class TranscriptPartial(Event):
    type: Literal["transcript.partial"] = "transcript.partial"
    turn_id: str
    text: str


class TranscriptFinal(Event):
    type: Literal["transcript.final"] = "transcript.final"
    turn_id: str
    text: str


class SpeechSegment(Event):
    """Announces one spoken segment; its audio follows as speech.audio chunks, then speech.segment.end."""

    type: Literal["speech.segment"] = "speech.segment"
    turn_id: str
    seq: int = Field(ge=0)
    text: str
    mime: str                            # "audio/pcm" (s16le mono, streamed) or a complete file type ("audio/wav", ...)
    sample_rate: int | None = None       # required for audio/pcm
    directives: list[TimedDirective] = Field(default_factory=list)


class SpeechAudio(Event):
    type: Literal["speech.audio"] = "speech.audio"
    turn_id: str
    seq: int = Field(ge=0)               # the segment
    index: int = Field(ge=0)             # chunk order within the segment
    data_b64: str


class SpeechSegmentEnd(Event):
    type: Literal["speech.segment.end"] = "speech.segment.end"
    turn_id: str
    seq: int = Field(ge=0)
    duration_ms: float = Field(ge=0)
    # Start time (ms) of every character of `text` when the TTS reports alignment (exact directive timing)
    char_times_ms: list[float] | None = None


class VisualDirectiveEvent(Event):
    type: Literal["visual.directive"] = "visual.directive"
    turn_id: str
    directive: Directive


class SceneBuilding(Event):
    """The brain asked for a scene and the codegen model is writing it: show the stage with a placeholder."""

    type: Literal["scene.building"] = "scene.building"
    turn_id: str
    scene_id: str
    kind: SceneKind
    title: str = ""


class SceneRender(Event):
    """Code for the scene sandbox; attempt 2 is the automatic repair after a failed scene.result."""

    type: Literal["scene.render"] = "scene.render"
    turn_id: str
    scene_id: str
    kind: SceneKind
    title: str = ""
    code: str
    attempt: int = 1
    codegen_ms: float | None = None      # how long the codegen model took to write (or repair) this code


class SceneFailed(Event):
    """No scene this time: the codegen model failed, or the repaired code still didn't render."""

    type: Literal["scene.failed"] = "scene.failed"
    turn_id: str
    scene_id: str
    error: str


class ConsentRequest(Event):
    """The brain proposed a browser task: ask the user. The extension works out the sites itself (cookie scope =
    the registrable domain of the turn's real tab; a start URL on another site makes the task cross-site)."""

    type: Literal["consent.request"] = "consent.request"
    turn_id: str
    task_id: str
    goal: str
    start_url: str | None = None        # validated http(s); None = start on the user's tab
    session: bool = True                # the task wants the user's login on the tab's site


class TaskStarted(Event):
    type: Literal["task.started"] = "task.started"
    task_id: str
    goal: str
    sites: list[str] = Field(default_factory=list)
    cookies: int = 0                    # how many of the user's cookies the agent's browser got


class TaskStep(Event):
    type: Literal["task.step"] = "task.step"
    task_id: str
    n: int
    action_text: str
    url: str | None = None
    thumb_b64: str | None = None


class TaskConfirmRequest(Event):
    type: Literal["task.confirm.request"] = "task.confirm.request"
    task_id: str
    request_id: str
    question: str


class TaskDone(Event):
    type: Literal["task.done"] = "task.done"
    task_id: str
    success: bool
    summary: str
    final_url: str | None = None


class SetupProgress(Event):
    type: Literal["setup.progress"] = "setup.progress"
    item: str                           # e.g. "ollama:qwen3.5:4b"
    status: str
    completed: int | None = None
    total: int | None = None


class ConversationStarted(Event):
    type: Literal["conversation.started"] = "conversation.started"
    conversation_id: str


class ProviderOption(Event):
    """A provider the options page can pick for a capability. `available` = its package is installed and its API
    key is set (health is only known once it is chosen); `models` = local Ollama models, when relevant."""

    name: str
    kind: Literal["paid", "open", "test"]
    source: Literal["api", "ollama", "huggingface", "local"]
    available: bool
    reason: str = ""
    default_model: str | None = None
    needs_model: bool = True
    models: list[str] = Field(default_factory=list)


class VoiceOption(Event):
    id: str
    name: str


class CapabilityConfig(Event):
    capability: str
    status: CapabilityStatus
    choice: ProviderChoiceWire | None = None     # what the options page set; None = clicky.toml or the preset
    providers: list[ProviderOption] = Field(default_factory=list)
    voices: list[VoiceOption] = Field(default_factory=list)   # TTS: the active provider's voices
    voice_key: str | None = None                 # the option that holds the voice ("voice", "voice_id")


class ConfigState(Event):
    """Answer to config.get / config.set (with its request_id), and broadcast to every session after a change."""

    type: Literal["config.state"] = "config.state"
    request_id: str | None = None
    profile: str
    profile_pinned: bool = False                 # CLICKY_PROFILE / `clicky serve --profile` decides, not the page
    profiles: list[str] = Field(default_factory=list)
    tier: str
    capabilities: list[CapabilityConfig] = Field(default_factory=list)
    page_budget: PageBudget | None = None        # the brain may have changed: capture accordingly
    error: str | None = None                     # the change was refused; nothing changed


ServerEvent = Annotated[
    Welcome | Pong | ErrorEvent | TurnState | AssistantTextDelta | TurnDone | CapabilityStatusEvent
    | TranscriptPartial | TranscriptFinal | SpeechSegment | SpeechAudio | SpeechSegmentEnd | VisualDirectiveEvent
    | SceneBuilding | SceneRender | SceneFailed | ConsentRequest
    | TaskStarted | TaskStep | TaskConfirmRequest | TaskDone | SetupProgress | ConversationStarted | ConfigState,
    Field(discriminator="type"),
]

CLIENT_EVENT_ADAPTER: TypeAdapter[ClientEvent] = TypeAdapter(ClientEvent)
SERVER_EVENT_ADAPTER: TypeAdapter[ServerEvent] = TypeAdapter(ServerEvent)


def parse_client_event(raw: str | bytes) -> ClientEvent:
    """Validate one JSON text frame. Malformed JSON and unknown types both raise pydantic.ValidationError."""
    return CLIENT_EVENT_ADAPTER.validate_json(raw)


def parse_server_event(raw: str | bytes) -> ServerEvent:
    return SERVER_EVENT_ADAPTER.validate_json(raw)
