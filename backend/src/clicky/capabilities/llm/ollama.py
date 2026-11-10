"""OllamaChat: brain / codegen / summarizer over Ollama's native /api/chat (httpx, NDJSON streaming).

Request layout (misplaced keys are silently ignored by Ollama, so this is pinned by tests):
- top level: model, messages (images as base64), stream, think, keep_alive, format
- `options`: the model's runner options (num_ctx, num_batch, num_gpu, num_thread; shared by every role so the model
  is never reloaded) + this role's sampling options (temperature, top_p, top_k, seed, num_predict)
"""

from __future__ import annotations

import base64
import contextlib
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from clicky.capabilities.base import (
    ChatEvent,
    ChatRequest,
    CoordSpace,
    Finish,
    Health,
    ImagePart,
    ModelInfo,
    ProviderSpec,
    StopReason,
    TextDelta,
    TextPart,
    Usage,
)
from clicky.capabilities.context import ProviderContext
from clicky.capabilities.ollama_api import OllamaError
from clicky.capabilities.options import OllamaChatOptions

log = logging.getLogger(__name__)

_STOP_REASONS: dict[str, StopReason] = {"stop": "end", "length": "max_tokens", "load": "end", "unload": "end"}

FULL_PROMPT_BILLIONS = 12.0      # smaller models get the compact, few-shot prompt
# How each vision family writes coordinates when grounding (prefix of the model name; first match wins).
_COORD_SPACES: tuple[tuple[str, CoordSpace], ...] = (
    ("qwen2.5vl", "image_px"), ("qwen2.5-vl", "image_px"),
    ("qwen3-vl", "norm1000"), ("qwen3vl", "norm1000"), ("qwen3.5", "norm1000"), ("qwen3.6", "norm1000"),
    ("gemma3", "norm1000"), ("gemma4", "norm1000"),
    ("moondream", "norm1"),
)


def coord_space_for(model: str) -> CoordSpace:
    name = model.lower().removeprefix("hf.co/").rsplit("/", 1)[-1]
    return next((space for prefix, space in _COORD_SPACES if name.startswith(prefix)), "image_px")


