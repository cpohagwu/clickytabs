"""Brain: directive parsing/validation, segment timing, prompts, history, the Claude / OpenAI-compatible / Ollama
adapters (mocked HTTP) and conversation continuity over the WebSocket."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from clicky.brain.directives import (
    DirectiveParser,
    DirectiveValidator,
    RawBlock,
    RawTag,
    ReplyParser,
    SpeechText,
    TaskRequest,
)
from clicky.brain.history import (
    EPOCH_LIMITS,
    IDLE_RESET_S,
    Conversation,
    ConversationStore,
    make_exchange,
)
from clicky.brain.prompts import system_prompt
from clicky.capabilities.base import ChatMessage, ChatRequest, Finish, ImagePart, ModelInfo, TextDelta, TextPart
from clicky.capabilities.context import ProviderContext
from clicky.capabilities.kinds import Capability
from clicky.capabilities.llm.ollama import coord_space_for, parameter_billions
from clicky.capabilities.registry import resolve_member
from clicky.config import ProviderChoice, Settings
from clicky.protocol.events import MarkDirective, PointDirective
from clicky.protocol.page import PageContext
from clicky.speech.segmenter import SentenceSegmenter
from tests.conftest import ORIGIN, hello

PAGE = PageContext.model_validate({
    "url": "https://docs.example/popover", "title": "Popover API",
    "viewport": {"width": 1280, "height": 720},
    "screenshot": {"data_b64": base64.b64encode(b"jpeg").decode(), "width": 1024, "height": 576},
    "elements": [
        {"id": "e1", "role": "heading", "name": "Popover API", "level": 1, "bbox": [20, 80, 300, 40]},
        {"id": "e2", "role": "button", "name": "Show popover", "bbox": [20, 200, 120, 32]},
        {"id": "e3", "role": "link", "name": "Docs", "bbox": [400, 10, 60, 20]},
    ],
    "text_excerpt": "Popover API\nThe Popover API shows content on top of other content.",
})


def parse_all(text: str, step: int = 1) -> list[Any]:
    parser = DirectiveParser()
    events: list[Any] = []
    for start in range(0, len(text), step):
        events += parser.feed(text[start:start + step])
    return events + parser.close()


def speech(events: list[Any]) -> str:
    return "".join(e.text for e in events if isinstance(e, SpeechText))


# --------------------------------------------------------------------------------------------- parser + validator


@pytest.mark.parametrize("step", [1, 3, 7, 1000])
def test_tags_are_never_spoken_whatever_the_chunking(step: int) -> None:
    reply = 'Top right. The <point to="e2" label="Show popover"/>Show popover button opens it.'
    events = parse_all(reply, step)
    assert speech(events) == "Top right. The Show popover button opens it."
    tags = [e for e in events if isinstance(e, RawTag)]
    assert tags == [RawTag(len("Top right. The "), "point", {"to": "e2", "label": "Show popover"})]


def test_parser_handles_html_noise_literals_and_blocks() -> None:
    events = parse_all("<b>Press</b> <Enter> when 3 < 5 and a<b c.<mark to='e2'></mark><br/>"
                       '<task start_url="https://shop.example">Buy milk</task> Done. <point to="e9"')
    assert speech(events) == "Press Enter when 3 < 5 and a<b c. Done. "
    assert [e.name for e in events if isinstance(e, RawTag)] == ["mark"]
    blocks = [e for e in events if isinstance(e, RawBlock)]
    assert blocks and blocks[0].body == "Buy milk" and blocks[0].attrs["start_url"] == "https://shop.example"


def test_validator_checks_ids_and_converts_coordinates() -> None:
    norm = DirectiveValidator(PAGE, "norm1000")
    assert norm.check(RawTag(0, "point", {"to": "#E2"})) == PointDirective(to="e2")
    assert norm.check(RawTag(0, "point", {"to": "e9"})) is None                    # invented id
    assert norm.check(RawTag(0, "point", {"x": "500", "y": "500"})) == PointDirective(x=512.0, y=288.0)
    assert norm.check(RawTag(0, "point", {"x": "1500", "y": "10"})) is None        # off the screenshot
    assert norm.check(RawTag(0, "mark", {"to": "e3", "style": "sparkle"})) == MarkDirective(to="e3", style="circle")
    assert norm.stats.total == 5 and norm.stats.valid == 3
    assert any("e9 is not an element" in p for p in norm.stats.problems)
    dom_only = DirectiveValidator(PAGE, None)
    assert dom_only.check(RawTag(0, "point", {"x": "10", "y": "10"})) is None
    assert "cannot point by coordinates" in dom_only.stats.problems[0]
    pixels = DirectiveValidator(PAGE, "image_px")
    arrow = pixels.check(RawTag(0, "arrow", {"from": "e1", "to": "600,300"}))
    assert arrow is not None and arrow.source == "e1" and arrow.target == "600,300"
    assert pixels.check(RawTag(0, "path", {"points": "1,1 50,50 90,10"})) is not None
    assert DirectiveValidator(None, "image_px").check(RawTag(0, "point", {"to": "e1"})) is None


def reply_speech(page: PageContext, *deltas: str) -> tuple[str, list[Any]]:
    reply = ReplyParser(page, "image_px")
    events: list[Any] = []
    for delta in deltas:
        events += reply.feed(delta)
    events += reply.close()
    return "".join(e.text for e in events if isinstance(e, SpeechText)), [e for e in events if hasattr(e, "at_char")]


def test_a_tag_written_instead_of_words_has_its_label_spoken() -> None:
    text, marks = reply_speech(PAGE, 'Click <point to="e2" label="Show popover"/>', ". It opens the menu.")
    assert text == "Click Show popover. It opens the menu." and marks[0].at_char == len("Click ")
    text, marks = reply_speech(PAGE, 'Press <point to="e2" label="Show popover"/> or <point to="e3" label="Docs"/>.',
                               ' Then <point to="e1" label="Title"/>scroll.')
    assert text == "Press Show popover or Docs. Then scroll."   # "scroll" follows the last tag: a word, kept as is
    assert [m.at_char for m in marks] == [len("Press "), len("Press Show popover or "),
                                          len("Press Show popover or Docs. Then ")]
    named, _ = reply_speech(PAGE, 'The <point to="e2" label="Show popover"/>Show popover button opens it.')
    assert named == "The Show popover button opens it."           # the name follows the tag: unchanged
    text, _ = reply_speech(PAGE, 'Top left. Click <point to="e2" label="Show popover"/> Right above the buttons.')
    assert text == "Top left. Click Show popover. Right above the buttons."
    for kept in ('Press <point to="e2" label="Pro plan"/>Choose Pro under the price.',      # shares "Pro"
                 'Click the little <point to="e2" label="Close"/>X in the corner.',         # no action verb before
                 'Click <point to="e2" label="Close"/>the X in the corner.'):               # lowercase: a phrase
        assert reply_speech(PAGE, kept)[0] == re.sub(r"<[^>]+>", "", kept)


def test_task_requests_are_validated_not_trusted() -> None:
    validator = DirectiveValidator(PAGE, "image_px")
    task = validator.block(RawBlock(0, "task", {"start_url": "https://shop.example/cart", "session": "yes"},
                                    " Order  more milk "))
    assert task == TaskRequest(goal="Order more milk", start_url="https://shop.example/cart", session=True,
                               cross_site=True)
    assert validator.block(RawBlock(0, "task", {"start_url": "javascript:alert(1)"}, "Do it now")) is None
    same = validator.block(RawBlock(0, "task", {"start_url": "https://www.docs.example/x"}, "Open the guide"))
    assert isinstance(same, TaskRequest) and not same.cross_site and not same.session


def test_directives_ride_with_the_sentence_that_names_their_target() -> None:
    reply = ReplyParser(PAGE, "image_px")
    segmenter = SentenceSegmenter()
    pieces = []
    for delta in ["Top right corner. ", "Press the <point ", 'to="e2"/>Show popover ', "button. ", '<mark to="e3"/>']:
        for event in reply.feed(delta):
            if isinstance(event, SpeechText):
                pieces += list(segmenter.push(event.text))
            else:
                segmenter.mark(event.directive)
    pieces += list(segmenter.finish())
    assert [p.text for p in pieces] == ["Top right corner.", "Press the Show popover button.", ""]
    second = pieces[1]
    assert second.marks == ((len("Press the "), PointDirective(to="e2")),)
    assert pieces[2].marks == ((0, MarkDirective(to="e3")),)          # trailing: no words of its own
    assert reply.raw_text.endswith('button. <mark to="e3"/>')


# ------------------------------------------------------------------------------------------------------- prompts


def test_system_prompts_are_stable_and_match_the_model() -> None:
    claude = ModelInfo("claude", vision=True, coord_space="image_px", prompt="full", history="verbatim")
    qwen = ModelInfo("qwen3.5:4b", vision=True, coord_space="norm1000")
    blind = ModelInfo("text-only")
    assert system_prompt(claude) == system_prompt(claude)
    assert "screenshot pixels" in system_prompt(claude) and "<arrow" in system_prompt(claude)
    assert "0 to 1000" in system_prompt(qwen) and "<arrow" not in system_prompt(qwen)
    assert "only point at elements" in system_prompt(blind)
    assert all("{{" not in system_prompt(i) for i in (claude, qwen, blind, None))


# ------------------------------------------------------------------------------------------------------- history


def exchange(n: int, *, provider: str = "anthropic:claude", native: object | None = "native") -> Any:
    user = ChatMessage("user", (ImagePart(b"shot"), TextPart(f"<page_context>...</page_context>\nUser: q{n}")))
    brief = ChatMessage("user", (TextPart(f"User: q{n}"),))
    return make_exchange(utterance=f"q{n}", page=PAGE, user=user, brief=brief, reply=f"a{n}", native=native,
                         provider=provider)


def test_history_replays_verbatim_or_compact_per_model() -> None:
    conversation = Conversation("conversation-1")
    conversation.record(exchange(1), None, None)
    newest = ChatMessage("user", (TextPart("q2"),))
    claude = ModelInfo("claude", history="verbatim")
    verbatim = conversation.messages_for(claude, "anthropic:claude", newest)
    assert verbatim[0] is conversation.exchanges[0].user and verbatim[1].native == "native"
    assert verbatim[-1] is newest
    compact = conversation.messages_for(ModelInfo("qwen"), "ollama:qwen", newest)
    assert compact[0].parts == (TextPart("User: q1"),) and compact[1].native is None   # other provider: text only


async def test_epochs_roll_over_into_a_summary_and_keep_the_new_tail_as_text() -> None:
    conversation = Conversation("conversation-1")
    limit = EPOCH_LIMITS["compact"].max_exchanges
    gate = asyncio.Event()
    seen: list[str] = []

    async def summarize(transcript: str) -> str:
        seen.append(transcript)
        await gate.wait()
        return "The user asked about popovers."

    for n in range(limit):
        conversation.record(exchange(n), ModelInfo("qwen"), summarize)
    conversation.record(exchange(99), ModelInfo("qwen"), summarize)       # arrives while summarizing
    await asyncio.sleep(0)
    gate.set()
    await conversation.ready()
    assert conversation.epoch == 1 and conversation.summary == "The user asked about popovers."
    assert [e.utterance for e in conversation.exchanges] == ["q99"] and conversation.exchanges[0].native is None
    assert "User (on \"Popover API\"): q0" in seen[0]
    messages = conversation.messages_for(ModelInfo("qwen"), "x", ChatMessage("user", (TextPart("next"),)))
    assert messages[0].parts[0].text.endswith("The user asked about popovers.")


async def test_a_failing_summarizer_keeps_notes_and_idle_conversations_start_afresh() -> None:
    now = [0.0]
    conversation = Conversation("conversation-1", clock=lambda: now[0])

    async def broken(_transcript: str) -> str:
        raise RuntimeError("summarizer down")

    for n in range(EPOCH_LIMITS["compact"].max_exchanges):
        conversation.record(exchange(n), ModelInfo("qwen"), broken)
    await conversation.ready()
    assert conversation.epoch == 1 and conversation.summary and "q5" in conversation.summary
    conversation.record(exchange(7), ModelInfo("qwen"), None)
    now[0] += IDLE_RESET_S + 1
    conversation.touch()
    assert conversation.exchanges == [] and conversation.summary is None and conversation.answered == 0


def test_conversation_store_reuses_ids_and_evicts_the_oldest() -> None:
    store = ConversationStore(max_conversations=2)
    first = store.get("abcdefgh-1")
    assert store.get("abcdefgh-1") is first
    assert store.get("bad id!").id != "bad id!"
    store.get("abcdefgh-3")
    assert len(store) == 2 and store.get("abcdefgh-1") is not first      # evicted, recreated fresh


# ---------------------------------------------------------------------------------------------- Claude adapter


def sse(*events: dict[str, Any]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def claude_stream(*texts: str, stop: str = "end_turn", stop_details: dict[str, Any] | None = None,
                  model: str = "claude-opus-5-5") -> bytes:
    usage = {"input_tokens": 900, "output_tokens": 1, "cache_read_input_tokens": 700,
             "cache_creation_input_tokens": 0}
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                              "content": [], "stop_reason": None, "stop_sequence": None,
                                              "usage": usage}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "",
                                                                      "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-1"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        *({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": t}} for t in texts),
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None,
                                            **({"stop_details": stop_details} if stop_details else {})},
         "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    ]
    return sse(*events)


def claude(handler: Any, **options: Any) -> Any:
    pytest.importorskip("anthropic")
    import anthropic
    import httpx2

    from clicky.capabilities.llm.anthropic import AnthropicChat

    settings = Settings(profile="cloud", anthropic_api_key="sk-ant-test")
    ctx = ProviderContext.create(settings, httpx.AsyncClient())
    member = resolve_member(Capability.BRAIN, ProviderChoice(provider="anthropic", options=options), "test")
    chat = AnthropicChat(member.spec, model=member.model, options=member.options, context=ctx)
    chat._client = anthropic.AsyncAnthropic(api_key="sk-ant-test", max_retries=0, http_client=(
        anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))))
    return chat


async def run(chat: Any, request: ChatRequest) -> list[Any]:
    return [event async for event in chat.stream(request)]


REQUEST = ChatRequest(system="SYSTEM", max_tokens=1024, messages=(
    ChatMessage("user", (ImagePart(b"shot"), TextPart("where?"))),
    ChatMessage("assistant", (TextPart("old"),), native=[{"type": "thinking", "thinking": "", "signature": "s0"},
                                                         {"type": "text", "text": "There."}]),
    ChatMessage("user", (TextPart("and now?"),)),
))


async def test_claude_request_shape_streaming_and_native_replay() -> None:
    seen: list[Any] = []

    def handler(request: Any) -> Any:
        import httpx2

        seen.append(request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=claude_stream("Top right. ", "The button."))

    events = await run(claude(handler), REQUEST)
    body = json.loads(seen[0].content)
    assert body["model"] == "claude-opus-5-5" and body["output_config"] == {"effort": "low"}
    assert body["max_tokens"] == 1024 + 4096 and "thinking" not in body and "temperature" not in body
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in seen[0].headers["anthropic-beta"]
    assert body["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][0]["content"][0]["source"] == {"type": "base64", "media_type": "image/jpeg",
                                                           "data": base64.b64encode(b"shot").decode()}
    assert body["messages"][1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "s0"}
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Top right. ", "The button."]
    finish = events[-1]
    assert isinstance(finish, Finish) and finish.stop_reason == "end"
    assert finish.native_turn == [{"type": "thinking", "thinking": "", "signature": "sig-1"},
                                  {"type": "text", "text": "Top right. The button."}]
    assert finish.usage is not None and finish.usage.cache_read_tokens == 700


async def test_claude_refusals_keep_nothing_and_rejected_fallbacks_are_dropped() -> None:
    import httpx2

    def refusing(_request: Any) -> Any:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=claude_stream("I", stop="refusal",
                                                     stop_details={"type": "refusal", "category": "cyber"}))

    finish = (await run(claude(refusing), REQUEST))[-1]
    assert finish.stop_reason == "refusal" and finish.native_turn is None

    bodies: list[dict[str, Any]] = []

    def no_fallbacks(request: Any) -> Any:
        body = json.loads(request.content)
        bodies.append(body)
        if "fallbacks" in body:
            return httpx2.Response(400, json={"type": "error", "error": {
                "type": "invalid_request_error", "message": "fallbacks: not available for this organization"}})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=claude_stream("Hi."))

    chat = claude(no_fallbacks)
    assert (await run(chat, REQUEST))[-1].stop_reason == "end"
    assert "fallbacks" in bodies[0] and "fallbacks" not in bodies[1]
    await run(chat, REQUEST)
    assert "fallbacks" not in bodies[2]                                   # remembered


async def test_claude_errors_are_described_without_secrets() -> None:
    import httpx2

    from clicky.capabilities.llm.anthropic import AnthropicError

    def unauthorized(_request: Any) -> Any:
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error",
                                                                      "message": "invalid x-api-key"}})

    with pytest.raises(AnthropicError, match="rejected ANTHROPIC_API_KEY") as caught:
        await run(claude(unauthorized), REQUEST)
    assert "sk-ant-test" not in str(caught.value)


# --------------------------------------------------------------------------------- OpenAI-compatible + Ollama


async def test_openai_compatible_chat_streams_sse_with_images() -> None:
    from clicky.capabilities.llm.openai_compat import OpenAICompatChat

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-x"}]})
        chunks = [{"choices": [{"delta": {"content": "Hello "}}]}, {"choices": [{"delta": {"content": "there."},
                                                                                "finish_reason": "stop"}]},
                  {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 3}}]
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    settings = Settings(profile="cloud", openai_api_key="sk-test")
    ctx = ProviderContext.create(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    member = resolve_member(Capability.BRAIN, ProviderChoice(provider="openai_compat", model="gpt-x",
                                                             options={"base_url": "http://llm.test/v1"}), "test")
    chat = OpenAICompatChat(member.spec, model="gpt-x", options=member.options, context=ctx)
    assert (await chat.health()).ok
    events = await run(chat, REQUEST)
    body = json.loads(seen[-1].content)
    assert seen[-1].headers["authorization"] == "Bearer sk-test"
    assert body["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert body["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert body["messages"][2] == {"role": "assistant", "content": "old"} and body["max_completion_tokens"] == 1024
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello there."
    assert events[-1].stop_reason == "end" and events[-1].usage.input_tokens == 50


def test_ollama_infers_coordinates_and_prompt_size() -> None:
    assert coord_space_for("qwen3.5:4b") == "norm1000" and coord_space_for("qwen2.5vl:3b") == "image_px"
    assert coord_space_for("hf.co/someone/Qwen3-VL-8B:Q4_K_M") == "norm1000"
    assert coord_space_for("ministral-3:3b") == "image_px"
    assert parameter_billions("4.7B") == 4.7 and parameter_billions("820M") == 0.82
    assert parameter_billions(None) is None


# ----------------------------------------------------------------------------------------- over the WebSocket


def turn(ws: Any, turn_id: str, text: str, page: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    ws.send_json({"type": "turn.start", "turn_id": turn_id, "mode": "text", "text": text,
                  **({"page": page} if page else {})})
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] == "turn.done" and event["turn_id"] == turn_id:
            return events


def said(events: list[dict[str, Any]]) -> str:
    return "".join(e["delta"] for e in events if e["type"] == "assistant.text.delta")


def test_follow_up_questions_continue_the_conversation_across_reconnects(client: TestClient) -> None:
    with client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_json(hello())
        welcome = ws.receive_json()
        conversation_id = welcome["conversation_id"]
        assert welcome["conversation_turns"] == 0
        turn(ws, "t1", "What is a popover?")
        assert said(turn(ws, "t2", "[history] and then?")) == "I remember 1 earlier turn."
    with client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:   # e.g. a service-worker restart
        ws.send_json({**hello(), "conversation_id": conversation_id})
        welcome = ws.receive_json()
        assert welcome["conversation_id"] == conversation_id and welcome["conversation_turns"] == 2
        assert said(turn(ws, "t3", "[history] still there?")) == "I remember 2 earlier turns."
        ws.send_json({"type": "conversation.reset"})
        started = ws.receive_json()
        assert started["type"] == "conversation.started" and started["conversation_id"] != conversation_id
        assert said(turn(ws, "t4", "[history] fresh?")) == "I remember 0 earlier turns."


def test_text_turn_points_without_speaking_the_tag(client: TestClient) -> None:
    page = PAGE.model_dump()
    with client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_json(hello())
        ws.receive_json()
        events = turn(ws, "p1", "[point] where is it?", page)
    assert said(events) == "Right over here. The Show popover is what you want."
    directives = [e["directive"] for e in events if e["type"] == "visual.directive"]
    assert directives == [{"kind": "point", "to": "e2", "x": None, "y": None, "label": "Show popover"}]
    assert events[-1]["reason"] == "end"


def test_voice_turn_times_directives_inside_their_sentence(client: TestClient) -> None:
    from clicky.capabilities.options import FakeSTTOptions

    stt = client.app.state.capabilities.get(Capability.STT).active.provider       # type: ignore[attr-defined]
    stt.options = FakeSTTOptions(transcript="[point] where is the button?")
    with client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_json(hello())
        ws.receive_json()
        ws.send_json({"type": "turn.start", "turn_id": "v1", "mode": "voice", "page": PAGE.model_dump()})
        ws.send_json({"type": "audio.chunk", "turn_id": "v1", "seq": 0, "data_b64": base64.b64encode(b"x").decode()})
        ws.send_json({"type": "audio.end", "turn_id": "v1"})
        events = []
        while (event := ws.receive_json())["type"] != "turn.done":
            events.append(event)
    segments = [e for e in events if e["type"] == "speech.segment"]
    pointed = [(s["text"], d) for s in segments for d in s["directives"]]
    assert len(pointed) == 1
    text, timed = pointed[0]
    assert text == "The Show popover is what you want."
    assert timed["at_char"] == len("The ") and timed["directive"]["to"] == "e2"
    assert not any(e["type"] == "visual.directive" for e in events)
    assert all("<" not in s["text"] for s in segments)


def test_voice_turn_prewarms_the_brain_with_its_system_prompt(client: TestClient) -> None:
    brain = client.app.state.capabilities.get(Capability.BRAIN).active.provider   # type: ignore[attr-defined]
    assert brain.warm_system == system_prompt(brain.info())                        # at startup
    with client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_json(hello())
        ws.receive_json()
        ws.send_json({"type": "turn.start", "turn_id": "v1", "mode": "voice"})
        ws.send_json({"type": "audio.end", "turn_id": "v1"})
        while ws.receive_json()["type"] != "turn.done":
            pass
    assert brain.prewarms == [system_prompt(brain.info())]
