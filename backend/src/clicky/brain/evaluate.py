"""`clicky eval brain`: the brain on 20 scripted pages, scored on pointing and latency.

Cases live in backend/evals/brain/cases.json; their pages (evals/brain/pages/*.html) are captured through the real
extension pipeline (DOM set-of-marks + screenshot, at the "full" and "compact" budgets) by the opt-in Playwright spec
extension/tests/e2e/eval-capture.spec.ts, which writes <pages>/<budget>/<id>.json.

Each case is one first turn of a conversation, built exactly like a voice turn's request (system prompt variant of
the model, page message, screenshot for vision models), streamed, and parsed with the same directive parser and
sentence segmenter as a voice turn. Scores:
- hit: a pointing directive (point / mark / arrow target) lands on an acceptable target - by element id, or by
  coordinates inside the target's box (multi-step answers may point at a prerequisite first); `none` cases must not
  point, `optional` ones may skip pointing;
- valid: every tag in the reply parsed and validated (no invented ids, no off-screen coordinates);
- say: at least one expected word is spoken (where the case lists any);
- latency: time to first token, to the first speakable sentence (what voice waits for), and to the end.
"""

from __future__ import annotations

import contextlib
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from clicky.brain.directives import DirectiveAt, ReplyParser, SpeechText
from clicky.capabilities.base import ChatModel, Finish, ModelInfo, TextDelta
from clicky.protocol.events import ArrowDirective, Directive, MarkDirective, PointDirective
from clicky.protocol.page import ElementMark, PageContext
from clicky.session.brain_turn import single_request
from clicky.speech.segmenter import SentenceSegmenter

CASES_FILE = Path(__file__).resolve().parents[3] / "evals" / "brain" / "cases.json"
COORD_MARGIN_PX = 8.0


@dataclass(frozen=True)
class Target:
    role: str | None = None
    name: str | None = None
    region: tuple[float, float, float, float] | None = None    # CSS px (x, y, w, h)


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    targets: tuple[Target, ...]
    optional: bool = False
    none: bool = False
    say: tuple[str, ...] = ()


def load_cases(path: Path = CASES_FILE, only: set[str] | None = None) -> list[Case]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = []
    for item in raw["cases"]:
        if only and item["id"] not in only:
            continue
        expect = item.get("expect", {})
        targets = tuple(Target(role=t.get("role"), name=t.get("name"),
                               region=tuple(t["region"]) if "region" in t else None)
                        for t in expect.get("point", []))
        cases.append(Case(item["id"], item["question"], targets, bool(expect.get("optional")),
                          bool(expect.get("none")), tuple(item.get("say", []))))
    return cases


