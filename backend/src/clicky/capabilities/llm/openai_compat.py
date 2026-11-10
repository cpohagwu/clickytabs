"""OpenAICompatChat: any server speaking OpenAI's /chat/completions, over plain httpx (SSE streaming).

The model id comes from config and is checked against GET {base_url}/models at startup. The API can't say whether a
model sees images or how it writes coordinates, so `vision`, `coord_space` and `prompt` are options. History uses the
"compact" policy. The API key comes from settings (OPENAI_API_KEY), never os.environ.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx

from clicky.capabilities.base import (
    ChatEvent,
    ChatRequest,
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
from clicky.capabilities.options import OpenAICompatChatOptions
from clicky.capabilities.speech_base import raise_for_status

if TYPE_CHECKING:
    from clicky.capabilities.context import ProviderContext

DEFAULT_BASE_URL = "https://api.openai.com/v1"
REQUEST_TIMEOUT_S = 60.0
HEALTH_TIMEOUT_S = 8.0

_STOP_REASONS: dict[str, StopReason] = {"stop": "end", "length": "max_tokens", "content_filter": "refusal"}


class OpenAICompatChat:
    def __init__(self, spec: ProviderSpec, *, model: str | None, options: OpenAICompatChatOptions,
                 context: ProviderContext) -> None:
        if not model:
            raise ValueError("openai_compat needs a model id")
        self.spec = spec
        self.model = model
        self.options = options
        self._ctx = context
        self.base_url = (options.base_url or context.settings.openai_base_url or DEFAULT_BASE_URL).rstrip("/")

    def info(self) -> ModelInfo:
        vision = self.options.vision
        return ModelInfo(name=self.model, vision=vision, coord_space=self.options.coord_space if vision else None,
                         prompt=self.options.prompt, history="compact")

    def _headers(self) -> dict[str, str]:
        key = self._ctx.settings.secret_value("openai_api_key")
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def health(self) -> Health:
        try:
            response = await self._ctx.http.get(f"{self.base_url}/models", headers=self._headers(),
                                                timeout=HEALTH_TIMEOUT_S)
        except httpx.HTTPError as exc:
            return Health(False, f"cannot reach {self.base_url} ({type(exc).__name__})",
                          "start the server, or fix base_url / OPENAI_BASE_URL")
        if response.status_code in (401, 403):
            return Health(False, f"{self.base_url} rejected OPENAI_API_KEY ({response.status_code})",
                          f"check OPENAI_API_KEY in {self._ctx.settings.home / '.env'}")
        if not response.is_success:
            return Health(True, f"model list unavailable ({response.status_code}); turns will try anyway")
        try:
            ids = {str(item.get("id")) for item in response.json().get("data", [])}
        except (ValueError, AttributeError):
            ids = set()
        if ids and self.model not in ids:
            return Health(False, f"{self.model} is not offered by {self.base_url}",
                          f"pick one of GET {self.base_url}/models")
        return Health(True)

    def build_body(self, req: ChatRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": req.system}] if req.system else []
        for message in req.messages:
            if message.role == "assistant":
                text = "".join(p.text for p in message.parts if isinstance(p, TextPart))
                messages.append({"role": "assistant", "content": text})
                continue
            content: list[dict[str, Any]] = []
            for part in message.parts:
                if isinstance(part, ImagePart):
                    data = base64.b64encode(part.data).decode("ascii")
                    content.append({"type": "image_url", "image_url": {"url": f"data:{part.mime};base64,{data}"}})
                elif part.text:
                    content.append({"type": "text", "text": part.text})
            messages.append({"role": "user", "content": content})
        body: dict[str, Any] = {
            "model": self.model, "messages": messages, "stream": True, "stream_options": {"include_usage": True},
            self.options.token_param: self.options.max_tokens or req.max_tokens,
        }
        for key in ("reasoning_effort", "temperature", "top_p"):
            if (value := getattr(self.options, key)) is not None:
                body[key] = value
        return body

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        content: list[str] = []
        finish_reason: str | None = None
        usage: Usage | None = None
        async with self._ctx.http.stream("POST", f"{self.base_url}/chat/completions", json=self.build_body(req),
                                         headers=self._headers(), timeout=REQUEST_TIMEOUT_S) as response:
            if not response.is_success:
                await response.aread()
                raise_for_status(response, f"{self.base_url} chat")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if counts := chunk.get("usage"):
                    usage = Usage(input_tokens=counts.get("prompt_tokens"),
                                  output_tokens=counts.get("completion_tokens"),
                                  cache_read_tokens=(counts.get("prompt_tokens_details") or {}).get("cached_tokens"))
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if text := delta.get("content"):
                        content.append(text)
                        yield TextDelta(text)
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
        stop = _STOP_REASONS.get(finish_reason or "stop", "end")
        native = {"role": "assistant", "content": "".join(content)} if stop != "refusal" else None
        yield Finish(stop, refusal_category="content_filter" if stop == "refusal" else None, usage=usage,
                     native_turn=native)

    async def warm_up(self, system: str | None = None) -> None:
        """Nothing to load remotely; a cheap model list opens the connection."""
        await self._ctx.http.get(f"{self.base_url}/models", headers=self._headers(), timeout=HEALTH_TIMEOUT_S)

    async def aclose(self) -> None:
        """The shared HTTP client belongs to the registry."""
