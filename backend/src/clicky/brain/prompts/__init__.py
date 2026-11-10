"""Brain system prompts: `full` (API and big local models) and `compact` (small local models, few-shot).

The system prompt is a pure function of the answering model's ModelInfo, so it is byte-identical on every turn of a
conversation: Claude's prompt cache and preserved thinking (append-only history) both depend on that. Everything that
changes per turn (page, utterance) goes into the newest user message instead.
"""

from __future__ import annotations

from functools import cache
from importlib import resources

from clicky.capabilities.base import ModelInfo, PromptVariant

_UNKNOWN = ModelInfo(name="unknown")

_NO_ID = "- If something has no element id but you can see it in the screenshot, point at its center with x and y"
_COORDINATES = {
    "image_px": f'{_NO_ID} in screenshot pixels (page_context gives the screenshot size): '
                '<point x="640" y="212" label="chart peak"/>',
    "norm1000": f"{_NO_ID} from 0 to 1000 across the screenshot's width and height (0,0 is the top left): "
                '<point x="512" y="300" label="chart peak"/>',
    "norm1": f"{_NO_ID} from 0 to 1 across the screenshot's width and height (0,0 is the top left): "
             '<point x="0.5" y="0.3" label="chart peak"/>',
}
_NO_COORDINATES = "- You can only point at elements listed in page_context."


@cache
def template(variant: PromptVariant) -> str:
    return resources.files(__package__).joinpath(f"{variant}.md").read_text(encoding="utf-8")


def coordinates_rule(info: ModelInfo) -> str:
    if info.vision and info.coord_space is not None:
        return _COORDINATES[info.coord_space]
    return _NO_COORDINATES


def system_prompt(info: ModelInfo | None) -> str:
    info = info or _UNKNOWN
    return template(info.prompt).replace("{{COORDINATES}}", coordinates_rule(info)).strip()
