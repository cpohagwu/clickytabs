"""Live check against the local Ollama (run with: uv run pytest -m live). Skipped when Ollama or the model is absent."""

from __future__ import annotations

import httpx
import pytest

from clicky.capabilities.base import ChatMessage, ChatRequest, Finish, TextDelta, TextPart
from clicky.capabilities.context import ProviderContext
from clicky.capabilities.kinds import Capability
from clicky.capabilities.llm.ollama import OllamaChat
from clicky.capabilities.registry import resolve_member
from clicky.config import ProviderChoice, Settings

MODEL = "qwen2.5vl:3b"
pytestmark = pytest.mark.live


async def test_streams_a_short_answer() -> None:
    settings = Settings(profile="local")
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=3.0), trust_env=False) as http:
        try:
            tags = (await http.get(f"{settings.ollama.host}/api/tags")).json()
        except httpx.HTTPError:
            pytest.skip("Ollama is not reachable")
        if MODEL not in {m["name"] for m in tags.get("models", [])}:
            pytest.skip(f"{MODEL} is not pulled")
        member = resolve_member(Capability.BRAIN, ProviderChoice(provider="ollama", model=MODEL), "live")
        chat = OllamaChat(member.spec, model=MODEL, options=member.options,
                          context=ProviderContext.create(settings, http))
        assert (await chat.health()).ok
        request = ChatRequest(system="Answer in at most five words.",
                              messages=(ChatMessage("user", (TextPart("Say hello."),)),), max_tokens=32)
        events = [event async for event in chat.stream(request)]
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text.strip()
    assert isinstance(events[-1], Finish) and events[-1].stop_reason in ("end", "max_tokens")
