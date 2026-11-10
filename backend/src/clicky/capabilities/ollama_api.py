"""Minimal Ollama REST client over a shared httpx.AsyncClient (no `ollama` package).

Endpoints: /api/version, /api/tags, /api/show, /api/ps, /api/chat, /api/pull, /api/create. Streaming endpoints
return NDJSON; an `{"error": ...}` line (or a non-2xx status) raises OllamaError. Streams are async generators that
own their HTTP response, so closing the generator (contextlib.aclosing) closes the upstream request at once.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

PROBE_TIMEOUT = httpx.Timeout(5.0)


class OllamaError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OllamaUnreachable(OllamaError):
    """Ollama is not listening (connection refused / connect timeout)."""


def model_tag(name: str) -> str:
    """Canonical name as listed by /api/tags: Ollama stores untagged names as `<name>:latest`."""
    tail = name.rsplit("/", 1)[-1]
    return name if ":" in tail else f"{name}:latest"


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    return f"HTTP {response.status_code}: {response.text[:200]}"


class OllamaAPI:
    def __init__(self, http: httpx.AsyncClient, host: str, *, request_timeout_s: float = 300.0,
                 cache_ttl_s: float = 2.0) -> None:
        self._http = http
        self._host = host.rstrip("/")
        self._timeout = httpx.Timeout(request_timeout_s, connect=5.0)
        self._cache_ttl_s = cache_ttl_s
        self._tags: tuple[float, list[dict[str, Any]]] | None = None
        self._show: dict[str, dict[str, Any]] = {}

    @property
    def host(self) -> str:
        return self._host

    def _url(self, path: str) -> str:
        return f"{self._host}{path}"

    def _transport_error(self, exc: httpx.TransportError) -> OllamaError:
        if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
            return OllamaUnreachable(f"Ollama unreachable at {self._host}")
        return OllamaError(f"Ollama request failed: {type(exc).__name__}: {exc}")

    async def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None,
                       timeout: httpx.Timeout | None = None) -> Any:
        try:
            response = await self._http.request(method, self._url(path), json=json_body,
                                                 timeout=timeout or PROBE_TIMEOUT)
        except httpx.TransportError as exc:
            raise self._transport_error(exc) from exc
        if response.status_code >= 400:
            raise OllamaError(_error_message(response), status_code=response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise OllamaError(f"malformed JSON from {path}") from exc

    # ----------------------------------------------------------------------------------------------- probes

    async def version(self) -> str:
        return str((await self._request("GET", "/api/version")).get("version", "?"))

    async def tags(self, *, fresh: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if not fresh and self._tags is not None and now - self._tags[0] < self._cache_ttl_s:
            return self._tags[1]
        models = list((await self._request("GET", "/api/tags")).get("models") or [])
        self._tags = (now, models)
        return models

    async def model_names(self, *, fresh: bool = False) -> set[str]:
        return {model_tag(str(m.get("model") or m.get("name"))) for m in await self.tags(fresh=fresh)}

    async def has_model(self, name: str) -> bool:
        return model_tag(name) in await self.model_names()

    async def show(self, model: str) -> dict[str, Any]:
        key = model_tag(model)
        if key not in self._show:
            self._show[key] = await self._request("POST", "/api/show", json_body={"model": model})
        return self._show[key]

    async def ps(self) -> list[dict[str, Any]]:
        return list((await self._request("GET", "/api/ps")).get("models") or [])

    def invalidate(self) -> None:
        self._tags = None
        self._show.clear()

    # ------------------------------------------------------------------------------------------- generation

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Non-streaming POST with the long request timeout (model loads can take a while)."""
        return await self._request("POST", path, json_body={**body, "stream": False}, timeout=self._timeout)

    async def stream(self, path: str, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """POST with stream=true and yield each NDJSON object."""
        try:
            async with self._http.stream("POST", self._url(path), json={**body, "stream": True},
                                         timeout=self._timeout) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise OllamaError(_error_message(response), status_code=response.status_code)
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError as exc:
                        raise OllamaError(f"malformed line from {path}: {line[:120]!r}") from exc
                    if isinstance(obj, dict) and obj.get("error"):
                        raise OllamaError(str(obj["error"]))
                    yield obj
        except httpx.TransportError as exc:
            raise self._transport_error(exc) from exc
