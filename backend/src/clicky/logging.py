"""Logging setup with secret redaction.

`clicky serve` runs uvicorn with log_config=None, so this module owns logging. One RedactingFilter is attached to
the *handlers* of the root logger and of the uvicorn loggers: a filter on a logger only sees records logged to that
exact logger, while a handler filter sees everything the handler emits, including records propagated from child
loggers (httpx, starlette, ...). It masks the pairing token, explicitly registered secrets (API keys), and anything
that looks like an Anthropic/OpenAI key or a bearer credential.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable
from typing import Any

MASK = "[REDACTED]"
REDACTED_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")
_HANDLER_NAME = "clicky-console"
_MIN_SECRET_LENGTH = 6           # shorter values would mask ordinary words

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), MASK),                                # Anthropic
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), MASK),                                 # OpenAI (sk-, sk-proj-, ...)
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=\-]{8,}"), r"\1" + MASK),         # Authorization: Bearer ...
    (re.compile(r"(?i)\b(token\s+)[a-f0-9]{32,}\b"), r"\1" + MASK),                  # Authorization: Token <hex>
    # JSON token fields, even cut short: uvicorn's DEBUG frame log shows the start of `hello` ("token":"abc...xyz")
    (re.compile(r'("(?:token|api_key|apiKey)"\s*:\s*")[^"]*'), r"\1" + MASK),
)


class RedactingFilter(logging.Filter):
    """Masks secrets in a record's message, args, exception text and stack info. Never drops a record."""

    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        super().__init__()
        self._secrets: tuple[str, ...] = ()
        self.set_secrets(secrets)

    def set_secrets(self, secrets: Iterable[str | None]) -> None:
        unique = {s for s in secrets if s and len(s) >= _MIN_SECRET_LENGTH}
        self._secrets = tuple(sorted(unique, key=len, reverse=True))   # longest first: no partial leftovers

    def add_secret(self, secret: str | None) -> None:
        self.set_secrets((*self._secrets, secret))

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, MASK)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def _redact_arg(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, int | float | bool) or value is None:
            return value
        text = str(value)
        redacted = self.redact(text)
        return redacted if redacted != text else value

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact message and args separately so formatters that read record.args (uvicorn's access formatter)
        # keep working; fall back to flattening when a secret only appears once they are combined.
        if isinstance(record.msg, str):
            record.msg = self.redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact_arg(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: self._redact_arg(value) for key, value in record.args.items()}
        try:
            message = record.getMessage()
        except Exception:     # a malformed format string: leave it to the handler's own error reporting
            message = None
        if message is not None and (redacted := self.redact(message)) != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to the *current* sys.stderr (test runners and the CLI swap it after setup)."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property  # type: ignore[override]
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, _value: Any) -> None:
        pass


_FILTER = RedactingFilter()


def get_redaction_filter() -> RedactingFilter:
    """The process-wide filter (also attached to the automation worker's stderr log)."""
    return _FILTER


def configure_logging(level: str | int = "INFO", secrets: Iterable[str | None] = ()) -> RedactingFilter:
    """Idempotent: one console handler on the root logger, redaction on every root + uvicorn handler."""
    _FILTER.set_secrets(secrets)
    root = logging.getLogger()
    numeric = level if isinstance(level, int) else logging.getLevelName(str(level).upper())
    root.setLevel(numeric if isinstance(numeric, int) else logging.INFO)

    console = next((h for h in root.handlers if h.get_name() == _HANDLER_NAME), None)
    if console is None:
        console = _StderrHandler()
        console.set_name(_HANDLER_NAME)
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        root.addHandler(console)

    for name in REDACTED_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)

    # Request lines from httpx (the Ollama client) are noise at INFO.
    logging.getLogger("httpx").setLevel(logging.DEBUG if root.level <= logging.DEBUG else logging.WARNING)
    return _FILTER