def load_page(pages_dir: Path, budget: str, case_id: str) -> PageContext:
    path = pages_dir / budget / f"{case_id}.json"
    return PageContext.model_validate_json(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------------- scoring


def _scale(page: PageContext) -> float:
    return page.screenshot.width / page.viewport.width if page.screenshot else 1.0


def _region_px(page: PageContext, region: tuple[float, float, float, float]) -> tuple[float, ...]:
    s = _scale(page)
    return tuple(v * s for v in region)


def _inside(x: float, y: float, box: tuple[float, ...], margin: float = COORD_MARGIN_PX) -> bool:
    bx, by, bw, bh = box
    return bx - margin <= x <= bx + bw + margin and by - margin <= y <= by + bh + margin


def _element_matches(mark: ElementMark, target: Target, page: PageContext) -> bool:
    if target.region is not None:
        x, y, w, h = mark.bbox
        return _inside(x + w / 2, y + h / 2, _region_px(page, target.region), 0)
    role_ok = target.role is None or mark.role == target.role
    name_ok = target.name is None or target.name.lower() in mark.name.lower()
    return role_ok and name_ok


def _point_matches(x: float, y: float, target: Target, page: PageContext) -> bool:
    if target.region is not None:
        return _inside(x, y, _region_px(page, target.region))
    return any(_element_matches(mark, target, page) and _inside(x, y, mark.bbox) for mark in page.elements)


def _aim(directive: Directive) -> str | tuple[float, float] | None:
    if isinstance(directive, PointDirective):
        if directive.to:
            return directive.to
        return (directive.x, directive.y) if directive.x is not None and directive.y is not None else None
    ref = directive.to if isinstance(directive, MarkDirective) else (
        directive.target if isinstance(directive, ArrowDirective) else None)
    if ref and "," in ref:
        x, y = ref.split(",", 1)
        return float(x), float(y)
    return ref


def score_pointing(case: Case, page: PageContext, directives: list[Directive]) -> bool:
    aims = [aim for d in directives if (aim := _aim(d)) is not None]
    if case.none:
        return not aims
    if not aims:
        return case.optional
    return any(_aim_matches(aim, case, page) for aim in aims)     # multi-step answers ("size first, then...")


def _aim_matches(aim: str | tuple[float, float], case: Case, page: PageContext) -> bool:
    if isinstance(aim, str):
        mark = next((m for m in page.elements if m.id == aim), None)
        return mark is not None and any(_element_matches(mark, t, page) for t in case.targets)
    return any(_point_matches(aim[0], aim[1], t, page) for t in case.targets)


# ------------------------------------------------------------------------------------------------------- running


@dataclass
class CaseResult:
    id: str
    hit: bool
    valid: bool
    said: bool | None
    spoken: str
    directives: list[dict[str, Any]]
    problems: list[str]
    ttft_ms: float | None
    first_sentence_ms: float | None
    total_ms: float
    stop: str
    usage: dict[str, int | None] = field(default_factory=dict)
    error: str | None = None


async def run_case(model: ChatModel, info: ModelInfo, case: Case, page: PageContext) -> CaseResult:
    request = single_request(case.question, page, info)
    parser = ReplyParser(page, info.coord_space)
    segmenter = SentenceSegmenter()
    spoken: list[str] = []
    directives: list[Directive] = []
    started = time.perf_counter()
    ttft: float | None = None
    first_sentence: float | None = None
    finish: Finish | None = None
    error: str | None = None

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 1)

    def take(events: list[Any]) -> None:
        nonlocal first_sentence
        for event in events:
            if isinstance(event, SpeechText):
                spoken.append(event.text)
                if first_sentence is None and any(p.text for p in segmenter.push(event.text)):
                    first_sentence = elapsed()
            elif isinstance(event, DirectiveAt):
                directives.append(event.directive)

    try:
        async with contextlib.aclosing(model.stream(request)) as events:
            async for event in events:
                if isinstance(event, TextDelta) and event.text:
                    ttft = ttft if ttft is not None else elapsed()
                    take(parser.feed(event.text))
                elif isinstance(event, Finish):
                    finish = event
        take(parser.close())
        if first_sentence is None and "".join(spoken).strip():
            first_sentence = elapsed()
    except Exception as exc:          # one failing case must not end the run
        error = str(exc) or type(exc).__name__
    text = " ".join("".join(spoken).split())
    said = any(word.lower() in text.lower() for word in case.say) if case.say else None
    usage = asdict(finish.usage) if finish is not None and finish.usage is not None else {}
    return CaseResult(
        id=case.id, hit=error is None and score_pointing(case, page, directives),
        valid=error is None and parser.stats.invalid == 0 and parser.parser.dropped == 0,
        said=said, spoken=text, directives=[d.model_dump(exclude_none=True) for d in directives],
        problems=list(parser.stats.problems), ttft_ms=ttft, first_sentence_ms=first_sentence, total_ms=elapsed(),
        stop=finish.stop_reason if finish is not None else "error", usage=usage, error=error)


def _p50(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(statistics.median(present), 1) if present else None


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    said = [r.said for r in results if r.said is not None]
    usage = [r.usage for r in results if r.usage]
    return {
        "cases": len(results),
        "hits": sum(r.hit for r in results),
        "valid": sum(r.valid for r in results),
        "said": f"{sum(said)}/{len(said)}",
        "errors": sum(r.error is not None for r in results),
        "p50_ttft_ms": _p50([r.ttft_ms for r in results]),
        "p50_first_sentence_ms": _p50([r.first_sentence_ms for r in results]),
        "p50_total_ms": _p50([r.total_ms for r in results]),
        "avg_input_tokens": round(statistics.mean(u.get("input_tokens") or 0 for u in usage)) if usage else None,
        "avg_cache_read_tokens": round(statistics.mean(u.get("cache_read_tokens") or 0 for u in usage))
        if usage else None,
        "avg_cache_write_tokens": round(statistics.mean(u.get("cache_write_tokens") or 0 for u in usage))
        if usage else None,
        "avg_output_tokens": round(statistics.mean(u.get("output_tokens") or 0 for u in usage)) if usage else None,
    }
