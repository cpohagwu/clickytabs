"""WebSocket handshake checks: Origin allow-list, extension-ID binding, constant-time token comparison."""

from __future__ import annotations

import hmac
import re
from collections.abc import Collection
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clicky.config import Settings

EXTENSION_SCHEME = "chrome-extension://"
_EXTENSION_ID = re.compile(r"[a-p]{32}")


def extension_id_from_origin(origin: str | None) -> str | None:
    """The ID in an exact `chrome-extension://<32 chars a-p>` Origin, else None (no path, port or trailing slash)."""
    if not origin or not origin.startswith(EXTENSION_SCHEME):
        return None
    candidate = origin[len(EXTENSION_SCHEME):]
    return candidate if _EXTENSION_ID.fullmatch(candidate) else None


def origin_allowed(origin: str | None, allowed_ids: Collection[str]) -> bool:
    """Only extension origins; when `allowed_ids` is non-empty the ID must be one of them."""
    extension_id = extension_id_from_origin(origin)
    if extension_id is None:
        return False
    return not allowed_ids or extension_id in {i.strip().lower() for i in allowed_ids}


def extension_id_matches(hello_id: str | None, origin: str | None) -> bool:
    """The ID claimed in `hello` must be the one the browser put in the Origin header."""
    extension_id = extension_id_from_origin(origin)
    return extension_id is not None and hello_id == extension_id


def token_matches(presented: str | None, expected: str | None) -> bool:
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


class TokenStore:
    """The pairing token the handshake checks. CLICKY_AUTH_TOKEN is fixed; otherwise the token file is re-read
    whenever it changes, so `clicky token rotate` takes effect on a running backend (the watcher in main.py then
    closes every session with 4401). A deleted or emptied file keeps the last token rather than locking everyone out."""

    def __init__(self, settings: Settings) -> None:
        self._path = settings.token_path
        fixed = settings.auth_token.get_secret_value() if settings.auth_token is not None else ""
        self._fixed = fixed or None
        self._value = settings.resolved_token()
        self._stamp = self._stat()

    def _stat(self) -> tuple[int, int] | None:
        try:
            info = self._path.stat()
        except OSError:
            return None
        return info.st_mtime_ns, info.st_size

    @property
    def rotatable(self) -> bool:
        return self._fixed is None

    def current(self) -> str:
        if self._fixed is not None:
            return self._fixed
        stamp = self._stat()
        if stamp != self._stamp:
            self._stamp = stamp
            try:
                value = self._path.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            if value:
                self._value = value
        return self._value
