"""Runtime fallback for chat chains.

A turn tries the chain's active member, then each later healthy member, but only until the first text delta has
been emitted: once the user has seen (or heard) part of an answer, a failure ends the turn instead of restarting it
with another model. Provider streams are always consumed through contextlib.aclosing, so cancelling the turn closes
the upstream request immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import cast

from clicky.capabilities.base import AudioChunk, ChatModel, ChatRequest, Finish, TextDelta, TTSEngine
from clicky.capabilities.kinds import Capability
from clicky.capabilities.registry import CapabilityChain, ChainMember

log = logging.getLogger(__name__)


class ProviderStreamError(RuntimeError):
    """A provider reported a failure through its stream (Finish(stop_reason="error") or an empty stream)."""


class NoUsableProvider(RuntimeError):
    def __init__(self, capability: Capability, failures: list[tuple[str, str]], hint: str = "") -> None:
        detail = "; ".join(f"{label}: {reason}" for label, reason in failures) or "empty chain"
        super().__init__(f"no usable {capability.value} provider ({detail})")
        self.capability = capability
        self.failures = failures
        self.hint = hint


@dataclass(frozen=True)
class ChatOutcome:
    finish: Finish
    provider: str            # label ("ollama:qwen3.5:4b") of the member that answered
    fell_back: bool


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, NotImplementedError):
        return str(exc) or "not implemented"
    return str(exc) or type(exc).__name__


RequestBuilder = Callable[[ChainMember], ChatRequest]


async def stream_chat(chain: CapabilityChain, request: ChatRequest | RequestBuilder,
                      on_delta: Callable[[str], None]) -> ChatOutcome:
    """Stream one answer from `chain`, calling `on_delta` for every non-empty text delta.

    `request` may be a builder called once per member tried: the prompt variant, the history policy and whether a
    screenshot is attached all depend on the model that is about to answer.
    """
    failures: list[tuple[str, str]] = []
    hint = ""
    for member in chain.candidates():
        provider = member.provider
        if provider is None:
            failures.append((member.label, member.health.reason or "unavailable"))
            hint = hint or member.health.hint
            continue
        started = False
        try:
            model = cast(ChatModel, provider)      # chat chains only hold ChatModels
            built = request(member) if callable(request) else request
            async with contextlib.aclosing(model.stream(built)) as events:
                async for event in events:
                    if isinstance(event, TextDelta):
                        if event.text:
                            started = True
                            on_delta(event.text)
                    elif isinstance(event, Finish):
                        if event.stop_reason == "error" and not started:
                            raise ProviderStreamError(event.error or "provider reported an error")
                        return ChatOutcome(event, member.label, fell_back=bool(failures))
            if not started:
                raise ProviderStreamError("stream ended without output")
            return ChatOutcome(Finish("end"), member.label, fell_back=bool(failures))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if started:
                raise
            reason = describe_error(exc)
            log.warning("%s failed before its first delta (%s); trying the next provider", member.label, reason)
            failures.append((member.label, reason))
            hint = hint or member.health.hint
    raise NoUsableProvider(chain.capability, failures, hint)


async def complete_chat(chain: CapabilityChain, request: ChatRequest | RequestBuilder) -> tuple[str, ChatOutcome]:
    """The whole answer as one string (summaries, evals)."""
    parts: list[str] = []
    outcome = await stream_chat(chain, request, parts.append)
    return "".join(parts), outcome


async def synthesize_speech(chain: CapabilityChain, text: str) -> AsyncIterator[AudioChunk]:
    """Audio for `text` from the chain, falling back to the next healthy TTS member only before the first chunk."""
    failures: list[tuple[str, str]] = []
    hint = ""
    for member in chain.candidates():
        provider = member.provider
        if provider is None:
            failures.append((member.label, member.health.reason or "unavailable"))
            hint = hint or member.health.hint
            continue
        started = False
        try:
            engine = cast(TTSEngine, provider)       # TTS chains only hold TTSEngines
            async with contextlib.aclosing(engine.synthesize(text)) as chunks:
                async for chunk in chunks:
                    started = True
                    yield chunk
            if started:
                return
            raise ProviderStreamError("speech stream ended without audio")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if started:
                raise
            reason = describe_error(exc)
            log.warning("%s failed before its first audio (%s); trying the next provider", member.label, reason)
            failures.append((member.label, reason))
            hint = hint or member.health.hint
    raise NoUsableProvider(chain.capability, failures, hint)
