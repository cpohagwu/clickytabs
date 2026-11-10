"""Scenes - the <scene> brief, codegen prompts and code extraction, the render/repair loop, and the wire flow."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clicky.brain.context import page_message
from clicky.brain.directives import DirectiveAt, ReplyParser, SceneRequest, SpeechText
from clicky.brain.scenes import (
    MAX_CODE_CHARS,
    SceneCodeError,
    SceneJob,
    SceneManager,
    SceneRecord,
    codegen_request,
    extract_code,
    job_message,
    repair_request,
)
from clicky.capabilities.base import ModelInfo, TextPart
from clicky.capabilities.fake import BROKEN_SCENES, FAKE_SCENES, FakeChat
from clicky.capabilities.kinds import Capability
from clicky.capabilities.registry import Capabilities, build_capabilities
from clicky.protocol.events import Event, SceneBuilding, SceneFailed, SceneRender, SceneResult
from clicky.protocol.page import OpenScene, PageContext
from tests.conftest import ORIGIN, hello, make_settings

PAGE = PageContext.model_validate({
    "url": "https://docs.example/engines", "title": "Engines",
    "viewport": {"width": 1280, "height": 720},
    "elements": [{"id": "e2", "role": "button", "name": "Show popover", "bbox": [20, 200, 120, 32]}],
})
FULL = ModelInfo(name="claude", vision=True, coord_space="image_px", prompt="full", history="verbatim")
SMALL = ModelInfo(name="qwen", vision=True, coord_space="norm1000")


def reply(text: str, step: int) -> tuple[str, list[Any]]:
    parser = ReplyParser(PAGE, "image_px")
    events: list[Any] = []
    for start in range(0, len(text), step):
        events += parser.feed(text[start:start + step])
    events += parser.close()
    return "".join(e.text for e in events if isinstance(e, SpeechText)), events


# ------------------------------------------------------------------------------------------------- the brief


@pytest.mark.parametrize("step", [1, 4, 1000])
def test_a_scene_brief_is_never_spoken_and_the_speech_around_it_closes_up(step: int) -> None:
    text = ('Sure, here it is. <scene kind="three" title="Four-stroke  engine">A cutaway cylinder; label the strokes.'
            '</scene> Press <point to="e2" label="Show popover"/>Show popover to see more.')
    spoken, events = reply(text, step)
    assert spoken == "Sure, here it is. Press Show popover to see more."
    scenes = [e for e in events if isinstance(e, SceneRequest)]
    assert scenes == [SceneRequest("three", "Four-stroke engine", "A cutaway cylinder; label the strokes.")]
    (point,) = [e for e in events if isinstance(e, DirectiveAt)]
    assert spoken[point.at_char:].startswith("Show popover")            # offsets follow the squeezed speech


def test_a_bad_scene_is_dropped_but_still_never_spoken() -> None:
    spoken, events = reply('Okay. <scene kind="video" title="x">a movie</scene> Done.', 1000)
    assert spoken == "Okay. Done."
    assert not [e for e in events if isinstance(e, SceneRequest)]
    spoken, events = reply('<scene kind="svg" title="Water cycle"></scene>Here it is.', 3)
    assert spoken == "Here it is."
    assert [e for e in events if isinstance(e, SceneRequest)] == [SceneRequest("svg", "Water cycle", "")]


# --------------------------------------------------------------------------------------- prompts and code


def test_codegen_prompts_match_the_model_and_carry_the_scene_on_screen() -> None:
    base = SceneRecord("scene-old-0001", "three", "Cube", "scene.add(cube);")
    job = SceneJob("scene-new-0001", "t1", "three", "Cube", "make it spin faster", "make it spin faster", base)
    full, compact = codegen_request(job, FULL), codegen_request(job, SMALL)
    assert full.purpose == compact.purpose == "codegen"
    assert "OrbitControls with damping" in full.system and len(compact.system) < len(full.system) / 2
    assert full.max_tokens > compact.max_tokens
    message = job_message(job)
    assert "Kind: three" in message and 'The user said: "make it spin faster"' in message
    assert "The scene on screen now is \"Cube\"" in message and "scene.add(cube);" in message
    repair = repair_request(job, "cube.spin();", "TypeError: cube.spin is not a function (line 1: cube.spin();)", FULL)
    roles = [m.role for m in repair.messages]
    assert roles == ["user", "assistant", "user"]
    last = repair.messages[-1].parts[0]
    assert isinstance(last, TextPart) and "cube.spin is not a function" in last.text
    fresh = SceneJob("scene-new-0002", "t1", "three", "Cube", "a spinning cube", "show me a cube")
    (only,) = repair_request(fresh, "cube.spin();", "TypeError: cube.spin is not a function", SMALL).messages
    assert isinstance(only.parts[0], TextPart)                  # a fresh attempt: no failed code to replay
    assert "A first attempt failed" in only.parts[0].text and "cube.spin();" not in only.parts[0].text


def test_code_is_extracted_from_fences_prose_and_svg_answers() -> None:
    assert extract_code("three", "```js\nscene.add(cube);\n```") == "scene.add(cube);"
    assert extract_code("three", "scene.add(cube);") == "scene.add(cube);"
    longest = extract_code("canvas", "```\nshort\n```\nThen:\n```javascript\nonFrame(() => {\n  draw();\n});\n```")
    assert longest == "onFrame(() => {\n  draw();\n});"
    assert extract_code("svg", 'Here you go:\n<svg viewBox="0 0 10 10"><rect/></svg>\nEnjoy!') == \
        '<svg viewBox="0 0 10 10"><rect/></svg>'
    assert extract_code("three", "```js\nunterminated();") == "unterminated();"
    with pytest.raises(SceneCodeError):
        extract_code("svg", "<div>no svg here</div>")
    with pytest.raises(SceneCodeError):
        extract_code("html", "  ")
    with pytest.raises(SceneCodeError):
        extract_code("html", "x" * (MAX_CODE_CHARS + 1))


def test_the_page_tells_the_brain_which_scene_is_showing() -> None:
    page = PAGE.model_copy(update={"scene": OpenScene(scene_id="scene-abc-0001", title="Solar system")})
    text = page_message("make it bigger", page, FULL).parts[-1]
    assert isinstance(text, TextPart)
    assert 'Scene you built, showing in front of the page: "Solar system"' in text.text


# --------------------------------------------------------------------------------------------- the manager


async def fake_capabilities() -> Capabilities:
    return await build_capabilities(make_settings())


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.arrived = asyncio.Event()

    def __call__(self, event: Event) -> None:
        self.events.append(event)
        self.arrived.set()

    async def next(self, kind: type[Event], timeout: float = 2.0) -> Any:
        async with asyncio.timeout(timeout):
            while True:
                for event in self.events:
                    if isinstance(event, kind):
                        self.events.remove(event)
                        return event
                self.arrived.clear()
                await self.arrived.wait()


async def test_a_scene_is_written_rendered_and_remembered() -> None:
    caps = await fake_capabilities()
    emit = Recorder()
    manager = SceneManager(caps, emit)
    try:
        scene_id = manager.start("t1", SceneRequest("three", "Cube", "a spinning cube"), "show me a cube", None)
        building = await emit.next(SceneBuilding)
        assert (building.scene_id, building.kind, building.title, building.turn_id) == (scene_id, "three", "Cube", "t1")
        render = await emit.next(SceneRender)
        assert render.code == FAKE_SCENES["three"] and render.attempt == 1 and render.codegen_ms is not None
        manager.on_result(SceneResult(scene_id="someone-else-1", ok=True))          # not this scene: ignored
        manager.on_result(SceneResult(scene_id=scene_id, attempt=1, ok=True))
        await asyncio.wait_for(manager._task, 2)  # type: ignore[arg-type]
        assert manager.finished == [(scene_id, "shown", 1)]
        assert manager.record(scene_id) == SceneRecord(scene_id, "three", "Cube", FAKE_SCENES["three"])

        # A follow-up while it shows: the codegen model gets its code to edit.
        page = PAGE.model_copy(update={"scene": OpenScene(scene_id=scene_id, title="Cube")})
        manager.start("t2", SceneRequest("three", "Cube", "make it red"), "make it red", page)
        await emit.next(SceneRender)
        codegen = caps.get(Capability.CODEGEN).active.provider
        assert isinstance(codegen, FakeChat)
        first = codegen.requests[-1].messages[0].parts[0]
        assert isinstance(first, TextPart) and FAKE_SCENES["three"] in first.text
    finally:
        await manager.cancel()
        await caps.aclose()


async def test_a_failing_scene_gets_one_repair_then_gives_up() -> None:
    caps = await fake_capabilities()
    emit = Recorder()
    manager = SceneManager(caps, emit)
    try:
        scene_id = manager.start("t1", SceneRequest("svg", "Diagram", "boxes [broken]"), "draw it", None)
        first = await emit.next(SceneRender)
        assert first.code == BROKEN_SCENES["svg"] and first.attempt == 1
        manager.on_result(SceneResult(scene_id=scene_id, attempt=1, ok=False, error="blank scene"))
        second = await emit.next(SceneRender)
        assert second.code == FAKE_SCENES["svg"] and second.attempt == 2
        manager.on_result(SceneResult(scene_id=scene_id, attempt=1, ok=False, error="stale"))   # old attempt: ignored
        manager.on_result(SceneResult(scene_id=scene_id, attempt=2, ok=False, error="still broken"))
        failed = await emit.next(SceneFailed)
        assert failed.scene_id == scene_id and failed.error == "still broken"
        assert manager.finished == [(scene_id, "failed", 2)]

        # A tab that can't show it: no repair.
        other = manager.start("t2", SceneRequest("html", "Card", "a card"), "a card", None)
        await emit.next(SceneRender)
        manager.on_result(SceneResult(scene_id=other, attempt=1, ok=False, unavailable=True, error="closed"))
        assert (await emit.next(SceneFailed)).scene_id == other
        assert not [e for e in emit.events if isinstance(e, SceneRender)]
    finally:
        await manager.cancel()
        await caps.aclose()


async def test_no_answer_from_the_browser_and_a_newer_request_end_a_scene() -> None:
    caps = await fake_capabilities()
    emit = Recorder()
    manager = SceneManager(caps, emit, result_timeout_s=0.2)
    try:
        silent = manager.start("t1", SceneRequest("three", "Cube", "a cube"), "cube", None)
        await emit.next(SceneRender)
        failed = await emit.next(SceneFailed)
        assert failed.scene_id == silent and "did not report back" in failed.error

        manager = SceneManager(caps, emit, result_timeout_s=5)
        replaced = manager.start("t2", SceneRequest("three", "Cube", "a cube"), "cube", None)
        await emit.next(SceneRender)
        newer = manager.start("t3", SceneRequest("svg", "Map", "a map"), "map", None)
        assert newer != replaced
        await emit.next(SceneRender)
        manager.on_result(SceneResult(scene_id=replaced, attempt=1, ok=True))       # the replaced job is gone
        assert manager.finished == []
    finally:
        await manager.cancel()
        await caps.aclose()


# ----------------------------------------------------------------------------------------------- the wire


def scene_turn(ws: Any, turn_id: str, text: str, *, tab: bool = True) -> list[dict[str, Any]]:
    ws.send_json({"type": "turn.start", "turn_id": turn_id, "mode": "text", "text": text,
                  **({"tab_id": 7} if tab else {})})
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] == "turn.done" and event["turn_id"] == turn_id:
            return events


def receive_until(ws: Any, kind: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in events:
        if event["type"] == kind:
            events.remove(event)
            return event
    while True:
        event = ws.receive_json()
        if event["type"] == kind:
            return event


def test_scenes_over_the_wire_with_one_repair(client: TestClient) -> None:
    with client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_json(hello())
        ws.receive_json()
        events = scene_turn(ws, "s1", "[scene:canvas] [broken] a wave please")
        assert "".join(e["delta"] for e in events if e["type"] == "assistant.text.delta") == \
            "Here you go. It should appear in a moment."
        building = receive_until(ws, "scene.building", events)
        assert building["kind"] == "canvas" and building["turn_id"] == "s1"
        first = receive_until(ws, "scene.render", events)
        assert first["code"] == BROKEN_SCENES["canvas"] and first["attempt"] == 1
        ws.send_json({"type": "scene.result", "scene_id": first["scene_id"], "attempt": 1, "ok": False,
                      "error": "TypeError: g.drawNothing is not a function (line 1)"})
        second = receive_until(ws, "scene.render", [])
        assert second["code"] == FAKE_SCENES["canvas"] and second["attempt"] == 2
        ws.send_json({"type": "scene.result", "scene_id": second["scene_id"], "attempt": 2, "ok": True})
        # The popup's debug box has no tab: the brain's scene is not built there.
        events = scene_turn(ws, "s2", "[scene] a cube", tab=False)
        assert not [e for e in events if e["type"].startswith("scene.")]
        ws.send_json({"type": "ping"})
        assert receive_until(ws, "pong", [])["type"] == "pong"


def test_a_malformed_scene_result_is_rejected(client: TestClient) -> None:
    with client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_json(hello())
        ws.receive_json()
        ws.send_json({"type": "scene.result", "scene_id": "<script>", "ok": True})
        error = ws.receive_json()
        assert error["type"] == "error" and error["code"] == "bad_request"
        too_long = base64.b64encode(b"x" * 3000).decode()
        ws.send_json({"type": "scene.result", "scene_id": "scene-0001", "ok": False, "error": too_long})
        assert ws.receive_json()["type"] == "error"


async def test_a_new_conversation_tells_the_stage_its_scene_was_stopped() -> None:
    caps = await fake_capabilities()
    emit = Recorder()
    manager = SceneManager(caps, emit)
    try:
        scene_id = manager.start("t1", SceneRequest("three", "Cube", "a cube"), "cube", None)
        await emit.next(SceneRender)
        await manager.cancel("Stopped: a new conversation started.")
        failed = await emit.next(SceneFailed)
        assert failed.scene_id == scene_id and failed.error.startswith("Stopped")
        await manager.cancel("again")                                    # nothing running: nothing to say
        assert not [e for e in emit.events if isinstance(e, SceneFailed)]
    finally:
        await caps.aclose()


async def test_an_answer_without_code_goes_to_the_repair_turn() -> None:
    caps = await fake_capabilities()
    emit = Recorder()
    manager = SceneManager(caps, emit)
    try:
        scene_id = manager.start("t1", SceneRequest("svg", "Boxes", "two boxes [nocode]"), "draw boxes", None)
        render = await emit.next(SceneRender)                 # attempt 1 had nothing to render
        assert render.attempt == 2 and render.code == FAKE_SCENES["svg"]
        manager.on_result(SceneResult(scene_id=scene_id, attempt=2, ok=True))
        await asyncio.wait_for(manager._task, 2)  # type: ignore[arg-type]
        assert manager.finished == [(scene_id, "shown", 2)]
        codegen = caps.get(Capability.CODEGEN).active.provider
        assert isinstance(codegen, FakeChat)
        retry = codegen.requests[-1].messages[-1].parts[0]
        assert isinstance(retry, TextPart) and "no <svg> element" in retry.text
    finally:
        await manager.cancel()
        await caps.aclose()
