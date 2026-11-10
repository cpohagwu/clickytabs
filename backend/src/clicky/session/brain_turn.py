"""One brain call inside a text or voice turn.

    BrainTurn.request_for(member)   system prompt (variant + coordinate rule of that model), the conversation's
                                    history replayed per its policy, the newest user turn (page context, screenshot
                                    for vision models)
    BrainTurn.feed(delta)           reply -> speech text + validated directives (+ <task>/<scene> requests)
    BrainTurn.record(...)           the exchange goes into the conversation; a cancelled reply is kept as plain text

The chain may fall back only before the first delta, so the model whose request was built last is the one answering
when the first delta arrives: the reply parser is created then, with that model's coordinate convention.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from clicky.brain.context import brief_message, page_message
from clicky.brain.directives import ReplyEvent, ReplyParser, SceneRequest, TaskRequest
from clicky.brain.history import SUMMARIZER_SYSTEM, Conversation, make_exchange
from clicky.brain.prompts import system_prompt
from clicky.capabilities.base import ChatMessage, ChatRequest, Finish, ModelInfo, TextPart
from clicky.capabilities.fallback import complete_chat
from clicky.capabilities.kinds import Capability
from clicky.capabilities.registry import Capabilities, ChainMember
from clicky.protocol.page import PageContext

log = logging.getLogger(__name__)

REPLY_MAX_TOKENS = 1024            # spoken replies are a few sentences; Claude adds thinking headroom on top
SUMMARY_MAX_TOKENS = 400

# Starts building a scene the brain asked for: (request, what the user said, the page)
SceneStarter = Callable[[SceneRequest, str, PageContext | None], object]
# Asks the user to consent to a browser task the brain proposed: (request, the page)
TaskStarter = Callable[[TaskRequest, PageContext | None], object]


def member_info(member: ChainMember) -> ModelInfo | None:
    info = getattr(member.provider, "info", None)
    return info() if callable(info) else None


def single_request(utterance: str, page: PageContext | None, info: ModelInfo | None) -> ChatRequest:
    """A one-exchange brain request without history (evals, `clicky chat`)."""
    return ChatRequest(system=system_prompt(info), messages=(page_message(utterance, page, info),),
                       max_tokens=REPLY_MAX_TOKENS, purpose="brain")


class BrainTurn:
    def __init__(self, conversation: Conversation, capabilities: Capabilities, utterance: str,
                 page: PageContext | None, on_scene: SceneStarter | None = None,
                 on_task: TaskStarter | None = None) -> None:
        self.conversation = conversation
        self.on_scene = on_scene
        self.on_task = on_task
        self.capabilities = capabilities
        self.utterance = utterance
        self.page = page
        self.info: ModelInfo | None = None
        self.label: str | None = None
        self.user_message: ChatMessage | None = None
        self.reply: ReplyParser | None = None
        self.tasks: list[TaskRequest] = []
        self.scenes: list[SceneRequest] = []
        self._recorded = False

    def request_for(self, member: ChainMember) -> ChatRequest:
        info = member_info(member)
        self.info, self.label = info, member.label
        self.user_message = page_message(self.utterance, self.page, info)
        messages = self.conversation.messages_for(info, member.label, self.user_message)
        return ChatRequest(system=system_prompt(info), messages=messages, max_tokens=REPLY_MAX_TOKENS,
                           purpose="brain")

    # ------------------------------------------------------------------------------------------------ reply

    def feed(self, delta: str) -> list[ReplyEvent]:
        if self.reply is None:
            self.reply = ReplyParser(self.page, self.info.coord_space if self.info else None)
        return self._note(self.reply.feed(delta))

    def close(self) -> list[ReplyEvent]:
        return self._note(self.reply.close()) if self.reply is not None else []

    def _note(self, events: list[ReplyEvent]) -> list[ReplyEvent]:
        for event in events:
            if isinstance(event, TaskRequest):
                self.tasks.append(event)
                if len(self.tasks) > 1:
                    log.info("ignoring a second task in one reply")
                elif self.on_task is None:
                    log.info("the brain proposed a browser task, but this turn has no tab to ask for consent in")
                else:
                    self.on_task(event, self.page)
            elif isinstance(event, SceneRequest):
                self.scenes.append(event)
                if len(self.scenes) > 1:
                    log.info("ignoring a second scene in one reply")
                elif self.on_scene is None:
                    log.info("the brain asked for a %s scene, but this turn has no tab to show it", event.kind)
                else:
                    self.on_scene(event, self.utterance, self.page)
        return events

    # ----------------------------------------------------------------------------------------------- history

    def record(self, finish: Finish | None, provider: str | None) -> None:
        """Keep the exchange: an answer with its native turn, or (finish=None) a cut-off reply as plain text."""
        if self._recorded or self.reply is None or self.user_message is None:
            return
        raw = self.reply.raw_text
        if not raw.strip():
            return
        if finish is not None and finish.stop_reason in ("refusal", "error"):
            return                        # nothing worth replaying; the user turn is dropped with it
        self._recorded = True
        native = finish.native_turn if finish is not None else None
        exchange = make_exchange(utterance=self.utterance, page=self.page, user=self.user_message,
                                 brief=brief_message(self.utterance, self.page), reply=raw, native=native,
                                 provider=provider or self.label or "unknown")
        self.conversation.record(exchange, self.info, self._summarize)

    async def _summarize(self, transcript: str) -> str:
        request = ChatRequest(system=SUMMARIZER_SYSTEM, messages=(ChatMessage("user", (TextPart(transcript),)),),
                              max_tokens=SUMMARY_MAX_TOKENS, purpose="summarizer")
        text, _outcome = await complete_chat(self.capabilities.get(Capability.SUMMARIZER), request)
        return text
