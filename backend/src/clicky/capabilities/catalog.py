"""Provider catalog.

Invariant (enforced by tests/test_catalog_invariants.py): every capability lists at least one `paid` provider and at
least one `open` provider. Open providers come from Ollama first and Hugging Face as fallback; LLM weights hosted on
Hugging Face are reached through Ollama's `hf.co/<user>/<repo>:<quant>` tags.
"""

from __future__ import annotations

from collections.abc import Iterator

from clicky.capabilities.base import ProviderSpec
from clicky.capabilities.kinds import Capability as C
from clicky.capabilities.kinds import Kind as K
from clicky.capabilities.kinds import Source as S

_CHAT_CAPABILITIES = (C.BRAIN, C.CODEGEN, C.SUMMARIZER)


def _chat_rows() -> Iterator[ProviderSpec]:
    for cap in _CHAT_CAPABILITIES:
        yield ProviderSpec(cap, "anthropic", K.PAID, S.API, "clicky.capabilities.llm.anthropic:AnthropicChat",
                           extra="anthropic", secret="anthropic_api_key", requires=("anthropic",))
        yield ProviderSpec(cap, "openai_compat", K.PAID, S.API,          # plain httpx: no SDK needed
                           "clicky.capabilities.llm.openai_compat:OpenAICompatChat",
                           secret="openai_api_key")
        yield ProviderSpec(cap, "ollama", K.OPEN, S.OLLAMA, "clicky.capabilities.llm.ollama:OllamaChat")
        yield ProviderSpec(cap, "fake", K.TEST, S.LOCAL, "clicky.capabilities.fake:FakeChat")


CATALOG: tuple[ProviderSpec, ...] = (
    *_chat_rows(),
    # --- browser-use agent LLM (built inside the automation worker; these rows only describe + health-check it)
    ProviderSpec(C.AGENT_LLM, "anthropic", K.PAID, S.API, "clicky.capabilities.agent_llm:AnthropicAgentLLM",
                 secret="anthropic_api_key"),
    ProviderSpec(C.AGENT_LLM, "openai_compat", K.PAID, S.API, "clicky.capabilities.agent_llm:OpenAICompatAgentLLM",
                 secret="openai_api_key"),
    ProviderSpec(C.AGENT_LLM, "ollama", K.OPEN, S.OLLAMA, "clicky.capabilities.agent_llm:OllamaAgentLLM"),
    ProviderSpec(C.AGENT_LLM, "fake", K.TEST, S.LOCAL, "clicky.capabilities.fake:FakeAgentLLM"),
    # --- speech-to-text (+ endpointing)
    # Speech APIs are called over httpx / websockets (core dependencies): no vendor SDK needed.
    ProviderSpec(C.STT, "deepgram", K.PAID, S.API, "clicky.capabilities.stt.deepgram:DeepgramSTT",
                 secret="deepgram_api_key", requires=("websockets",)),
    ProviderSpec(C.STT, "openai", K.PAID, S.API, "clicky.capabilities.stt.openai:OpenAISTT",
                 secret="openai_api_key"),
    ProviderSpec(C.STT, "ollama_audio", K.OPEN, S.OLLAMA, "clicky.capabilities.stt.ollama_audio:OllamaAudioSTT",
                 extra="local", requires=("av",)),
    ProviderSpec(C.STT, "parakeet", K.OPEN, S.HUGGINGFACE, "clicky.capabilities.stt.parakeet:ParakeetSTT",
                 extra="local-hf", requires=("onnx_asr", "av")),
    ProviderSpec(C.STT, "faster_whisper", K.OPEN, S.HUGGINGFACE,
                 "clicky.capabilities.stt.faster_whisper:FasterWhisperSTT",
                 extra="local-hf", requires=("faster_whisper", "av")),
    ProviderSpec(C.STT, "fake", K.TEST, S.LOCAL, "clicky.capabilities.fake:FakeSTT"),
    # --- text-to-speech
    ProviderSpec(C.TTS, "elevenlabs", K.PAID, S.API, "clicky.capabilities.tts.elevenlabs:ElevenLabsTTS",
                 secret="elevenlabs_api_key"),
    ProviderSpec(C.TTS, "openai", K.PAID, S.API, "clicky.capabilities.tts.openai:OpenAITTS",
                 secret="openai_api_key"),
    ProviderSpec(C.TTS, "ollama_audio", K.OPEN, S.OLLAMA, "clicky.capabilities.tts.ollama_audio:OllamaAudioTTS"),
    ProviderSpec(C.TTS, "kokoro", K.OPEN, S.HUGGINGFACE, "clicky.capabilities.tts.kokoro:KokoroTTS",
                 extra="kokoro", requires=("kokoro_onnx",)),
    ProviderSpec(C.TTS, "fake", K.TEST, S.LOCAL, "clicky.capabilities.fake:FakeTTS"),
    # --- embeddings (optional semantic memory)
    ProviderSpec(C.EMBEDDINGS, "openai", K.PAID, S.API, "clicky.capabilities.embed.openai:OpenAIEmbedder",
                 extra="openai", secret="openai_api_key", planned=True, requires=("openai",)),
    ProviderSpec(C.EMBEDDINGS, "ollama", K.OPEN, S.OLLAMA, "clicky.capabilities.embed.ollama:OllamaEmbedder",
                 planned=True),
    ProviderSpec(C.EMBEDDINGS, "fake", K.TEST, S.LOCAL, "clicky.capabilities.fake:FakeEmbedder"),
)


def providers_for(capability: C) -> tuple[ProviderSpec, ...]:
    return tuple(spec for spec in CATALOG if spec.capability is capability)


def get_spec(capability: C, name: str) -> ProviderSpec:
    for spec in CATALOG:
        if spec.capability is capability and spec.name == name:
            return spec
    known = ", ".join(s.name for s in providers_for(capability)) or "none"
    raise KeyError(f"unknown provider {name!r} for capability {capability.value!r} (known: {known})")
