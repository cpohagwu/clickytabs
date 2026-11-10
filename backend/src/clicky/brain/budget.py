"""Per-model page-context budget: screenshot size and how much DOM and text the active brain is given.

Small local VLMs (gpu-4gb tier) pay for every image tile and element line in prefill time, so they get the compact
budget (about 1024 px long edge, as planned for local VLMs). API models get the full one.
"""

from __future__ import annotations

from clicky.capabilities.base import ModelInfo
from clicky.capabilities.kinds import Capability, Source
from clicky.capabilities.registry import Capabilities
from clicky.protocol.page import PageBudget

COMPACT = PageBudget(vision=True, image_long_edge=1024, image_max_pixels=1024 * 768, jpeg_quality=0.8,
                     max_elements=80, max_text_chars=2_000, max_name_chars=80)
# Claude image sizing: the long-standing 1568 px / ~1.15 MP guidance.
FULL = PageBudget(vision=True, image_long_edge=1568, image_max_pixels=1_150_000, jpeg_quality=0.85,
                  max_elements=150, max_text_chars=8_000, max_name_chars=100)

# Text-token targets the budgets are sized for (a worst-case page must stay under them; see tests).
TEXT_TOKEN_TARGET = {"compact": 3_000, "full": 8_000}


def active_model_info(capabilities: Capabilities, capability: Capability = Capability.BRAIN) -> ModelInfo | None:
    """ModelInfo of the chain's active member, or None when it has no instantiated chat provider."""
    chain = capabilities.chains.get(capability)
    if chain is None:
        return None
    provider = chain.active.provider
    info = getattr(provider, "info", None)
    return info() if callable(info) else None


def page_budget(capabilities: Capabilities) -> PageBudget:
    chain = capabilities.chains.get(Capability.BRAIN)
    if chain is None:
        return COMPACT.model_copy(update={"vision": False})
    base = FULL if chain.active.spec.source is Source.API else COMPACT
    info = active_model_info(capabilities)
    # Unknown vision (provider not instantiated, e.g. missing key) -> no screenshot: DOM + text still help.
    return base.model_copy(update={"vision": bool(info and info.vision)})
