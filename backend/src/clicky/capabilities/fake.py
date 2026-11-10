"""Deterministic fake providers for tests, the e2e suite and offline demos (profile = "fake").

FakeChat echoes the newest user text in word-sized deltas (`options.deltas` replaces the script). Markers in the user
text change the script, so e2e tests can drive edge cases through the real UI:

- `[slow]`     60 deltas, 0.1 s apart (time to press Cancel)
- `[error]`    fails before the first delta (provider_error path)
- `[refusal]`  ends with stop_reason="refusal"
- `[page]`     describes the page context that arrived
- `[point]`    points at the page's first button or link with a <point/> tag split across deltas
- `[history]`  says how many earlier turns the request carried
- `[task]`     proposes a browser task (session="yes") with the rest of the message as its goal
- `[scene]`, `[scene:svg]`, `[scene:html|A title]`  asks for a scene of that kind (default three) with a <scene> tag
               whose brief is the rest of the message (the scene eval drives real codegen models this way); as the
               codegen model (purpose "codegen") it writes a small working scene of the requested kind, or - with
               `[broken]` in the brief - code that fails at runtime until the repair turn
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import re
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from clicky.capabilities.base import (
    AgentLLMConfigOut,
    AudioChunk,
    ChatEvent,
    ChatRequest,
    Finish,
    Health,
    ImagePart,
    ModelInfo,
    Partial,
    ProviderSpec,
    SpeechEnded,
    SpeechStarted,
    StopReason,
    STTEvent,
    TextDelta,
    TextPart,
    Transcript,
    Usage,
    UtteranceEnd,
)
from clicky.capabilities.options import (
    FakeAgentOptions,
    FakeChatOptions,
    FakeEmbedOptions,
    FakeSTTOptions,
    FakeTTSOptions,
)

if TYPE_CHECKING:
    from clicky.capabilities.context import ProviderContext

SLOW_MARKER = "[slow]"
ERROR_MARKER = "[error]"
REFUSAL_MARKER = "[refusal]"
PAGE_MARKER = "[page]"          # reply with what arrived of the page context (e2e checks the page-context pipeline)
POINT_MARKER = "[point]"
HISTORY_MARKER = "[history]"
SCENE_MARKER = re.compile(r"\[scene(?::(three|canvas|svg|html))?(?:\|([^\]\n]{1,80}))?\]")
TASK_MARKER = "[task]"          # proposes a browser task (session="yes") whose goal is the rest of the message
BROKEN_MARKER = "[broken]"
NOCODE_MARKER = "[nocode]"      # codegen answers with prose instead of code until the repair turn
_MARKERS = (SLOW_MARKER, ERROR_MARKER, REFUSAL_MARKER)
_ELEMENT_LINE = re.compile(r'^(e\d+) ([a-z]+)\d? "([^"]*)"', re.MULTILINE)
_KIND_LINE = re.compile(r"^Kind: (three|canvas|svg|html)$", re.MULTILINE)

# Scenes the fake codegen model "writes" (small, valid, visibly non-blank in the real sandbox).
FAKE_SCENES = {
    "three": "\n".join((
        "const cube = new THREE.Mesh(new THREE.BoxGeometry(2, 2, 2),",
        "  new THREE.MeshStandardMaterial({ color: 0x7c5cff, roughness: 0.4 }));",
        "scene.add(cube);",
        "addLabel('Fake cube', [0, 1.9, 0]);",
        "onFrame((dt) => { cube.rotation.y += dt; cube.rotation.x += dt * 0.4; });",
    )),
    "canvas": "\n".join((
        "onFrame((dt, t) => {",
        "  g.clearRect(0, 0, width, height);",
        "  g.strokeStyle = '#00f0ff'; g.lineWidth = 3; g.beginPath();",
        "  for (let x = 0; x <= width; x += 4) {",
        "    const y = height / 2 + Math.sin(x / 40 + t * 2) * height / 4;",
        "    if (x === 0) g.moveTo(x, y); else g.lineTo(x, y);",
        "  }",
        "  g.stroke();",
        "  g.fillStyle = '#ffffff'; g.font = '16px system-ui, sans-serif'; g.fillText('Fake wave', 16, 28);",
        "});",
    )),
    "svg": (
        '<svg viewBox="0 0 400 200"><rect x="20" y="60" width="140" height="80" rx="10" fill="#2a3a6a"/>'
        '<text x="90" y="106" fill="#fff" font-size="18" text-anchor="middle" font-family="system-ui">Fake</text>'
        '<line x1="160" y1="100" x2="240" y2="100" stroke="#7cf0ff" stroke-width="4"/>'
        '<rect x="240" y="60" width="140" height="80" rx="10" fill="#2a3a6a"/>'
        '<text x="310" y="106" fill="#fff" font-size="18" text-anchor="middle" font-family="system-ui">diagram</text>'
        "</svg>"
    ),
    "html": (
        "<style>.card{margin:24px;padding:20px;border-radius:14px;background:#1d2747;color:#fff;"
        "font:16px system-ui}</style>"
        '<div class="card"><h2>Fake card</h2><p>Made by the fake codegen model.</p></div>'
    ),
}
BROKEN_SCENES = {
    "three": "const cube = new THREE.Mesh(new THREE.BoxGeometry(2, 2, 2));\ncube.spinForever();",
    "canvas": "onFrame(() => { g.drawNothing(); });",
    "svg": '<svg viewBox="0 0 400 200"></svg>',
    "html": "<script>never()</script>",
}


class FakeProviderError(RuntimeError):
    """A scripted failure."""


class _FakeBase:
    def __init__(self, spec: ProviderSpec, *, model: str | None = None,
                 context: ProviderContext | None = None) -> None:
        self.spec = spec
        self.model = model or "fake"
        self.closed = False

    async def health(self) -> Health:
        return Health(True)

    async def aclose(self) -> None:
        self.closed = True


# ------------------------------------------------------------------------------------------------------- chat


@dataclass
class FakeStreamStats:
    started: int = 0
    completed: int = 0       # reached Finish
    cancelled: int = 0       # closed or cancelled before Finish
    closed: int = 0          # generator finalized (always == started once streams are done)


@dataclass(frozen=True)
class _Script:
    deltas: tuple[str, ...]
    delay_s: float
    fail_at: int | None
    stop_reason: StopReason


def _words(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\s*\S+", text))


def _describe_page(req: ChatRequest) -> str:
    message = next((m for m in reversed(req.messages) if m.role == "user"), None)
    if message is None:
        return "No page arrived."
    text = " ".join(p.text for p in message.parts if isinstance(p, TextPart))
    images = sum(1 for p in message.parts if isinstance(p, ImagePart))
    if "<page_context>" not in text:
        return "No page arrived."
    title = re.search(r"^Title: (.*)$", text, re.MULTILINE)
    shot = re.search(r"^Screenshot: (\d+x\d+) px", text, re.MULTILINE)
    elements = len(re.findall(r"^e\d+ ", text, re.MULTILINE))
    size = f" {shot.group(1)}" if shot else ""
    return f"Page '{title.group(1) if title else ''}' with {elements} elements and {images} screenshot(s){size}."


def _last_user_text(req: ChatRequest) -> str:
    for msg in reversed(req.messages):
        if msg.role == "user":
            return " ".join(p.text for p in msg.parts if isinstance(p, TextPart))
    return ""


def _utterance(text: str) -> str:
    """What the user said: a message ends with "User: <utterance>" (after the page block, if any)."""
    if "<page_context>" in text:
        return text.rsplit("\nUser: ", 1)[-1]
    return text.removeprefix("User: ")


def _point_reply(req: ChatRequest) -> str:
    elements = _ELEMENT_LINE.findall(_last_user_text(req))
    target = next((e for e in elements if e[1] in ("button", "link")), elements[0] if elements else None)
    if target is None:
        return "I can't see anything to point at."
    ident, _role, name = target
    label = " ".join(name.split()[:4]) or ident
    return f'Right over here. The <point to="{ident}" label="{label}"/>{label} is what you want.'


def _scene_reply(match: re.Match[str], text: str) -> str:
    kind = match.group(1) or "three"
    title = (match.group(2) or f"Fake {kind} scene").replace('"', "'").strip()
    brief = " ".join(SCENE_MARKER.sub(" ", _utterance(text)).split()) or f"A fake {kind} scene"
    return f'Here you go. <scene kind="{kind}" title="{title}">{brief}</scene> It should appear in a moment.'


def _codegen_reply(req: ChatRequest) -> str:
    first = " ".join(p.text for p in req.messages[0].parts if isinstance(p, TextPart)) if req.messages else ""
    match = _KIND_LINE.search(first)
    kind = match.group(1) if match else "three"
    repairing = len(req.messages) > 1 or "A first attempt failed" in first
    if BROKEN_MARKER in first and not repairing:
        return BROKEN_SCENES[kind]
    if NOCODE_MARKER in first and not repairing:
        return "I would draw this as a diagram with two boxes and an arrow."
    return FAKE_SCENES[kind]


def _history_reply(req: ChatRequest) -> str:
    earlier = sum(1 for message in req.messages if message.role == "assistant")
    return f"I remember {earlier} earlier {'turn' if earlier == 1 else 'turns'}."


class FakeChat(_FakeBase):
    def __init__(self, spec: ProviderSpec, *, model: str | None = None, options: FakeChatOptions | None = None,
                 context: ProviderContext | None = None) -> None:
        super().__init__(spec, model=model, context=context)
        self.options = options or FakeChatOptions()
        self.stats = FakeStreamStats()
        self.requests: list[ChatRequest] = []
        self.warmed_up = False
        self.warm_system: str | None = None
        self.prewarms: list[str] = []

    def info(self) -> ModelInfo:
        return ModelInfo(name=self.model, vision=True, coord_space="image_px", context_window=32_768)

    async def warm_up(self, system: str | None = None) -> None:
        self.warmed_up = True
        self.warm_system = system

    async def prewarm(self, system: str) -> None:
        self.prewarms.append(system)

    def script_for(self, req: ChatRequest) -> _Script:
        opts = self.options
        text = _last_user_text(req)
        if opts.deltas is not None:
            return _Script(opts.deltas, opts.delay_s, opts.fail_at, opts.stop_reason)
        if req.purpose == "codegen":
            return _Script(_words(_codegen_reply(req)), opts.delay_s, opts.fail_at, opts.stop_reason)
        if TASK_MARKER in text:
            goal = " ".join(_utterance(text).replace(TASK_MARKER, " ").split()) or "Add the item to the cart"
            reply = f'On it. <task session="yes">{goal}</task> I will ask before anything you cannot undo.'
            return _Script(_words(reply), opts.delay_s, opts.fail_at, opts.stop_reason)
        if (scene := SCENE_MARKER.search(text)) is not None:
            return _Script(_words(_scene_reply(scene, text)), opts.delay_s, opts.fail_at, opts.stop_reason)
        if SLOW_MARKER in text:
            return _Script(("Counting slowly:", *(f" {i}" for i in range(1, 61))), 0.1, opts.fail_at,
                           opts.stop_reason)
        if PAGE_MARKER in text:
            return _Script(_words(_describe_page(req)), opts.delay_s, opts.fail_at, opts.stop_reason)
        if POINT_MARKER in text:
            return _Script(_words(_point_reply(req)), opts.delay_s, opts.fail_at, opts.stop_reason)
        if HISTORY_MARKER in text:
            return _Script(_words(_history_reply(req)), opts.delay_s, opts.fail_at, opts.stop_reason)
        clean = _utterance(text)
        for marker in _MARKERS:
            clean = clean.replace(marker, " ")
        heard = " ".join(clean.split()) or "nothing"
        deltas = _words(f"You said: {heard}. This is the fake brain streaming a scripted reply.")
        fail_at = 0 if ERROR_MARKER in text else opts.fail_at
        stop: StopReason = "refusal" if REFUSAL_MARKER in text else opts.stop_reason
        return _Script(deltas, opts.delay_s, fail_at, stop)

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        self.requests.append(req)
        self.stats.started += 1
        script = self.script_for(req)
        finished = False
        try:
            for index, text in enumerate(script.deltas):
                if script.fail_at == index:
                    raise FakeProviderError(f"scripted failure before delta {index}")
                if script.delay_s:
                    await asyncio.sleep(script.delay_s)
                yield TextDelta(text)
            if script.fail_at is not None and script.fail_at >= len(script.deltas):
                raise FakeProviderError("scripted failure before finish")
            finished = True
            self.stats.completed += 1
            yield Finish(
                script.stop_reason,
                refusal_category="fake" if script.stop_reason == "refusal" else None,
                usage=Usage(input_tokens=len(_last_user_text(req).split()), output_tokens=len(script.deltas)),
                native_turn={"role": "assistant", "content": "".join(script.deltas)},
            )
        except (asyncio.CancelledError, GeneratorExit):
            if not finished:
                self.stats.cancelled += 1
            raise
        finally:
            self.stats.closed += 1


# ------------------------------------------------------------------------------------------------- agent LLM


class FakeAgentLLM(_FakeBase):
    def __init__(self, spec: ProviderSpec, *, model: str | None = None, options: FakeAgentOptions | None = None,
                 context: ProviderContext | None = None) -> None:
        super().__init__(spec, model=model, context=context)
        self.options = options or FakeAgentOptions()

    def worker_config(self) -> AgentLLMConfigOut:
        return AgentLLMConfigOut(provider="fake", model=self.model)


# ------------------------------------------------------------------------------------------------------ speech


class FakeSTTStream:
    def __init__(self, transcript: str, delay_s: float = 0.0) -> None:
        self._transcript = transcript
        self._delay_s = delay_s
        self.bytes_fed = 0

    async def feed(self, chunk: bytes) -> None:
        self.bytes_fed += len(chunk)

    async def events(self) -> AsyncIterator[STTEvent]:
        yield SpeechStarted(at_ms=0.0)
        yield Partial(self._transcript)
        yield SpeechEnded(at_ms=500.0)
        yield UtteranceEnd()

    async def finish(self) -> Transcript:
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        # Nothing recorded -> nothing heard (exercises the no-speech path).
        return Transcript(self._transcript if self.bytes_fed else "", language="en", duration_ms=500.0)


class FakeSTT(_FakeBase):
    def __init__(self, spec: ProviderSpec, *, model: str | None = None, options: FakeSTTOptions | None = None,
                 context: ProviderContext | None = None) -> None:
        super().__init__(spec, model=model, context=context)
        self.options = options or FakeSTTOptions()

    async def open_stream(self, *, mime: str, language: str | None = "en") -> FakeSTTStream:
        return FakeSTTStream(self.options.transcript, self.options.delay_s)


def tone_pcm(duration_ms: int, sample_rate: int, hz: float = 220.0, amplitude: int = 2000) -> bytes:
    """Mono PCM16 sine (little-endian)."""
    count = sample_rate * duration_ms // 1000
    return b"".join(int(amplitude * math.sin(2 * math.pi * hz * i / sample_rate)).to_bytes(2, "little", signed=True)
                    for i in range(count))


def silent_wav(duration_ms: int, sample_rate: int = 16_000) -> bytes:
    """A mono 16-bit PCM WAV of silence."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * (sample_rate * duration_ms // 1000))
    return buffer.getvalue()


