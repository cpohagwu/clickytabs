"""OllamaEmbedder (planned, with the optional memory feature):
POST /api/embed with `embeddinggemma` (CPU-pinned `clicky-embeddinggemma-cpu` on
cpu/gpu-4gb). Optional semantic memory.
"""

from __future__ import annotations

from clicky.capabilities._stub import StubProvider


class OllamaEmbedder(StubProvider):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        self._not_implemented()
