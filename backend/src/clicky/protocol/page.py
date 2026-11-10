"""Page context captured by the extension for a turn (DOM set-of-marks + screenshot), and the budget it is cut to.

Everything in a PageContext comes from the web page and is untrusted. The hard limits below bound what any client can
send; the per-model `PageBudget` (announced in `welcome`) is what the extension actually aims for.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_ELEMENTS = 400
MAX_TEXT_CHARS = 20_000
MAX_NAME_CHARS = 200
MAX_SELECTION_CHARS = 4_000
MAX_STATES = 10
MAX_SCREENSHOT_B64_CHARS = 6_000_000      # ~4.5 MB of image bytes; budgets produce a few hundred KB

ElementState = Literal[
    "disabled", "checked", "mixed", "selected", "expanded", "collapsed", "pressed", "required", "readonly", "current",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Viewport(_Model):
    width: int = Field(ge=1)            # CSS px
    height: int = Field(ge=1)
    dpr: float = Field(default=1.0, gt=0)
    scroll_x: float = 0.0
    scroll_y: float = 0.0


class Screenshot(_Model):
    data_b64: str = Field(max_length=MAX_SCREENSHOT_B64_CHARS)  # already resized to the active model's budget
    mime: Literal["image/jpeg", "image/png", "image/webp"] = "image/jpeg"
    width: int = Field(ge=1, le=8192)   # pixel size of *this* image; element boxes and directive coordinates use it
    height: int = Field(ge=1, le=8192)


class ElementMark(_Model):
    id: str = Field(pattern=r"^e[1-9][0-9]{0,3}$")   # "e1".."eN", stable for the turn
    role: str = Field(max_length=40)                  # ARIA role or tag-derived role
    name: str = Field(default="", max_length=MAX_NAME_CHARS)  # accessible name; never an input's value
    tag: str = Field(default="", max_length=40)
    level: int | None = Field(default=None, ge=1, le=6)       # headings
    states: list[ElementState] = Field(default_factory=list)
    bbox: tuple[float, float, float, float]           # x, y, w, h in screenshot pixels (CSS px when no screenshot)

    @field_validator("states")
    @classmethod
    def _few_states(cls, value: list[str]) -> list[str]:
        # A validator, not max_length: a bounded array would generate a union of every tuple length in TypeScript.
        if len(value) > MAX_STATES:
            raise ValueError(f"at most {MAX_STATES} states")
        return value


class OpenScene(_Model):
    """The scene the companion is showing in front of the page, so a follow-up can change it."""

    scene_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    title: str = Field(default="", max_length=200)


class PageContext(_Model):
    url: str = Field(max_length=4096)
    title: str = Field(default="", max_length=500)
    viewport: Viewport
    screenshot: Screenshot | None = None
    elements: list[ElementMark] = Field(default_factory=list, max_length=MAX_ELEMENTS)
    selection: str | None = Field(default=None, max_length=MAX_SELECTION_CHARS)
    text_excerpt: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)  # budgeted visible text
    scene: OpenScene | None = None                                             #


class PageBudget(_Model):
    """How much page context the active brain gets. Sent in `welcome`; the extension cuts every snapshot to it."""

    vision: bool                                   # attach a screenshot at all
    image_long_edge: int = Field(ge=64, le=4096)   # px, never upscaled
    image_max_pixels: int = Field(ge=4096)
    jpeg_quality: float = Field(gt=0, le=1)
    max_elements: int = Field(ge=0, le=MAX_ELEMENTS)
    max_text_chars: int = Field(ge=0, le=MAX_TEXT_CHARS)
    max_name_chars: int = Field(ge=8, le=MAX_NAME_CHARS)
