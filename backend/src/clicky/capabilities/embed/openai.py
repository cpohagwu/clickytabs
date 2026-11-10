"""OpenAIEmbedder (planned, with the optional memory feature):
`text-embedding-3-small` via the `openai` SDK, imported lazily."""

from __future__ import annotations

from clicky.capabilities._stub import StubProvider


class OpenAIEmbedder(StubProvider):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        self._not_implemented()
