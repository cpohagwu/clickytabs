"""Turn lifecycle shared by every turn (text turns: text_turn.py; voice turns: voice_turn.py).

Invariant: every accepted `turn.start` ends with exactly one `turn.done`, whoever gets there first (the turn task,
a cancel, or a barge-in). TurnContext enforces it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from clicky.protocol.events import ErrorEvent, Event, TurnDone, TurnDoneReason


@dataclass
class TurnContext:
    turn_id: str
    emit: Callable[[Event], None]
    finished: bool = False
    metrics: dict[str, float] = field(default_factory=dict)   # sent with turn.done

    def finish(self, reason: TurnDoneReason, detail: str | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        self.emit(TurnDone(turn_id=self.turn_id, reason=reason, detail=detail, metrics=self.metrics or None))

    def fail(self, code: str, message: str, *, retryable: bool = False) -> None:
        """Report an error for this turn and end it with reason "error"."""
        if self.finished:
            return
        self.emit(ErrorEvent(code=code, message=message, turn_id=self.turn_id, retryable=retryable))
        self.finish("error", detail=code)
