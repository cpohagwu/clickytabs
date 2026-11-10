"""OllamaChat request shape + streaming + cancellation, and ModelManager pulls, against httpx.MockTransport."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from clicky.capabilities.base import ChatMessage, ChatRequest, Finish, ImagePart, TextDelta, TextPart
from clicky.capabilities.context import ProviderContext
from clicky.capabilities.kinds import Capability, HardwareTier
from clicky.capabilities.llm.ollama import OllamaChat
from clicky.capabilities.models import ModelManager, PullProgress
from clicky.capabilities.ollama_api import OllamaAPI
from clicky.capabilities.registry import ConfigError, build_capabilities, resolve_member
from clicky.config import ProviderChoice, RunnerOptions, Settings

HOST = "http://ollama.test:11434"


def _settings(**kw: object) -> Settings:
    return Settings(profile="local", ollama={"host": HOST, "runner": {"m:1b": RunnerOptions(num_ctx=4096)}}, **kw)


class _Stream(httpx.AsyncByteStream):
    """NDJSON body; records whether the client closed it (cancellation) and supports per-line delays."""

    def __init__(self, lines: list[dict], delay_s: float = 0.0) -> None:
        self.lines, self.delay_s, self.closed = lines, delay_s, False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for line in self.lines:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            yield (json.dumps(line) + "\n").encode()

    async def aclose(self) -> None:
        self.closed = True


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _chat(settings: Settings, http: httpx.AsyncClient, options: dict | None = None) -> OllamaChat:
    member = resolve_member(Capability.BRAIN, ProviderChoice(provider="ollama", model="m:1b",
                                                             options=options or {}), "test")
    return OllamaChat(member.spec, model="m:1b", options=member.options,
                      context=ProviderContext.create(settings, http))


def _request() -> ChatRequest:
    return ChatRequest(system="sys", messages=(ChatMessage("user", (ImagePart(b"\x89PNG", "image/png"),
                                                                     TextPart("hi"))),), max_tokens=77)


async def test_request_body_puts_think_and_keep_alive_top_level() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, stream=_Stream([
            {"message": {"content": "Hel"}, "done": False},
            {"message": {"content": "lo"}, "done": False},
            {"message": {"content": ""}, "done": True, "done_reason": "stop", "eval_count": 2,
             "prompt_eval_count": 9},
        ]))

    async with _client(handler) as http:
        chat = _chat(_settings(), http, {"think": False, "keep_alive": "30m", "temperature": 0.2})
        events = [event async for event in chat.stream(_request())]

    assert seen["think"] is False and seen["keep_alive"] == "30m" and seen["stream"] is True
    assert seen["options"] == {"num_ctx": 4096, "temperature": 0.2, "num_predict": 77}
    assert "think" not in seen["options"] and "keep_alive" not in seen["options"]
    assert seen["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["messages"][1]["images"] == ["iVBORw=="] and seen["messages"][1]["content"] == "hi"
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
    finish = events[-1]
    assert isinstance(finish, Finish) and finish.stop_reason == "end" and finish.usage.output_tokens == 2
    assert finish.native_turn == {"role": "assistant", "content": "Hello"}


@pytest.mark.parametrize("bad", [{"num_ctx": 8192}, {"unknown_key": 1}])
def test_runner_and_unknown_keys_are_rejected_at_startup(bad: dict) -> None:
    with pytest.raises(ConfigError):
        resolve_member(Capability.BRAIN, ProviderChoice(provider="ollama", model="m:1b", options=bad), "test")


async def test_length_maps_to_max_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Stream([{"message": {"content": "x"}, "done": True,
                                                    "done_reason": "length"}]))

    async with _client(handler) as http:
        events = [e async for e in _chat(_settings(), http).stream(_request())]
    assert events[-1].stop_reason == "max_tokens"


async def test_cancel_closes_the_upstream_stream() -> None:
    body = _Stream([{"message": {"content": f"{i} "}, "done": False} for i in range(100)], delay_s=0.01)

    async with _client(lambda request: httpx.Response(200, stream=body)) as http:
        chat = _chat(_settings(), http)

        async def consume() -> None:
            async with contextlib.aclosing(chat.stream(_request())) as events:
                async for _ in events:
                    pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert body.closed


async def test_health_reports_missing_model_with_pull_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "other:1b"}]})

    async with _client(handler) as http:
        health = await _chat(_settings(), http).health()
    assert not health.ok and "not pulled" in health.reason and "ollama pull m:1b" in health.hint


async def test_unhealthy_brain_never_crashes_startup() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with _client(handler) as http:
        caps = await build_capabilities(_settings(), http=http, tier=HardwareTier.GPU_4GB)
        status = {s.capability: s for s in caps.describe()}
        await caps.aclose()
    assert not status["brain"].ok and status["brain"].hint


async def test_pull_reports_progress_and_requires_success() -> None:
    lines = [{"status": "pulling manifest"},
             {"status": "pulling abc", "digest": "sha256:abc", "total": 100, "completed": 40},
             {"status": "success"}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"model": "m:1b", "stream": True}
        return httpx.Response(200, stream=_Stream(lines))

    progress: list[PullProgress] = []
    async with _client(handler) as http:
        manager = ModelManager(_settings(), OllamaAPI(http, HOST))
        await manager.pull_ollama("m:1b", progress.append)
    assert [p.status for p in progress] == ["pulling manifest", "pulling abc", "success"]
    assert (progress[1].completed, progress[1].total) == (40, 100)


async def test_pull_without_success_fails() -> None:
    async with _client(lambda r: httpx.Response(200, stream=_Stream([{"status": "pulling"}]))) as http:
        manager = ModelManager(_settings(), OllamaAPI(http, HOST))
        with pytest.raises(Exception, match="ended with status"):
            await manager.pull_ollama("m:1b")
