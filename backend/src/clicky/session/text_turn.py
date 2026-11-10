"""Text turns: turn.start{mode:"text"} -> turn.state(thinking) -> assistant.text.delta* -> turn.done.

Used by the popup's debug box and the HUD text box. A turn may carry page context. The brain
sees the conversation so far (brain/history.py), its reply is parsed for visual directives - deltas carry the text
without tags, and each validated directive goes out at once as `visual.directive` - and the exchange is recorded. The
brain chain may fall back to its next healthy member only before the first delta (capabilities/fallback.py).
"""

from __future__ import annotations

import asyncio
import logging

from clicky.brain.directives import DirectiveAt, ReplyEvent, SpeechText
from clicky.brain.history import Conversation
from clicky.capabilities.base import StopReason
from clicky.capabilities.fallback import ChatOutcome, NoUsableProvider, describe_error, stream_chat
from clicky.capabilities.kinds import Capability
from clicky.capabilities.registry import Capabilities
from clicky.protocol.events import (
    AssistantTextDelta,
    ErrorEvent,
    TurnDoneReason,
    TurnStart,
    TurnState,
    VisualDirectiveEvent,
)
from clicky.session.brain_turn import BrainTurn, SceneStarter, TaskStarter
from clicky.session.turn import TurnContext

log = logging.getLogger(__name__)

DONE_REASONS: dict[StopReason, TurnDoneReason] = {
    "end": "end", "max_tokens": "max_tokens", "refusal": "refusal", "error": "error",
}


def _emit(ctx: TurnContext, event: ReplyEvent) -> None:
    if isinstance(event, SpeechText):
        ctx.emit(AssistantTextDelta(turn_id=ctx.turn_id, delta=event.text))
    elif isinstance(event, DirectiveAt):
        ctx.emit(VisualDirectiveEvent(turn_id=ctx.turn_id, directive=event.directive))


async def run_text_turn(event: TurnStart, ctx: TurnContext, capabilities: Capabilities,
                        conversation: Conversation, *, on_scene: SceneStarter | None = None,
                        on_task: TaskStarter | None = None) -> None:
    """Never raises (except CancelledError): every failure becomes an `error` event plus turn.done(error)."""
    brain: BrainTurn | None = None
    try:
        text = (event.text or "").strip()
        if not text:
            ctx.fail("bad_request", "a text turn needs non-empty `text`")
            return
        ctx.emit(TurnState(turn_id=ctx.turn_id, state="thinking"))
        await conversation.ready()
        conversation.touch()
        brain = BrainTurn(conversation, capabilities, text, event.page, on_scene, on_task)
        turn = brain

        def on_delta(delta: str) -> None:
            for reply_event in turn.feed(delta):
                _emit(ctx, reply_event)

        outcome: ChatOutcome = await stream_chat(capabilities.get(Capability.BRAIN), brain.request_for, on_delta)
        for reply_event in brain.close():
            _emit(ctx, reply_event)
    except asyncio.CancelledError:
        if brain is not None:
            brain.record(None, None)         # the user saw part of the answer: keep it
        raise
    except NoUsableProvider as exc:
        message = f"{exc} - {exc.hint}" if exc.hint else str(exc)
        ctx.fail("provider_unavailable", message, retryable=True)
        return
    except Exception as exc:              # mid-stream failure: no fallback once the first delta went out
        if brain is not None:
            brain.record(None, None)
        log.warning("turn %s failed: %s", ctx.turn_id, describe_error(exc))
        ctx.fail("provider_error", describe_error(exc), retryable=True)
        return
    finish = outcome.finish
    brain.record(finish, outcome.provider)
    if finish.stop_reason == "error":
        ctx.emit(ErrorEvent(code="provider_error", message=finish.error or "the provider reported an error",
                            turn_id=ctx.turn_id, retryable=True))
    ctx.finish(DONE_REASONS[finish.stop_reason], detail=finish.refusal_category)