def parameter_billions(size: object) -> float | None:
    """"4.7B" -> 4.7, "820M" -> 0.82 (Ollama's details.parameter_size)."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([BbMm])\s*", str(size or ""))
    if match is None:
        return None
    value = float(match.group(1))
    return value if match.group(2) in "Bb" else value / 1000


def _usage(chunk: dict[str, Any]) -> Usage:
    return Usage(input_tokens=chunk.get("prompt_eval_count"), output_tokens=chunk.get("eval_count"),
                 cache_read_tokens=chunk.get("prompt_eval_cached_count"))


class OllamaChat:
    def __init__(self, spec: ProviderSpec, *, model: str, options: OllamaChatOptions,
                 context: ProviderContext) -> None:
        self.spec = spec
        self.model = model
        self.options = options
        self._ctx = context
        self._info = ModelInfo(name=model)
        self._capabilities: frozenset[str] | None = None    # from /api/show, once health() ran
        self._think_downgraded = False

    def info(self) -> ModelInfo:
        return self._info

    @property
    def capabilities(self) -> frozenset[str] | None:
        return self._capabilities

    async def health(self) -> Health:
        api = self._ctx.ollama
        try:
            if not await api.has_model(self.model):
                return Health(False, f"{self.model} is not pulled", self._ctx.pull_hint(self.model))
            show = await api.show(self.model)
        except OllamaError as exc:
            return Health(False, str(exc), "start Ollama, or set [ollama] host in clicky.toml / OLLAMA_HOST")
        self._apply_show(show)
        caps = self._capabilities or frozenset()
        if caps and "completion" not in caps:
            return Health(False, f"{self.model} cannot chat (capabilities: {', '.join(sorted(caps))})",
                          "pick a chat model for this capability")
        return Health(True, "" if self._info.vision else "no vision: text + DOM-only pointing")

    def _apply_show(self, show: dict[str, Any]) -> None:
        self._capabilities = frozenset(str(c) for c in show.get("capabilities") or ())
        model_info = show.get("model_info") or {}
        arch = model_info.get("general.architecture")
        context_window = model_info.get(f"{arch}.context_length") if arch else None
        vision = "vision" in self._capabilities
        size = parameter_billions((show.get("details") or {}).get("parameter_size"))
        prompt = self.options.prompt or ("full" if size is not None and size >= FULL_PROMPT_BILLIONS else "compact")
        coords = (self.options.coord_space or coord_space_for(self.model)) if vision else None
        self._info = ModelInfo(name=self.model, vision=vision, coord_space=coords,
                               context_window=context_window if isinstance(context_window, int) else None,
                               prompt=prompt, history="compact")

    # ------------------------------------------------------------------------------------------------ requests

    def _think(self) -> object:
        think = self.options.think
        if think and self._capabilities is not None and "thinking" not in self._capabilities:
            if not self._think_downgraded:
                self._think_downgraded = True
                log.warning("%s has no thinking capability: sending think=false", self.model)
            return False
        return think

    def _messages(self, req: ChatRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": req.system}] if req.system else []
        for msg in req.messages:
            if msg.role == "assistant" and isinstance(msg.native, dict):
                messages.append(dict(msg.native))     # replay our own native turn verbatim
                continue
            entry: dict[str, Any] = {
                "role": msg.role,
                "content": "\n".join(p.text for p in msg.parts if isinstance(p, TextPart)),
            }
            images = [base64.b64encode(p.data).decode("ascii") for p in msg.parts if isinstance(p, ImagePart)]
            if images:
                entry["images"] = images
            messages.append(entry)
        return messages

    def runner_options(self) -> dict[str, Any]:
        return self._ctx.runner_options(self.model).model_dump(exclude_none=True)

    def build_body(self, req: ChatRequest) -> dict[str, Any]:
        sampling = self.options.sampling()
        sampling.setdefault("num_predict", req.max_tokens)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(req),
            "stream": True,
            "think": self._think(),
            "keep_alive": self.options.keep_alive,
            "options": {**self.runner_options(), **sampling},
        }
        if self.options.format is not None:
            body["format"] = self.options.format
        return body

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        content: list[str] = []
        thinking: list[str] = []
        async with contextlib.aclosing(self._ctx.ollama.stream("/api/chat", self.build_body(req))) as chunks:
            async for chunk in chunks:
                message = chunk.get("message") or {}
                if message.get("thinking"):
                    thinking.append(message["thinking"])
                if text := message.get("content"):
                    content.append(text)
                    yield TextDelta(text)
                if chunk.get("done"):
                    native: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
                    if thinking:
                        native["thinking"] = "".join(thinking)
                    yield Finish(_STOP_REASONS.get(str(chunk.get("done_reason") or "stop"), "end"),
                                 usage=_usage(chunk), native_turn=native)
                    return
        raise OllamaError(f"{self.model}: stream ended without a final chunk")

    async def warm_up(self, system: str | None = None) -> None:
        """Load the model with exactly the runner options real requests use (otherwise the first turn reloads).

        With `system`, also evaluate the system prompt (one token generated), so the first turn's prefill starts
        after it: Ollama reuses the longest common prompt prefix. Push-to-talk never re-warms Ollama - that would
        replace a conversation's cached prefix with this shorter one.
        """
        if not system:
            await self._ctx.ollama.post("/api/chat", {
                "model": self.model,
                "messages": [],
                "keep_alive": self.options.keep_alive,
                "options": self.runner_options(),
            })
            return
        await self._ctx.ollama.post("/api/chat", {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": "Hello"}],
            "stream": False,
            "think": self._think(),
            "keep_alive": self.options.keep_alive,
            "options": {**self.runner_options(), "num_predict": 1},
        })

    async def aclose(self) -> None:
        """Nothing to release: the shared HTTP client belongs to the registry."""
