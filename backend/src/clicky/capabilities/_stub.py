"""Base for typed provider stubs.

A stub satisfies its capability Protocol, so the registry, `describe()` and the catalog tests treat it like any other
provider: `health()` reports it as not implemented yet (the chain then falls back to the next healthy member) and
every capability method raises NotImplementedError. Stubs never import their SDKs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from clicky.capabilities.base import Health, ProviderSpec

if TYPE_CHECKING:
    from clicky.capabilities.context import ProviderContext
    from clicky.capabilities.options import AdapterOptions


class StubProvider:
    def __init__(self, spec: ProviderSpec, *, model: str | None, options: AdapterOptions,
                 context: ProviderContext | None = None) -> None:
        self.spec = spec
        self.model = model
        self.options = options
        self._ctx = context

    async def health(self) -> Health:
        return Health(False, "not implemented yet",
                      "the chain falls back to its next healthy provider meanwhile")

    async def aclose(self) -> None:
        return None

    def _not_implemented(self) -> NoReturn:
        raise NotImplementedError(f"{self.spec.key} is not implemented yet")
