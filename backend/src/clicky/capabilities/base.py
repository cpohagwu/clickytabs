"""Model-agnostic capability contracts.

Every provider (paid API, Ollama, Hugging Face, fake) implements one of these small interfaces. Orchestration code
only ever talks to these types, so swapping a model never touches the turn pipeline.

Rules shared by all providers:
- `health()` is cheap and side-effect free (no model loads); it reports *why* a provider is unusable and how to fix it.
- Streams are consumed with `contextlib.aclosing(...)`, so cancelling a turn closes the upstream request at once.
- A fallback provider may only take over before the first delta/byte of a turn has been emitted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from clicky.capabilities.kinds import Capability, Kind, Source

# --------------------------------------------------------------------------------------------- catalog / health


@dataclass(frozen=True)
class ProviderSpec:
    """One catalog row. Reading the catalog never imports provider modules or their heavy dependencies."""

    capability: Capability
    name: str                    # "ollama", "anthropic", "ollama_audio", "parakeet", "kokoro", "fake", ...
    kind: Kind                   # paid | open | test
    source: Source               # api | ollama | huggingface | local
    target: str                  # "package.module:ClassName", imported lazily
    extra: str | None = None     # uv extra that installs its dependencies (checked with importlib.util.find_spec)
    secret: str | None = None    # Settings attribute that must be set, e.g. "anthropic_api_key"
    planned: bool = False        # a typed stub of a planned provider: reports unhealthy, raises NotImplementedError
    requires: tuple[str, ...] = ()   # importable top-level modules the provider needs, e.g. ("av",)

    @property
    def key(self) -> str:
        return f"{self.capability.value}/{self.name}"


@dataclass(frozen=True)
class Health:
    ok: bool
    reason: str = ""
    hint: str = ""               # e.g. "qwen3.5:4b not pulled -> run: clicky models pull --profile local"


@runtime_checkable
class Provider(Protocol):
    spec: ProviderSpec

    async def health(self) -> Health: ...

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------------------------------- chat models

CoordSpace = Literal["image_px", "norm1000", "norm1"]
PromptVariant = Literal["full", "compact"]
HistoryPolicy = Literal["verbatim", "compact"]


@dataclass(frozen=True)
class ModelInfo:
    name: str
    vision: bool = False
    coord_space: CoordSpace | None = None    # how the model expresses pixel coordinates, if it can point at all
    context_window: int | None = None
    # Brain prompt variant: "full" for API and big local models, "compact" (few-shot, fewer tags) for small ones.
    prompt: PromptVariant = "compact"
    # How earlier turns are replayed. "verbatim": every user turn exactly as sent, page context included, and the
    # provider-native assistant turns (Claude preserved thinking needs an append-only history). "compact": earlier
    # turns as text only, page context in the newest user turn alone (small context windows, prefix KV caches).
    history: HistoryPolicy = "compact"


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    data: bytes                  # raw JPEG/PNG bytes; adapters encode as their API requires
    mime: str = "image/jpeg"


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    parts: tuple[TextPart | ImagePart, ...]
    native: object | None = None  # provider-native assistant turn, replayed verbatim to the same provider only


@dataclass(frozen=True)
class ChatRequest:
    system: str
    messages: tuple[ChatMessage, ...]
    max_tokens: int = 4096
    purpose: Literal["brain", "codegen", "summarizer"] = "brain"


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None            # uncached input
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None      # input written to a prompt cache (Claude)


@dataclass(frozen=True)
class TextDelta:
    text: str


StopReason = Literal["end", "max_tokens", "refusal", "error"]


@dataclass(frozen=True)
class Finish:
    stop_reason: StopReason
    refusal_category: str | None = None
    usage: Usage | None = None
    native_turn: object | None = None   # e.g. Anthropic content blocks incl. thinking; stored for append-only replay
    error: str | None = None


ChatEvent = TextDelta | Finish


@runtime_checkable
class ChatModel(Provider, Protocol):
    """brain / codegen / summarizer."""

    def info(self) -> ModelInfo: ...

    def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]: ...

    async def warm_up(self, system: str | None = None) -> None:
        """Load the model / open the connection; with `system`, also pre-fill (cache) that system prompt."""
        ...


# --------------------------------------------------------------------------------------------------------- speech


@dataclass(frozen=True)
class Partial:
    text: str


@dataclass(frozen=True)
class SpeechStarted:
    at_ms: float


@dataclass(frozen=True)
class SpeechEnded:
    at_ms: float


@dataclass(frozen=True)
class UtteranceEnd:
    pass


STTEvent = Partial | SpeechStarted | SpeechEnded | UtteranceEnd


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None = None
    duration_ms: float | None = None


class STTStream(Protocol):
    async def feed(self, chunk: bytes) -> None: ...

    def events(self) -> AsyncIterator[STTEvent]: ...     # partials + endpointing

    async def finish(self) -> Transcript: ...


@runtime_checkable
class STTEngine(Provider, Protocol):
    async def open_stream(self, *, mime: str, language: str | None = "en") -> STTStream: ...


@dataclass(frozen=True)
class AudioChunk:
    data: bytes
    mime: str | None = None                  # set on the first chunk, e.g. "audio/wav" or "audio/mpeg"
    sample_rate: int | None = None
    alignment: tuple[tuple[str, float, float], ...] | None = None   # (chars, start_ms, end_ms) when available


@runtime_checkable
class TTSEngine(Provider, Protocol):
    def synthesize(self, text: str, *, voice: str | None = None) -> AsyncIterator[AudioChunk]: ...


# ----------------------------------------------------------------------------------------------------- embeddings


@runtime_checkable
class Embedder(Provider, Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


# ------------------------------------------------------------------------------------------------- agent LLM


@dataclass(frozen=True)
class AgentLLMConfigOut:
    """What the automation worker needs to build its browser-use LLM. API keys travel via the child env only."""

    provider: str
    model: str
    base_url: str | None = None
    options: dict[str, object] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)   # e.g. {"ANTHROPIC_API_KEY": "..."}; never logged


@runtime_checkable
class AgentLLM(Provider, Protocol):
    """agent_llm has no in-process inference: it describes the LLM the worker should build."""

    def worker_config(self) -> AgentLLMConfigOut: ...


CAPABILITY_PROTOCOLS: dict[Capability, type] = {
    Capability.BRAIN: ChatModel,
    Capability.CODEGEN: ChatModel,
    Capability.SUMMARIZER: ChatModel,
    Capability.AGENT_LLM: AgentLLM,
    Capability.STT: STTEngine,
    Capability.TTS: TTSEngine,
    Capability.EMBEDDINGS: Embedder,
}