FAKE_VOICES = ("fake-bright", "fake-calm")


class FakeTTS(_FakeBase):
    def __init__(self, spec: ProviderSpec, *, model: str | None = None, options: FakeTTSOptions | None = None,
                 context: ProviderContext | None = None) -> None:
        super().__init__(spec, model=model, context=context)
        self.options = options or FakeTTSOptions()

    async def voices(self) -> list[tuple[str, str]]:
        return [(voice, voice.removeprefix("fake-").capitalize()) for voice in FAKE_VOICES]

    async def synthesize(self, text: str, *, voice: str | None = None) -> AsyncIterator[AudioChunk]:
        """A quiet 220 Hz tone as streamed PCM (audible enough for an output-level check, never loud)."""
        opts = self.options
        duration_ms = min(opts.max_ms, max(50, int(len(text) * opts.ms_per_char)))
        pcm = tone_pcm(duration_ms, opts.sample_rate)
        step = opts.sample_rate * opts.chunk_ms // 1000 * 2
        for start in range(0, len(pcm), step):
            if opts.delay_s and start:
                await asyncio.sleep(opts.delay_s)
            first = start == 0
            yield AudioChunk(pcm[start:start + step], mime="audio/pcm" if first else None,
                             sample_rate=opts.sample_rate if first else None)


# -------------------------------------------------------------------------------------------------- embeddings


class FakeEmbedder(_FakeBase):
    def __init__(self, spec: ProviderSpec, *, model: str | None = None, options: FakeEmbedOptions | None = None,
                 context: ProviderContext | None = None) -> None:
        super().__init__(spec, model=model, context=context)
        self.options = options or FakeEmbedOptions()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        raw = b""
        counter = 0
        while len(raw) < self.options.dimensions:
            raw += hashlib.sha256(f"{counter}:{text}".encode()).digest()
            counter += 1
        values = [byte / 127.5 - 1.0 for byte in raw[: self.options.dimensions]]
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]
