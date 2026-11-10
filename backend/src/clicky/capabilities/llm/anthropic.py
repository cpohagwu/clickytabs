"""AnthropicChat: Claude through the official `anthropic` SDK (2.x, async, streaming).

Request, one per brain turn:
- model `claude-opus-5-5`. Thinking is always on for this model, so `thinking` is omitted and `output_config.effort`
  is the latency knob (low for the voice brain). Sampling parameters are never sent.
- max_tokens = the turn's reply budget + thinking headroom (thinking counts toward max_tokens).
- system: one text block with a cache breakpoint (the same for every conversation, pre-warmed with a max_tokens=0
  request), plus top-level automatic caching, which caches the conversation through the newest user turn so the next
  turn reads it back (the history is append-only: brain/history.py).
- fallbacks "default" (beta server-side-fallback-2026-07-01): a safety-classifier decline is re-run server-side on
  the model Anthropic recommends for its category. If an account rejects the parameter, the adapter retries once
  without it and stops sending it.
- messages: images as base64; earlier assistant turns replayed from their native content blocks, thinking included.

Response: text deltas stream as TextDelta; stop_reason is checked before anything is kept (refusal ->
Finish(refusal, stop_details.category), no native turn). native_turn = the content blocks as request params, minus
fallback markers and any thinking that precedes the last fallback boundary (the echo rule for fallback turns).
The SDK is imported lazily; the API key comes from settings and is never logged.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from clicky.capabilities.base import (
    ChatEvent,
    ChatMessage,
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
from clicky.capabilities.options import AnthropicChatOptions

if TYPE_CHECKING:
    from clicky.capabilities.context import ProviderContext

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
THINKING_HEADROOM = 4_096
PREWARM_EVERY_S = 270.0          # the prompt cache lives 5 minutes; re-write it only when nothing touched it lately
HEALTH_TIMEOUT_S = 6.0           # well inside the registry's 10 s health deadline
CONTEXT_WINDOW = 1_000_000

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end", "stop_sequence": "end", "pause_turn": "end", "tool_use": "end",
    "max_tokens": "max_tokens", "model_context_window_exceeded": "max_tokens", "refusal": "refusal",
}


class AnthropicError(RuntimeError):
    """A Claude API failure, described without secrets."""


def describe_api_error(exc: BaseException, model: str) -> str:
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return "Anthropic rejected ANTHROPIC_API_KEY (401)"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return f"this API key may not use {model} (403)"
    if isinstance(exc, anthropic.NotFoundError):
        return f"{model} was not found (404)"
    if isinstance(exc, anthropic.RateLimitError):
        return "rate limited by the Anthropic API (429); try again shortly"
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic API error {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APITimeoutError):
        return "the Anthropic API timed out"
    if isinstance(exc, anthropic.APIConnectionError):
        return "cannot reach the Anthropic API (network)"
    return str(exc) or type(exc).__name__


def _content(message: ChatMessage) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, ImagePart):
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": part.mime,
                                                       "data": base64.b64encode(part.data).decode("ascii")}})
        elif isinstance(part, TextPart) and part.text:
            blocks.append({"type": "text", "text": part.text})
    return blocks


def request_messages(req: ChatRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for message in req.messages:
        if message.role == "assistant" and isinstance(message.native, list) and message.native:
            messages.append({"role": "assistant", "content": [dict(block) for block in message.native]})
            continue
        content = _content(message)
        if content:
            messages.append({"role": message.role, "content": content})
    return messages


def native_blocks(content: list[Any]) -> list[dict[str, Any]] | None:
    """The reply's content blocks as request params for verbatim replay (see the module docstring)."""
    boundary = max((i for i, block in enumerate(content) if block.type == "fallback"), default=-1)
    blocks: list[dict[str, Any]] = []
    for index, block in enumerate(content):
        if block.type == "text":
            if block.text:
                blocks.append({"type": "text", "text": block.text})
        elif block.type == "thinking" and index > boundary:
            blocks.append({"type": "thinking", "thinking": block.thinking, "signature": block.signature})
        elif block.type == "redacted_thinking" and index > boundary:
            blocks.append({"type": "redacted_thinking", "data": block.data})
    return blocks if any(block["type"] == "text" for block in blocks) else None


