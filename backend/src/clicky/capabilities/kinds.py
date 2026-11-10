"""Leaf enums shared by config and the capability layer.

This module must not import anything from `clicky` so that `config` -> `capabilities` can never form an import cycle.
"""

from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    BRAIN = "brain"              # conversation + vision + pointing directives
    CODEGEN = "codegen"          # Three.js / canvas / SVG scene code
    AGENT_LLM = "agent_llm"      # LLM driving the browser-use automation worker
    STT = "stt"                  # speech-to-text, including endpointing (VAD)
    TTS = "tts"                  # text-to-speech
    SUMMARIZER = "summarizer"    # conversation-history compaction
    EMBEDDINGS = "embeddings"    # optional semantic memory


class Kind(StrEnum):
    PAID = "paid"
    OPEN = "open"
    TEST = "test"


class Source(StrEnum):
    API = "api"
    OLLAMA = "ollama"
    HUGGINGFACE = "huggingface"
    LOCAL = "local"


class Profile(StrEnum):
    LOCAL = "local"      # zero API keys: Ollama first, Hugging Face fallback
    CLOUD = "cloud"      # paid providers everywhere
    HYBRID = "hybrid"    # paid brain/codegen/agent, open speech
    FAKE = "fake"        # deterministic fakes for tests and offline demos


class HardwareTier(StrEnum):
    AUTO = "auto"
    CPU = "cpu"
    GPU_4GB = "gpu-4gb"
    GPU_8GB = "gpu-8gb"
    GPU_24GB = "gpu-24gb"
