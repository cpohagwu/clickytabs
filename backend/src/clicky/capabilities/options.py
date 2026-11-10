"""Typed per-adapter options.

`ProviderChoice.options` is a free-form table in clicky.toml. The registry validates it against the adapter's model
below (extra='forbid') when it builds the chains, so a misspelled or misplaced key fails at startup instead of being
silently ignored by a remote API. `ADAPTERS` also records each adapter's default model id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clicky.capabilities.kinds import Capability

RUNNER_KEYS = frozenset({"num_ctx", "num_batch", "num_gpu", "num_thread"})
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "seed", "num_predict")

ThinkSetting = bool | Literal["low", "medium", "high", "max"]
KeepAlive = str | int        # Go duration ("30m", "1h") or seconds; 0 unloads at once, -1 keeps the model loaded


class AdapterOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _reject(data: Any, keys: frozenset[str] | tuple[str, ...], message: str) -> Any:
    if isinstance(data, dict):
        found = sorted(set(keys) & data.keys())
        if found:
            raise ValueError(f"{', '.join(found)}: {message}")
    return data


# ------------------------------------------------------------------------------------------------------ chat


class OllamaChatOptions(AdapterOptions):
    """Per-role /api/chat settings. Runner options (num_ctx, ...) are per model: see [ollama.runner."<model>"]."""

    think: ThinkSetting = False          # top-level request field; the voice brain runs without thinking
    keep_alive: KeepAlive = "30m"        # top-level request field
    format: Literal["json"] | dict[str, Any] | None = None   # JSON mode or a JSON schema (structured output)
    # Sampling options, sent inside the request's `options` object. They never force a model reload.
    temperature: float | None = Field(default=None, ge=0)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=1)
    seed: int | None = None
    num_predict: int | None = Field(default=None, ge=-2)   # default: ChatRequest.max_tokens
    # Overrides for what clicky infers from the model family / size (llm/ollama.py).
    coord_space: Literal["image_px", "norm1000", "norm1"] | None = None
    prompt: Literal["full", "compact"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_runner_keys(cls, data: Any) -> Any:
        return _reject(
            data, RUNNER_KEYS,
            'runner options are defined once per model under [ollama.runner."<model>"] in clicky.toml; '
            "per-role values would make Ollama reload the model",
        )

    def sampling(self) -> dict[str, Any]:
        return {key: value for key in SAMPLING_KEYS if (value := getattr(self, key)) is not None}


class AnthropicChatOptions(AdapterOptions):
    """claude-opus-5-5: thinking is always on, so `effort` is the latency knob. Sampling params are rejected."""

    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"   # -> output_config.effort
    max_tokens: int | None = Field(default=None, ge=1)                  # default: ChatRequest.max_tokens
    fallbacks: bool = True             # server-side fallbacks:"default" (beta server-side-fallback-2026-07-01)

    @model_validator(mode="before")
    @classmethod
    def _no_sampling(cls, data: Any) -> Any:
        return _reject(data, ("temperature", "top_p", "top_k"),
                       "claude-opus-5-5 does not accept sampling parameters; tune `effort` instead")


class OpenAICompatChatOptions(AdapterOptions):
    """Any server speaking OpenAI's /chat/completions (OpenAI, vLLM, LM Studio, llama.cpp, ...)."""

    base_url: str | None = None          # default: OPENAI_BASE_URL, else api.openai.com
    reasoning_effort: str | None = None  # passed through; valid values depend on the model
    temperature: float | None = Field(default=None, ge=0)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1)
    token_param: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"   # older servers: max_tokens
    # What the model can do (the API can't tell): screenshots, and how it writes pixel coordinates.
    vision: bool = True
    coord_space: Literal["image_px", "norm1000", "norm1"] | None = "image_px"
    prompt: Literal["full", "compact"] = "full"


class FakeChatOptions(AdapterOptions):
    deltas: tuple[str, ...] | None = None   # None: echo the newest user text (see FakeChat for text markers)
    delay_s: float = Field(default=0.03, ge=0)
    fail_at: int | None = Field(default=None, ge=0)   # raise before emitting delta #n (0 = before the first)
    stop_reason: Literal["end", "max_tokens", "refusal", "error"] = "end"


# ------------------------------------------------------------------------------------------------- agent LLM


