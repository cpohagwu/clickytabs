"""Conversation history: follow-up questions continue the conversation instead of starting over.

One conversation per browser session (the extension keeps its id in chrome.storage.session and sends it in `hello`,
so a service-worker restart or a reconnect resumes it). Each answered turn is stored as an `Exchange`: the user turn
exactly as it was sent (page context included), a brief text-only form of it, the raw reply (tags included) and the
provider-native assistant turn.

How a request replays the history depends on the answering model (ModelInfo.history):
- "verbatim" (Claude): every earlier user turn byte-identical to what was sent, and the native assistant turns
  (thinking blocks included). The history is append-only: Claude's preserved thinking and its prompt cache both break
  if an earlier turn changes, so page context stays in the turns that carried it.
- "compact" (local models): earlier turns as brief text, page context only in the newest user turn.

Epochs keep it bounded: when an epoch passes its exchange or token limit, the summarizer condenses it in the background
and the next turn starts a new epoch from that summary ("simple compaction": nothing of the old epoch is replayed).
Exchanges that arrive while the summary is being written are kept, but without their native turns: their thinking
blocks were produced over the old prefix. A conversation idle for IDLE_RESET_S starts afresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from clicky.capabilities.base import ChatMessage, ImagePart, ModelInfo, TextPart
from clicky.protocol.page import PageContext

log = logging.getLogger(__name__)

IDLE_RESET_S = 20 * 60
SUMMARY_WAIT_S = 3.0             # a new turn waits this long for an epoch summary still being written
SUMMARY_MAX_WORDS = 120
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@dataclass(frozen=True)
class EpochLimit:
    max_exchanges: int
    max_tokens: int


EPOCH_LIMITS = {
    "verbatim": EpochLimit(max_exchanges=12, max_tokens=64_000),   # page context stays in every turn
    "compact": EpochLimit(max_exchanges=6, max_tokens=2_500),      # brief turns only; small context windows
}

SUMMARY_PREFIX = "Notes on our conversation so far (written by you, ClickyTabs, for yourself):\n"
SUMMARIZER_SYSTEM = (
    "You condense a conversation between a user and ClickyTabs, a voice companion in the user's browser, into "
    f"notes for continuing it. At most {SUMMARY_MAX_WORDS} words of plain text: what the user wanted, what was "
    "answered or shown, the pages involved (by title), and anything still open. Leave out element ids and tags."
)


def estimate_tokens(message: ChatMessage) -> int:
    """Rough size: ~3.5 characters per token; images by Claude's width*height/750 rule (capped)."""
    tokens = 0
    for part in message.parts:
        if isinstance(part, TextPart):
            tokens += len(part.text) * 2 // 7 + 1
        else:
            tokens += 1_600
    return tokens


