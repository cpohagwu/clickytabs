"""Streaming directive parser + validator.

The brain writes visual directives inline, in plain text, so any model can use them:

    The <point to="e17" label="Add to cart"/>Add to cart button is under the price.

`DirectiveParser` splits the streamed reply into speech text and tags. It is chunk-boundary safe (a tag may arrive one
character at a time), tags are never spoken, and `at_char` records where in the spoken text each tag sat, so the
client can fire it while the voice says the words after it.

- inline tags: <point/>, <mark/>, <arrow/>, <path/>, <scroll/>, <clear/>; a missing "/" is tolerated, stray closing
  tags are dropped;
- block tags: <scene ...>brief</scene> and <task ...>goal</task> are captured whole, never spoken;
- other tag-shaped text: HTML-ish formatting tags (<b>, </p>, <br/>, <thinking>, ...) and any tag with attributes are
  dropped; a bare unknown word tag such as <Enter> is spoken as its word; a "<" that starts no tag is plain text.

`DirectiveValidator` turns raw tags into protocol directives checked against the turn's page: element ids must exist
in the newest snapshot, coordinates are converted from the model's convention (ModelInfo.coord_space) to screenshot
pixels and must lie on the screenshot. Anything invalid is dropped and counted (the brain eval scores it).
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from clicky.capabilities.base import CoordSpace
from clicky.protocol.events import (
    ArrowDirective,
    ClearDirective,
    Directive,
    MarkDirective,
    PathDirective,
    PointDirective,
    ScrollDirective,
)
from clicky.protocol.page import PageContext

log = logging.getLogger(__name__)

INLINE_TAGS = frozenset({"point", "mark", "arrow", "path", "scroll", "clear"})
BLOCK_TAGS = frozenset({"scene", "task"})
KNOWN_TAGS = INLINE_TAGS | BLOCK_TAGS
# Formatting/wrapper tags some models emit around (or inside) an answer: dropped without a word.
SILENT_TAGS = frozenset({
    "a", "answer", "b", "br", "code", "div", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "kbd", "li", "ol",
    "p", "pre", "reply", "response", "s", "small", "span", "speak", "speech", "strong", "sub", "sup", "thinking", "u",
    "ul",
})
MAX_TAG_CHARS = 400                       # "<point ... />" longer than this is malformed
MAX_UNKNOWN_TAG_CHARS = 40                # "<b>", "<Enter>"; a longer "<word ..." without ">" is plain text
MAX_BLOCK_CHARS = {"scene": 60_000, "task": 2_000}
MAX_DIRECTIVES_PER_TURN = 8
MAX_LABEL_CHARS = 60
MAX_PATH_POINTS = 64

_TAG_START = re.compile(r"<(/?)([A-Za-z][\w-]*)")
_ATTR = re.compile(r"""([A-Za-z_][\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`/]+))""")
_XY = re.compile(r"^\s*\(?\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*\)?\s*$")
_ELEMENT_ID = re.compile(r"^\s*[#\[]?\s*[eE]([1-9]\d{0,3})\s*\]?\s*$")
MARK_STYLES = ("circle", "box", "underline", "glow")

# ------------------------------------------------------------------------------------------------------ parsing


@dataclass(frozen=True)
class SpeechText:
    text: str


@dataclass(frozen=True)
class RawTag:
    """An inline tag as the model wrote it; `at_char` = spoken characters before it in the whole reply."""

    at_char: int
    name: str
    attrs: dict[str, str]


@dataclass(frozen=True)
class RawBlock:
    at_char: int
    name: str
    attrs: dict[str, str]
    body: str


ParserEvent = SpeechText | RawTag | RawBlock


def parse_attrs(text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR.finditer(text):
        value = next(v for v in match.groups()[1:] if v is not None)
        attrs.setdefault(match.group(1).lower(), html.unescape(value))
    return attrs


class DirectiveParser:
    def __init__(self) -> None:
        self._buffer = ""
        self._spoken = 0                  # characters of speech text emitted so far
        self._block: tuple[str, dict[str, str], int] | None = None   # (name, attrs, at_char) of an open block tag
        self.dropped = 0                  # malformed or oversized tags

    @property
    def spoken_chars(self) -> int:
        return self._spoken

    def feed(self, chunk: str) -> list[ParserEvent]:
        self._buffer += chunk
        return self._drain(final=False)

    def close(self) -> list[ParserEvent]:
        events = self._drain(final=True)
        if self._block is not None:
            log.debug("unterminated <%s> block dropped", self._block[0])
            self.dropped += 1
            self._block = None
            self._buffer = ""
        rest, self._buffer = self._buffer, ""
        if rest and not _TAG_START.match(rest):
            events.append(self._text(rest))
        elif rest:
            self.dropped += 1             # a tag cut off by the end of the reply
        return events

    def _text(self, text: str) -> SpeechText:
        self._spoken += len(text)
        return SpeechText(text)

    def _drain(self, *, final: bool) -> list[ParserEvent]:
        events: list[ParserEvent] = []
        while self._buffer:
            if self._block is not None:
                if not self._close_block(events):
                    break
                continue
            start = self._buffer.find("<")
            if start == -1:
                events.append(self._text(self._buffer))
                self._buffer = ""
                break
            if start:
                events.append(self._text(self._buffer[:start]))
                self._buffer = self._buffer[start:]
            if not self._tag(events, final=final):
                break
        return events

    def _close_block(self, events: list[ParserEvent]) -> bool:
        """Consume an open block's body; False = wait for more input."""
        assert self._block is not None
        name, attrs, at_char = self._block
        end = re.search(rf"</\s*{name}\s*>", self._buffer, re.IGNORECASE)
        if end is None:
            if len(self._buffer) > MAX_BLOCK_CHARS[name]:
                log.debug("<%s> block over %d chars dropped", name, MAX_BLOCK_CHARS[name])
                self.dropped += 1
                self._block = None
                self._buffer = ""
            return False
        events.append(RawBlock(at_char, name, attrs, self._buffer[:end.start()]))
        self._buffer = self._buffer[end.end():]
        self._block = None
        return True

    def _tag(self, events: list[ParserEvent], *, final: bool) -> bool:
        """The buffer starts with "<". Consume one tag or one literal "<"; False = wait for more input."""
        buffer = self._buffer
        match = _TAG_START.match(buffer)
        if match is None:
            if len(buffer) < 3 and not final and re.fullmatch(r"</?", buffer):
                return False                       # "<" or "</" at a chunk end: the name may follow
            events.append(self._text("<"))
            self._buffer = buffer[1:]
            return True
        name = match.group(2).lower()
        if match.end() == len(buffer) and not final:
            return False                           # the name itself may still be growing ("<po" -> "<point")
        end = buffer.find(">", match.end())
        inner_lt = buffer.find("<", match.end())
        if inner_lt != -1 and (end == -1 or inner_lt < end):    # a tag never contains "<": this one is broken off
            if name in KNOWN_TAGS:
                self.dropped += 1
                self._buffer = buffer[inner_lt:]
            else:
                events.append(self._text("<"))
                self._buffer = buffer[1:]
            return True
        if end == -1:
            # Known tags may be long ("<point ... label=...>"); anything else must close soon, or it was plain text.
            limit = MAX_TAG_CHARS if name in KNOWN_TAGS else MAX_UNKNOWN_TAG_CHARS
            if len(buffer) <= limit and not final:
                return False
            if name in KNOWN_TAGS:                 # malformed known tag: speak nothing of it
                self.dropped += 1
                self._buffer = "" if final else buffer[match.end():]   # cut off by the reply's end: all of it
            else:
                events.append(self._text("<"))     # not a tag after all ("<x and y")
                self._buffer = buffer[1:]
            return True
        inner = buffer[match.end():end]
        self._buffer = buffer[end + 1:]
        closing = match.group(1) == "/"
        if name not in KNOWN_TAGS:
            if not closing and name not in SILENT_TAGS and not inner.strip(" /"):
                events.append(self._text(match.group(2)))    # "<Enter>" -> "Enter"
            return True
        if closing:
            return True                                     # "</point>": nothing to do
        attrs = parse_attrs(inner)
        self_closing = inner.rstrip().endswith("/")
        if name in BLOCK_TAGS:
            if self_closing:
                events.append(RawBlock(self._spoken, name, attrs, ""))
            else:
                self._block = (name, attrs, self._spoken)
            return True
        events.append(RawTag(self._spoken, name, attrs))
        return True


# --------------------------------------------------------------------------------------------------- validation


@dataclass(frozen=True)
class TaskRequest:
    """A validated <task>. Cookie scope never comes from the model."""

    goal: str
    start_url: str | None
    session: bool
    cross_site: bool


@dataclass(frozen=True)
class SceneRequest:
    """A validated <scene>: the brain's brief for the codegen model, which writes the code (brain/scenes.py)."""

    kind: str
    title: str
    brief: str


@dataclass
class DirectiveStats:
    total: int = 0
    valid: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def invalid(self) -> int:
        return self.total - self.valid


def _label(attrs: dict[str, str]) -> str | None:
    label = " ".join((attrs.get("label") or "").split())
    return label[:MAX_LABEL_CHARS] or None


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value.strip().rstrip("px"))
    except ValueError:
        return None
    return number if number == number and abs(number) < 1e6 else None     # no NaN / absurd values


class DirectiveValidator:
    """Checks raw tags against the turn's page and converts coordinates to screenshot pixels."""

    def __init__(self, page: PageContext | None, coord_space: CoordSpace | None, *,
                 max_directives: int = MAX_DIRECTIVES_PER_TURN) -> None:
        self.stats = DirectiveStats()
        self._ids = {mark.id for mark in page.elements} if page else set()
        self._page = page
        self._coord_space = coord_space if page is not None else None
        self._max = max_directives
        if page is not None and page.screenshot is not None:
            self._width, self._height = float(page.screenshot.width), float(page.screenshot.height)
        elif page is not None:
            self._width, self._height = float(page.viewport.width), float(page.viewport.height)
        else:
            self._width = self._height = 0.0

    # ---------------------------------------------------------------------------------------------- refs

    def element(self, ref: str | None) -> str | None:
        """A known element id ("e17"), tolerating "#e17", "[e17]" and "E17"."""
        match = _ELEMENT_ID.match(ref or "")
        if match is None:
            return None
        ident = f"e{match.group(1)}"
        return ident if ident in self._ids else None

    def point(self, x: float | None, y: float | None) -> tuple[float, float] | None:
        """Model coordinates -> screenshot pixels, or None if the model can't point by coordinates / off-image."""
        if x is None or y is None or self._coord_space is None or not self._width:
            return None
        scale = {"image_px": (1.0, 1.0), "norm1000": (self._width / 1000, self._height / 1000),
                 "norm1": (self._width, self._height)}[self._coord_space]
        px, py = x * scale[0], y * scale[1]
        slack_x, slack_y = self._width * 0.02, self._height * 0.02
        if not (-slack_x <= px <= self._width + slack_x and -slack_y <= py <= self._height + slack_y):
            return None
        return (round(min(max(px, 0.0), self._width - 1), 1), round(min(max(py, 0.0), self._height - 1), 1))

    def _xy_ref(self, ref: str | None) -> tuple[float, float] | None:
        match = _XY.match(ref or "")
        return self.point(float(match.group(1)), float(match.group(2))) if match else None

    def ref(self, ref: str | None) -> str | None:
        """An element id or "x,y" (screenshot px) as the overlay's resolveRef expects."""
        if element := self.element(ref):
            return element
        if xy := self._xy_ref(ref):
            return f"{xy[0]:g},{xy[1]:g}"
        return None

    # --------------------------------------------------------------------------------------------- checks

    def _reject(self, tag: str, why: str) -> None:
        self.stats.problems.append(f"<{tag}>: {why}")

    def check(self, tag: RawTag) -> Directive | None:
        self.stats.total += 1
        if self.stats.valid >= self._max:
            self._reject(tag.name, f"more than {self._max} directives in one reply")
            return None
        directive = self._build(tag)
        if directive is not None:
            self.stats.valid += 1
        return directive

    def _build(self, tag: RawTag) -> Directive | None:
        attrs = tag.attrs
        name = tag.name
        if name == "clear":
            return ClearDirective()
        if self._page is None:
            self._reject(name, "no page context this turn")
            return None
        if name == "point":
            if element := self.element(attrs.get("to")):
                return PointDirective(to=element, label=_label(attrs))
            xy = self.point(_number(attrs.get("x")), _number(attrs.get("y"))) or self._xy_ref(attrs.get("to"))
            if xy is not None:
                return PointDirective(x=xy[0], y=xy[1], label=_label(attrs))
            self._reject(name, self._why(attrs.get("to"), attrs.get("x")))
            return None
        if name == "mark":
            target = self.ref(attrs.get("to"))
            if target is None:
                self._reject(name, self._why(attrs.get("to")))
                return None
            style = (attrs.get("style") or "circle").strip().lower()
            return MarkDirective(to=target, style=style if style in MARK_STYLES else "circle",  # type: ignore[arg-type]
                                 label=_label(attrs))
        if name == "arrow":
            source = self.ref(attrs.get("from") or attrs.get("source"))
            target = self.ref(attrs.get("to") or attrs.get("target"))
            if source is None or target is None:
                self._reject(name, "unknown or missing from/to")
                return None
            return ArrowDirective(source=source, target=target, label=_label(attrs))
        if name == "path":
            raw = re.findall(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", attrs.get("points") or "")
            points = [self.point(float(x), float(y)) for x, y in raw[:MAX_PATH_POINTS]]
            if len(points) < 2 or any(p is None for p in points):
                self._reject(name, "needs at least two on-screen points")
                return None
            return PathDirective(points=[p for p in points if p is not None], label=_label(attrs))
        if name == "scroll":
            if element := self.element(attrs.get("to")):
                return ScrollDirective(to=element)
            self._reject(name, self._why(attrs.get("to")))
            return None
        self._reject(name, "unknown directive")
        return None

    def _why(self, ref: str | None, x: str | None = None) -> str:
        if ref and _ELEMENT_ID.match(ref):
            return f"{ref.strip()} is not an element of this page"
        if (ref and _XY.match(ref)) or x is not None:
            return "coordinates off the screenshot" if self._coord_space else "this model cannot point by coordinates"
        return "missing target"

    # --------------------------------------------------------------------------------------------- blocks

    def block(self, block: RawBlock) -> TaskRequest | SceneRequest | None:
        if block.name == "task":
            return self.task(block)
        kind = (block.attrs.get("kind") or "").strip().lower()
        title = " ".join((block.attrs.get("title") or "").split())[:80]
        brief = block.body.strip()
        if kind not in ("three", "canvas", "svg", "html") or not (brief or title):
            self._reject("scene", "needs kind three|canvas|svg|html and a brief")
            return None
        return SceneRequest(kind, title, brief)

    def task(self, block: RawBlock) -> TaskRequest | None:
        goal = " ".join(block.body.split())
        if not 3 <= len(goal) <= 500:
            self._reject("task", "the goal must be 3-500 characters")
            return None
        start_url = (block.attrs.get("start_url") or "").strip() or None
        target_host = None
        if start_url is not None:
            parts = urlsplit(start_url)
            if parts.scheme not in ("http", "https") or not parts.hostname or len(start_url) > 2000:
                self._reject("task", "start_url must be an http(s) URL")
                return None
            target_host = parts.hostname
        session = (block.attrs.get("session") or "no").strip().lower() in ("yes", "true", "1")
        tab_host = urlsplit(self._page.url).hostname if self._page is not None else None
        # Any host change counts as cross-site here; the extension scopes cookies by registrable domain.
        cross_site = target_host is not None and _bare(target_host) != _bare(tab_host)
        return TaskRequest(goal=goal, start_url=start_url, session=session, cross_site=cross_site)


def _bare(host: str | None) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------------------------- combined stream


@dataclass(frozen=True)
class DirectiveAt:
    at_char: int
    directive: Directive


ReplyEvent = SpeechText | DirectiveAt | TaskRequest | SceneRequest


LABEL_HOLD_CHARS = 24                     # speech held after a labelled tag while deciding whether its name follows
_CONNECTORS = frozenset({"or", "and", "to", "then", "for"})
_ACTIONS = frozenset({"click", "press", "tap", "select", "choose", "use", "open", "hit", "pick", "try"})
_FIRST_WORD = re.compile(r"[A-Za-z']+")
_WORDS = re.compile(r"[A-Za-z0-9']+")


class ReplyParser:
    """Parser + validator: brain deltas in, speech text and validated directives out.

    Small models sometimes write a tag *instead of* the words ("Click <point label="Close"/>."), which would leave a
    hole in the speech. After a labelled point/mark, the next few characters are held; if what follows is clearly not
    the thing's name - punctuation, the end of the reply, a connector such as "or"/"to", or, right after an action verb
    ("Click <tag/>Right above it"), words that share none of the label's - the label is spoken in its place. Models
    that name the thing after the tag (as the prompts ask) are never changed.
    """

    def __init__(self, page: PageContext | None, coord_space: CoordSpace | None) -> None:
        self.parser = DirectiveParser()
        self.validator = DirectiveValidator(page, coord_space)
        self.raw: list[str] = []          # the reply exactly as written (tags included): history replays it
        self.spoken_labels = 0            # labels spoken in place of missing words
        self._label: str | None = None    # label of the last tag, while deciding whether its name follows
        self._held: list[str] = []
        self._shift = 0                   # characters of injected labels: later directives move right
        self._tail = ""                   # last speech character emitted
        self._recent = ""                 # the last few dozen characters of speech emitted
        self._squeeze = False             # after a removed <scene>/<task> block: drop the doubled whitespace

    @property
    def stats(self) -> DirectiveStats:
        return self.validator.stats

    @property
    def raw_text(self) -> str:
        return "".join(self.raw)

    def feed(self, delta: str) -> list[ReplyEvent]:
        self.raw.append(delta)
        return self._validated(self.parser.feed(delta), final=False)

    def close(self) -> list[ReplyEvent]:
        return self._validated(self.parser.close(), final=True)

    def _validated(self, events: list[ParserEvent], *, final: bool) -> list[ReplyEvent]:
        out: list[ReplyEvent] = []
        for event in events:
            if isinstance(event, SpeechText):
                if event.text:
                    self._speech(event.text, out)
            elif isinstance(event, RawTag):
                if (directive := self.validator.check(event)) is not None:
                    self._settle(out)
                    out.append(DirectiveAt(event.at_char + self._shift, directive))
                    label = getattr(directive, "label", None)
                    if isinstance(directive, PointDirective | MarkDirective) and label:
                        self._label = label
            else:
                request = self.validator.block(event)
                self._settle(out)
                self._squeeze = True      # "Sure. <scene>…</scene> It's coming." -> "Sure. It's coming."
                if request is not None:
                    out.append(request)
        if final:
            self._settle(out)
        return out

    def _speech(self, text: str, out: list[ReplyEvent]) -> None:
        if self._label is None:
            self._emit(text, out)
            return
        self._held.append(text)
        held = "".join(self._held)
        if len(held.strip()) >= LABEL_HOLD_CHARS or re.search(r"[.!?]", held):
            self._settle(out)

    def _settle(self, out: list[ReplyEvent]) -> None:
        """Decide whether the held tag's name was spoken; flush the held speech."""
        label, held = self._label, "".join(self._held)
        self._label, self._held = None, []
        if label is None:
            return
        after = held.lstrip()
        word = _FIRST_WORD.match(after)
        verb = re.search(r"([A-Za-z]+)\W*$", self._recent)
        label_words = {w.lower() for w in _WORDS.findall(label) if len(w) >= 3}
        unnamed = (verb is not None and verb.group(1).lower() in _ACTIONS and word is not None
                   and word.group()[0].isupper()          # a new phrase; "Click <tag/>the X" names it in its words
                   and not label_words & {w.lower() for w in _WORDS.findall(after)[:3]})
        if (not after or after[0] in ".,;:!?)" or (word is not None and word.group().lower() in _CONNECTORS)
                or unnamed):
            spoken = ("" if not self._tail or self._tail.isspace() else " ") + label
            if unnamed:
                spoken += "." + ("" if held[0].isspace() else " ")      # "Click Close. Right above it."
            elif after and not held[0].isspace() and after[0] not in ".,;:!?)":
                spoken += " "
            self._shift += len(spoken)
            self.spoken_labels += 1
            self._emit(spoken, out)
        if held:
            self._emit(held, out)

    def _emit(self, text: str, out: list[ReplyEvent]) -> None:
        if self._squeeze:
            if not self._tail or self._tail.isspace():
                kept = text.lstrip()
                self._shift -= len(text) - len(kept)          # later directives move left with the text
                text = kept
                if not text:
                    return                                    # still only whitespace: keep squeezing
            self._squeeze = False
        out.append(SpeechText(text))
        self._tail = text[-1]
        self._recent = (self._recent + text)[-48:]
