"""Presets: the provider chain of every capability for each Profile x HardwareTier (plan §4).

Local chains are Ollama first, Hugging Face as fallback. Every local LLM chain ends with `qwen2.5vl:3b`, so a fresh
install with only that model still answers. On cpu / gpu-4gb only the brain model uses the GPU; every other Ollama
model is a derived, CPU-pinned copy (FROM base + num_gpu 0 + num_thread 4) that `clicky models pull` creates.

Runner options (num_ctx, num_batch, num_gpu, num_thread) are defined once per *model* and shared by every role,
because Ollama reloads a model whenever they change. Defaults live in DEFAULT_RUNNER; `[ollama.runner."<model>"]`
in clicky.toml overrides individual fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from clicky.capabilities.kinds import Capability as C
from clicky.capabilities.kinds import HardwareTier as T
from clicky.capabilities.kinds import Profile as P
from clicky.config import ProviderChoice, RunnerOptions

if TYPE_CHECKING:
    from clicky.config import Settings

# --------------------------------------------------------------------------------------------- derived models


@dataclass(frozen=True)
class DerivedModel:
    """An Ollama model created from `base` with baked-in parameters (POST /api/create)."""

    name: str
    base: str
    parameters: tuple[tuple[str, int], ...]

    def parameters_dict(self) -> dict[str, int]:
        return dict(self.parameters)

    def modelfile(self) -> str:
        lines = [f"FROM {self.base}", *(f"PARAMETER {key} {value}" for key, value in self.parameters)]
        return "\n".join(lines) + "\n"


CPU_PINNED = (("num_gpu", 0), ("num_thread", 4))

LFM3_AUDIO = "lfm3-audio:2b"
LFM3_AUDIO_CPU = DerivedModel("clicky-lfm3-audio-cpu", LFM3_AUDIO, CPU_PINNED)
EMBEDDINGGEMMA = "embeddinggemma"
EMBEDDINGGEMMA_CPU = DerivedModel("clicky-embeddinggemma-cpu", EMBEDDINGGEMMA, CPU_PINNED)

DERIVED_MODELS: dict[str, DerivedModel] = {m.name: m for m in (LFM3_AUDIO_CPU, EMBEDDINGGEMMA_CPU)}

# ----------------------------------------------------------------------------------------------- runner options

DEFAULT_RUNNER: dict[str, RunnerOptions] = {
    # gpu-4gb brain: weights + vision projector are ~3 GiB of ~3.5 GiB usable; `clicky doctor --bench` tunes it.
    "qwen3.5:4b": RunnerOptions(num_ctx=24576, num_batch=256),
    "qwen3-vl:4b-instruct": RunnerOptions(num_ctx=16384, num_batch=256),
    "ministral-3:3b": RunnerOptions(num_ctx=16384, num_batch=256),
    "qwen2.5vl:3b": RunnerOptions(num_ctx=8192, num_batch=256),
    "qwen3.5:2b": RunnerOptions(num_ctx=16384, num_batch=256),
    "qwen3.5:9b": RunnerOptions(num_ctx=32768, num_batch=512),
    "gemma4:12b-it-qat": RunnerOptions(num_ctx=32768, num_batch=512),
    "ministral-3:8b": RunnerOptions(num_ctx=32768, num_batch=512),
    "qwen3.6:27b": RunnerOptions(num_ctx=65536, num_batch=512),
    "gemma4:31b": RunnerOptions(num_ctx=32768, num_batch=512),
    "qwen3.6:35b-a3b": RunnerOptions(num_ctx=65536, num_batch=512),
}


def runner_options_for(model: str, settings: Settings | None = None) -> RunnerOptions:
    """Effective runner options for `model`: DEFAULT_RUNNER, then the fields set in [ollama.runner."<model>"]."""
    base = DEFAULT_RUNNER.get(model, RunnerOptions())
    override = settings.ollama.runner.get(model) if settings is not None else None
    if override is None:
        return base
    return base.model_copy(update=override.model_dump(exclude_unset=True))


# ----------------------------------------------------------------------------------------------------- presets

_OLLAMA_CHAT_OPTIONS: dict[str, Any] = {"think": False, "keep_alive": "30m"}

LOCAL_LLM: dict[T, tuple[str, ...]] = {
    T.CPU: ("qwen3.5:2b", "ministral-3:3b", "qwen2.5vl:3b"),
    T.GPU_4GB: ("qwen3.5:4b", "qwen3-vl:4b-instruct", "ministral-3:3b", "qwen2.5vl:3b"),
    T.GPU_8GB: ("qwen3.5:9b", "gemma4:12b-it-qat", "qwen3.5:4b", "qwen2.5vl:3b"),
    T.GPU_24GB: ("qwen3.6:27b", "gemma4:31b", "qwen3.5:9b", "qwen2.5vl:3b"),
}
LOCAL_AGENT: dict[T, tuple[str, ...]] = {
    T.CPU: LOCAL_LLM[T.CPU],
    T.GPU_4GB: LOCAL_LLM[T.GPU_4GB],
    T.GPU_8GB: ("qwen3.5:9b", "ministral-3:8b", "qwen3.5:4b"),
    T.GPU_24GB: ("qwen3.6:35b-a3b", "qwen3.6:27b", "qwen3.5:9b"),
}
_WHISPER: dict[T, str] = {
    T.CPU: "Systran/faster-whisper-small.en",
    T.GPU_4GB: "Systran/faster-whisper-small.en",
    T.GPU_8GB: "distil-whisper/distil-large-v3.5-ct2",
    T.GPU_24GB: "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
PARAKEET = "nemo-parakeet-tdt-0.6b-v2"
KOKORO = "kokoro-v1.0"
CLAUDE = "claude-opus-5-5"


def _chain(provider: str, models: tuple[str, ...], options: dict[str, Any] | None = None) -> ProviderChoice:
    opts = dict(options or {})
    primary, *rest = models
    return ProviderChoice(provider=provider, model=primary, options=opts,
                          fallbacks=[ProviderChoice(provider=provider, model=m, options=opts) for m in rest])


def _cpu_pinned(tier: T) -> bool:
    return tier in (T.CPU, T.GPU_4GB)


def _local(tier: T) -> dict[C, ProviderChoice]:
    llm = _chain("ollama", LOCAL_LLM[tier], _OLLAMA_CHAT_OPTIONS)
    lfm3 = LFM3_AUDIO_CPU.name if _cpu_pinned(tier) else LFM3_AUDIO
    # CTranslate2 on the GPU for bigger tiers (needs the CUDA/cuDNN runtime).
    whisper_options = {} if _cpu_pinned(tier) else {"device": "auto", "compute_type": "default"}
    if tier is T.GPU_24GB:
        embeddings = _chain("ollama", ("qwen3-embedding:4b", EMBEDDINGGEMMA))
    else:
        embeddings = _chain("ollama", (EMBEDDINGGEMMA_CPU.name if _cpu_pinned(tier) else EMBEDDINGGEMMA,))
    return {
        C.BRAIN: llm,
        C.CODEGEN: llm,              # same model as the brain, so no reload
        C.SUMMARIZER: llm,           # runs between turns on the already-loaded brain model
        C.AGENT_LLM: _chain("ollama", LOCAL_AGENT[tier]),
        C.STT: ProviderChoice(provider="ollama_audio", model=lfm3, fallbacks=[
            ProviderChoice(provider="parakeet", model=PARAKEET),
            ProviderChoice(provider="faster_whisper", model=_WHISPER[tier], options=whisper_options),
        ]),
        # Kokoro first where lfm3 would run on the CPU: measured on gpu-4gb, key release -> first audio p50
        # 7.3 s with Kokoro vs 11.5 s with lfm3 (lfm3 speaks slower than real time while the brain decodes, so the
        # player buffers). Without the `kokoro` extra or its files, the chain falls back to lfm3 by health.
        C.TTS: (ProviderChoice(provider="kokoro", model=KOKORO, fallbacks=[
            ProviderChoice(provider="ollama_audio", model=lfm3),
        ]) if _cpu_pinned(tier) else ProviderChoice(provider="ollama_audio", model=lfm3, fallbacks=[
            ProviderChoice(provider="kokoro", model=KOKORO),
        ])),
        C.EMBEDDINGS: embeddings,
    }


def _cloud() -> dict[C, ProviderChoice]:
    return {
        C.BRAIN: ProviderChoice(provider="anthropic", model=CLAUDE, options={"effort": "low"}),
        C.CODEGEN: ProviderChoice(provider="anthropic", model=CLAUDE, options={"effort": "medium"}),
        C.SUMMARIZER: ProviderChoice(provider="anthropic", model=CLAUDE, options={"effort": "low"}),
        C.AGENT_LLM: ProviderChoice(provider="anthropic", model=CLAUDE),
        C.STT: ProviderChoice(provider="deepgram", model="nova-3", fallbacks=[
            ProviderChoice(provider="openai", model="gpt-4o-mini-transcribe"),
        ]),
        C.TTS: ProviderChoice(provider="elevenlabs", model="eleven_flash_v2_5", fallbacks=[
            ProviderChoice(provider="openai", model="gpt-4o-mini-tts"),
        ]),
        C.EMBEDDINGS: ProviderChoice(provider="openai", model="text-embedding-3-small"),
    }


_PAID_LLM_CAPABILITIES = (C.BRAIN, C.CODEGEN, C.SUMMARIZER, C.AGENT_LLM)


def needs_tier(profile: P) -> bool:
    return profile in (P.LOCAL, P.HYBRID)


def presets_for(profile: P, tier: T) -> dict[C, ProviderChoice]:
    """The preset chain of every capability. `tier` must be concrete for the local and hybrid profiles."""
    if profile is P.FAKE:
        return {cap: ProviderChoice(provider="fake") for cap in C}
    if profile is P.CLOUD:
        return _cloud()
    if tier is T.AUTO:
        raise ValueError("resolve hardware_tier 'auto' before asking for local presets")
    local = _local(tier)
    if profile is P.LOCAL:
        return local
    cloud = _cloud()
    return {cap: (cloud[cap] if cap in _PAID_LLM_CAPABILITIES else local[cap]) for cap in C}


def preset_label(profile: P, tier: T) -> str:
    return f"preset {profile.value}" if not needs_tier(profile) else f"preset {profile.value}/{tier.value}"