def _image_tokens(page: PageContext | None) -> int:
    shot = page.screenshot if page is not None else None
    return min(1_600, shot.width * shot.height // 750) if shot else 0


_TAG = re.compile(r"<[^<>]{0,400}>")


def spoken_text(reply: str) -> str:
    """A reply without its tags, for summaries."""
    return " ".join(_TAG.sub("", reply).split())


@dataclass(frozen=True)
class Exchange:
    utterance: str
    page_title: str | None
    user: ChatMessage                # exactly as sent to the model that answered
    brief: ChatMessage               # text-only form ("compact" replay)
    reply: str                       # raw reply, tags included
    native: object | None            # provider-native assistant turn (replayed to the same provider only)
    provider: str                    # chain label of the answering model
    tokens: int                      # size when replayed verbatim
    brief_tokens: int                # size when replayed compact


def make_exchange(*, utterance: str, page: PageContext | None, user: ChatMessage, brief: ChatMessage, reply: str,
                  native: object | None, provider: str) -> Exchange:
    reply_tokens = len(reply) * 2 // 7 + 1
    tokens = estimate_tokens(user) + reply_tokens
    if any(isinstance(p, ImagePart) for p in user.parts):
        tokens += _image_tokens(page) - 1_600
    return Exchange(utterance=utterance, page_title=page.title if page else None, user=user, brief=brief,
                    reply=reply, native=native, provider=provider, tokens=max(tokens, 1),
                    brief_tokens=estimate_tokens(brief) + reply_tokens)


Summarize = Callable[[str], Awaitable[str]]


class Conversation:
    def __init__(self, conversation_id: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.id = conversation_id
        self.epoch = 0
        self.answered = 0                 # exchanges recorded since the conversation (re)started, across epochs
        self.summary: str | None = None
        self.exchanges: list[Exchange] = []
        self._clock = clock
        self.last_active = clock()
        self._summarizing: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------------------------------------- reading

    def messages_for(self, info: ModelInfo | None, provider: str, newest: ChatMessage) -> tuple[ChatMessage, ...]:
        """The request's messages: [summary], the epoch's exchanges, then the newest user turn."""
        verbatim = info is not None and info.history == "verbatim"
        messages: list[ChatMessage] = []
        if self.summary:
            messages.append(ChatMessage("user", (TextPart(SUMMARY_PREFIX + self.summary),)))
        for exchange in self.exchanges:
            messages.append(exchange.user if verbatim else exchange.brief)
            native = exchange.native if exchange.provider == provider else None
            messages.append(ChatMessage("assistant", (TextPart(exchange.reply),), native=native))
        messages.append(newest)
        return tuple(messages)

    def tokens(self, policy: str) -> int:
        """Rough size of the epoch as a request replays it under `policy`."""
        return sum(e.tokens if policy == "verbatim" else e.brief_tokens for e in self.exchanges)

    # ------------------------------------------------------------------------------------------------- writing

    def touch(self) -> None:
        """Called when a turn starts: an idle conversation starts afresh."""
        now = self._clock()
        if self.exchanges and now - self.last_active > IDLE_RESET_S:
            log.info("conversation %s idle for %.0f min: starting afresh", self.id, (now - self.last_active) / 60)
            self.reset()
        self.last_active = now

    def reset(self) -> None:
        if self._summarizing is not None:
            self._summarizing.cancel()
            self._summarizing = None
        self.epoch += 1
        self.answered = 0
        self.summary = None
        self.exchanges = []

    def record(self, exchange: Exchange, info: ModelInfo | None, summarize: Summarize | None) -> None:
        self.exchanges.append(exchange)
        self.answered += 1
        self.last_active = self._clock()
        policy = info.history if info is not None else "compact"
        limit = EPOCH_LIMITS[policy]
        over = len(self.exchanges) >= limit.max_exchanges or self.tokens(policy) >= limit.max_tokens
        if over and summarize is not None and self._summarizing is None:
            covered = len(self.exchanges)        # what the summary covers: fixed now, not when the task first runs
            self._summarizing = asyncio.create_task(
                self._roll_over(summarize, covered, self.transcript(self.exchanges[:covered])),
                name=f"summarize-{self.id}")

    async def ready(self, timeout_s: float = SUMMARY_WAIT_S) -> None:
        """Wait (briefly) for an epoch summary in progress, so the next turn starts the new epoch."""
        task = self._summarizing
        if task is None:
            return
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout_s)

    def transcript(self, exchanges: list[Exchange]) -> str:
        lines = [f"Earlier notes: {self.summary}"] if self.summary else []
        for exchange in exchanges:
            page = f' (on "{exchange.page_title}")' if exchange.page_title else ""
            lines.append(f"User{page}: {exchange.utterance}")
            lines.append(f"ClickyTabs: {spoken_text(exchange.reply)}")
        return "\n".join(lines)

    async def _roll_over(self, summarize: Summarize, covered: int, transcript: str) -> None:
        epoch = self.epoch
        try:
            summary = " ".join((await summarize(transcript)).split())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("summarizer failed (%s); keeping the tail of the epoch as notes", exc)
            summary = transcript[-1_200:]
        finally:
            self._summarizing = None
        if self.epoch != epoch:           # reset meanwhile
            return
        # Exchanges recorded while summarizing survive as text only (their thinking belongs to the old prefix).
        tail = [replace(e, native=None) for e in self.exchanges[covered:]]
        self.summary = summary or None
        self.exchanges = tail
        self.epoch += 1
        log.info("conversation %s: epoch %d starts from a %d-word summary", self.id, self.epoch,
                 len(summary.split()))


class ConversationStore:
    """In-memory conversations, most recently used last (a backend restart forgets them)."""

    def __init__(self, *, max_conversations: int = 32, clock: Callable[[], float] = time.monotonic) -> None:
        self._items: OrderedDict[str, Conversation] = OrderedDict()
        self._max = max_conversations
        self._clock = clock

    def __len__(self) -> int:
        return len(self._items)

    def get(self, conversation_id: str | None) -> Conversation:
        """The conversation with this id; a new one (with this id if it is well-formed) otherwise."""
        if conversation_id and (conversation := self._items.get(conversation_id)) is not None:
            self._items.move_to_end(conversation_id)
            return conversation
        if not (conversation_id and CONVERSATION_ID.match(conversation_id)):
            conversation_id = uuid.uuid4().hex
        conversation = Conversation(conversation_id, clock=self._clock)
        self._items[conversation_id] = conversation
        while len(self._items) > self._max:
            _old_id, old = self._items.popitem(last=False)
            old.reset()
        return conversation

    def new(self) -> Conversation:
        return self.get(None)

    def close(self) -> None:
        for conversation in self._items.values():
            conversation.reset()
        self._items.clear()