class AnthropicChat:
    def __init__(self, spec: ProviderSpec, *, model: str | None, options: AnthropicChatOptions,
                 context: ProviderContext) -> None:
        import anthropic

        self.spec = spec
        self.model = model or DEFAULT_MODEL
        self.options = options
        self._ctx = context
        self._client = anthropic.AsyncAnthropic(
            api_key=context.settings.secret_value("anthropic_api_key"), max_retries=2,
            timeout=anthropic.Timeout(60.0, connect=5.0))
        self._fallbacks = options.fallbacks
        self._context_window = CONTEXT_WINDOW
        self._background: asyncio.Future[Any] | None = None    # a slow startup model lookup, left to finish
        self._touched = -PREWARM_EVERY_S    # last request that wrote or read the system-prompt cache

    def info(self) -> ModelInfo:
        return ModelInfo(name=self.model, vision=True, coord_space="image_px", context_window=self._context_window,
                         prompt="full", history="verbatim")

    async def health(self) -> Health:
        """A free model lookup: a bad key or model fails fast; a slow or unreachable API never marks Claude dead
        for the server's lifetime (turns retry), and a slow lookup is left to finish in the background rather than
        cancelled (the registry's own health deadline must not be reached waiting on a cancellation)."""
        import anthropic

        lookup = asyncio.ensure_future(self._client.models.retrieve(self.model))
        done, _pending = await asyncio.wait({lookup}, timeout=HEALTH_TIMEOUT_S)
        if not done:
            lookup.add_done_callback(lambda task: task.cancelled() or task.exception())   # consume the outcome
            self._background = lookup
            return Health(True, f"model check still running after {HEALTH_TIMEOUT_S:.0f} s; turns will retry")
        try:
            found = lookup.result()
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as exc:
            return Health(False, describe_api_error(exc, self.model),
                          f"check ANTHROPIC_API_KEY in {self._ctx.settings.home / '.env'} and the model id")
        except anthropic.APIError as exc:
            return Health(True, f"model check failed at startup ({describe_api_error(exc, self.model)}); "
                                "turns will retry")
        window = getattr(found, "max_input_tokens", None)
        if isinstance(window, int) and window > 0:
            self._context_window = window
        return Health(True)

    def max_tokens(self, req: ChatRequest) -> int:
        return self.options.max_tokens or req.max_tokens + THINKING_HEADROOM

    def build_params(self, req: ChatRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens(req),
            "messages": request_messages(req),
            "output_config": {"effort": self.options.effort},
            "cache_control": {"type": "ephemeral"},     # automatic caching through the newest user turn
        }
        if req.system:
            params["system"] = [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}]
        if self._fallbacks:
            params["fallbacks"] = "default"
            params["betas"] = [FALLBACK_BETA]
        return params

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        import anthropic

        started = False
        for attempt in range(2):
            params = self.build_params(req)
            self._touched = time.monotonic()
            try:
                async with self._client.beta.messages.stream(**params) as stream:
                    async for event in stream:
                        if event.type == "content_block_delta" and event.delta.type == "text_delta":
                            if event.delta.text:
                                started = True
                                yield TextDelta(event.delta.text)
                    message = await stream.get_final_message()
            except anthropic.BadRequestError as exc:
                if attempt == 0 and not started and self._fallbacks and "fallback" in str(exc.message).lower():
                    log.warning("this account rejects server-side fallbacks (%s); sending requests without them",
                                exc.message)
                    self._fallbacks = False
                    continue
                raise AnthropicError(describe_api_error(exc, self.model)) from exc
            except anthropic.APIError as exc:
                raise AnthropicError(describe_api_error(exc, self.model)) from exc
            yield self._finish(message)
            return

    def _finish(self, message: Any) -> Finish:
        usage = message.usage
        result = Usage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                       cache_read_tokens=usage.cache_read_input_tokens,
                       cache_write_tokens=usage.cache_creation_input_tokens)
        stop = _STOP_REASONS.get(str(message.stop_reason), "end")
        if stop == "refusal":       # check before reading content: a refusal's partial output is not kept
            details = getattr(message, "stop_details", None)
            return Finish("refusal", refusal_category=getattr(details, "category", None), usage=result)
        if message.model != self.model:
            log.info("answered by %s (server-side fallback)", message.model)
        return Finish(stop, usage=result, native_turn=native_blocks(list(message.content)))

    # ------------------------------------------------------------------------------------------------ warm-up

    async def warm_up(self, system: str | None = None) -> None:
        """Open the connection; with `system`, write its prompt cache (max_tokens=0: prefill only, no output)."""
        if system:
            await self._prewarm(system)
        else:
            await self._client.models.retrieve(self.model)

    async def prewarm(self, system: str) -> None:
        """Push-to-talk: re-write the system-prompt cache if no request has touched it recently."""
        if time.monotonic() - self._touched >= PREWARM_EVERY_S:
            await self._prewarm(system)

    async def _prewarm(self, system: str) -> None:
        self._touched = time.monotonic()
        # Same model, system and effort as real turns (both are rendered into the cached prompt); the breakpoint sits
        # on the system block, not on the placeholder message.
        await self._client.beta.messages.create(
            model=self.model, max_tokens=0, output_config={"effort": self.options.effort},
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "warmup"}])

    async def aclose(self) -> None:
        if self._background is not None and not self._background.done():
            self._background.cancel()
        await self._client.close()