class AnthropicAgentOptions(AdapterOptions):
    """ChatAnthropic inside the worker, always with adaptive thinking (Opus 5.5 can't disable it, and browser-use
    then uses tool_choice auto, which Opus 5.5 requires). Effort is the speed / cost knob per agent step."""

    effort: Literal["low", "medium", "high"] = "low"
    max_tokens: int | None = Field(default=None, ge=1024)


class OpenAICompatAgentOptions(AdapterOptions):
    base_url: str | None = None
    reasoning_effort: str | None = None
    temperature: float | None = Field(default=None, ge=0)


class OllamaAgentOptions(AdapterOptions):
    """ChatOllama inside the worker; runner options come from [ollama.runner] like every other role."""

    think: ThinkSetting = False
    keep_alive: KeepAlive = "30m"
    flash_mode: bool = True              # browser-use fast mode: no evaluation/next-goal fields
    use_vision: bool = False             # DOM-only: small VLMs follow the action schema better without screenshots
    llm_timeout_s: float = Field(default=180.0, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _no_runner_keys(cls, data: Any) -> Any:
        return _reject(data, RUNNER_KEYS, 'runner options belong under [ollama.runner."<model>"] in clicky.toml')


class FakeAgentOptions(AdapterOptions):
    pass


# ------------------------------------------------------------------------------------------------------- STT


class OllamaAudioSTTOptions(AdapterOptions):
    keep_alive: str = "30m"              # set by warm-up; a voice turn after an idle spell must not pay the reload
    language: str | None = "en"
    prompt: str | None = None            # vocabulary hint
    temperature: float | None = Field(default=None, ge=0)


class ParakeetOptions(AdapterOptions):
    quantization: Literal["int8"] | None = "int8"
    num_threads: int = Field(default=4, ge=1)


class FasterWhisperOptions(AdapterOptions):
    device: Literal["cpu", "cuda", "auto"] = "cpu"
    compute_type: str = "int8"           # CTranslate2 compute type: int8, int8_float16, float16, default, ...
    beam_size: int = Field(default=1, ge=1)
    language: str | None = "en"
    num_threads: int = Field(default=4, ge=1)


class DeepgramSTTOptions(AdapterOptions):
    language: str = "en"
    smart_format: bool = True
    interim_results: bool = True
    endpointing_ms: int = Field(default=300, ge=10)
    utterance_end_ms: int = Field(default=1000, ge=1000)
    vad_events: bool = True              # SpeechStarted events
    base_url: str = "wss://api.deepgram.com"


class OpenAISTTOptions(AdapterOptions):
    language: str | None = "en"
    prompt: str | None = None
    base_url: str = "https://api.openai.com/v1"   # or any OpenAI-compatible server


class FakeSTTOptions(AdapterOptions):
    transcript: str = "hello from the fake microphone"
    delay_s: float = Field(default=0.0, ge=0)   # simulated transcription time


# ------------------------------------------------------------------------------------------------------- TTS


class OllamaAudioTTSOptions(AdapterOptions):
    keep_alive: str = "30m"
    voice: str | None = None             # model default when unset (unknown voices are rejected)
    speed: float = Field(default=1.0, gt=0)
    sample_rate: int = Field(default=24_000, ge=8_000)   # of the raw PCM Ollama streams


class KokoroOptions(AdapterOptions):
    voice: str = "af_heart"
    speed: float = Field(default=1.0, gt=0)
    lang: str = "en-us"
    threads: int | None = Field(default=None, ge=1, le=64)   # onnxruntime intra-op threads (None: physical cores)


class ElevenLabsOptions(AdapterOptions):
    # "Sarah", one of ElevenLabs' default voices: usable on the free tier (library voices, such as the older default
    # "Rachel", answer 402 paid_plan_required to free accounts).
    voice_id: str = "EXAVITQu4vr4xnSDxMaL"
    output_format: Literal["pcm_16000", "pcm_22050", "pcm_24000", "pcm_44100"] = "pcm_24000"  # raw PCM streams
    with_timestamps: bool = True         # character alignment drives directive timing
    base_url: str = "https://api.elevenlabs.io"


class OpenAITTSOptions(AdapterOptions):
    voice: str = "marin"                 # marin / cedar are OpenAI's recommended voices
    instructions: str | None = None      # delivery style, e.g. "friendly, brisk"
    speed: float = Field(default=1.0, gt=0)
    base_url: str = "https://api.openai.com/v1"


class FakeTTSOptions(AdapterOptions):
    voice: Literal["fake-bright", "fake-calm"] = "fake-bright"     # two names, so the options page can switch
    sample_rate: int = Field(default=24_000, ge=8_000)
    ms_per_char: float = Field(default=12.0, ge=0)
    max_ms: int = Field(default=2_000, ge=50)
    chunk_ms: int = Field(default=200, ge=10)        # streamed in chunks like a real engine
    delay_s: float = Field(default=0.0, ge=0)        # between chunks


# ------------------------------------------------------------------------------------------------ embeddings


class OllamaEmbedOptions(AdapterOptions):
    keep_alive: KeepAlive = "30m"
    truncate: bool = True
    dimensions: int | None = Field(default=None, ge=1)


class OpenAIEmbedOptions(AdapterOptions):
    dimensions: int | None = Field(default=None, ge=1)


class FakeEmbedOptions(AdapterOptions):
    dimensions: int = Field(default=8, ge=1)


# ------------------------------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class AdapterSpec:
    options: type[AdapterOptions]
    default_model: str | None = None     # used when a ProviderChoice omits `model`
    needs_model: bool = True


_CHAT_ADAPTERS = {
    "anthropic": AdapterSpec(AnthropicChatOptions, "claude-opus-5-5"),
    "openai_compat": AdapterSpec(OpenAICompatChatOptions),        # model id comes from config
    "ollama": AdapterSpec(OllamaChatOptions),
    "fake": AdapterSpec(FakeChatOptions, needs_model=False),
}

ADAPTERS: dict[tuple[Capability, str], AdapterSpec] = {
    **{(cap, name): adapter
       for cap in (Capability.BRAIN, Capability.CODEGEN, Capability.SUMMARIZER)
       for name, adapter in _CHAT_ADAPTERS.items()},
    (Capability.AGENT_LLM, "anthropic"): AdapterSpec(AnthropicAgentOptions, "claude-opus-5-5"),
    (Capability.AGENT_LLM, "openai_compat"): AdapterSpec(OpenAICompatAgentOptions),
    (Capability.AGENT_LLM, "ollama"): AdapterSpec(OllamaAgentOptions),
    (Capability.AGENT_LLM, "fake"): AdapterSpec(FakeAgentOptions, needs_model=False),
    (Capability.STT, "deepgram"): AdapterSpec(DeepgramSTTOptions, "nova-3"),
    (Capability.STT, "openai"): AdapterSpec(OpenAISTTOptions, "gpt-4o-mini-transcribe"),
    (Capability.STT, "ollama_audio"): AdapterSpec(OllamaAudioSTTOptions),
    (Capability.STT, "parakeet"): AdapterSpec(ParakeetOptions, "nemo-parakeet-tdt-0.6b-v2"),
    (Capability.STT, "faster_whisper"): AdapterSpec(FasterWhisperOptions, "Systran/faster-whisper-small.en"),
    (Capability.STT, "fake"): AdapterSpec(FakeSTTOptions, needs_model=False),
    (Capability.TTS, "elevenlabs"): AdapterSpec(ElevenLabsOptions, "eleven_flash_v2_5"),
    (Capability.TTS, "openai"): AdapterSpec(OpenAITTSOptions, "gpt-4o-mini-tts"),
    (Capability.TTS, "ollama_audio"): AdapterSpec(OllamaAudioTTSOptions),
    (Capability.TTS, "kokoro"): AdapterSpec(KokoroOptions, "kokoro-v1.0"),
    (Capability.TTS, "fake"): AdapterSpec(FakeTTSOptions, needs_model=False),
    (Capability.EMBEDDINGS, "openai"): AdapterSpec(OpenAIEmbedOptions, "text-embedding-3-small"),
    (Capability.EMBEDDINGS, "ollama"): AdapterSpec(OllamaEmbedOptions),
    (Capability.EMBEDDINGS, "fake"): AdapterSpec(FakeEmbedOptions, needs_model=False),
}


def adapter_for(capability: Capability, provider: str) -> AdapterSpec:
    try:
        return ADAPTERS[(capability, provider)]
    except KeyError:
        raise KeyError(f"no options model registered for {capability.value}/{provider}") from None
