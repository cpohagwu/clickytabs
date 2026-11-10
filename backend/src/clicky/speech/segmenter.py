"""Streaming sentence segmenter for TTS.

The brain streams text; TTS works best (and starts soonest) on whole phrases. The first chunk is eager - the first
clause or sentence once it is long enough, or a short complete sentence like "Sure." - so the first audio starts
early; after that, whole sentences, with very long ones cut at a clause or word boundary. Markdown is stripped: it
is never meant to be read aloud.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

_SENTENCE_PUNCT = re.compile(r"[.!?…]+[\"')\]]?(?=\s)")
_CLAUSE_PUNCT = re.compile(r"[,;:—–](?=\s)")
_LAST_WORD = re.compile(r"(\S+)$")
_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "no", "e.g", "i.e", "eg", "ie", "approx", "fig"}
_MARKDOWN = re.compile(r"\*\*|__|`+|^#+\s*|^\s*[-*+]\s+|^\s*\d+\.\s+(?=\S)", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def clean_for_speech(text: str) -> str:
    text = _LINK.sub(r"\1", text)
    text = _MARKDOWN.sub("", text)
    return " ".join(text.split())


def _sentence_ends(text: str) -> list[int]:
    ends = []
    for match in _SENTENCE_PUNCT.finditer(text):
        if match.group().startswith("."):
            word = _LAST_WORD.search(text[: match.start()])
            token = word.group(1).lower().rstrip(".") if word else ""
            if token in _ABBREVIATIONS or (len(token) == 1 and token.isalpha()):   # "Dr." / "J. Smith"
                continue
        ends.append(match.end())
    return ends


@dataclass(frozen=True)
class SpeechPiece:
    """One chunk to speak, plus the items (visual directives) marked inside it at character offsets of `text`."""

    text: str
    marks: tuple[tuple[int, object], ...] = ()


def _spoken_offset(raw: str, pos: int, spoken: str) -> int:
    """Where raw offset `pos` lands in clean_for_speech(raw) (== spoken): the start of the word that follows."""
    prefix = raw[:pos]
    at = len(clean_for_speech(prefix))
    if at and at < len(spoken) and prefix[-1:].isspace():
        at += 1
    return min(at, len(spoken))


class SentenceSegmenter:
    def __init__(self, *, first_chunk_min_chars: int = 24, max_chars: int = 220) -> None:
        self.first_chunk_min_chars = first_chunk_min_chars
        self.max_chars = max_chars
        self._buffer = ""
        self._emitted = 0
        self._marks: list[tuple[int, object]] = []      # (raw offset into the buffer, item)

    def mark(self, item: object) -> None:
        """Attach `item` to the current end of the text (it belongs to the words that follow)."""
        self._marks.append((len(self._buffer), item))

    def push(self, text: str) -> Iterator[SpeechPiece]:
        self._buffer += text
        while (cut := self._next_cut()) is not None:
            if (piece := self._take(cut, final=False)) is not None:
                yield piece

    def finish(self) -> Iterator[SpeechPiece]:
        """The rest. A piece with empty text carries marks that came after the last spoken word."""
        if (piece := self._take(len(self._buffer), final=True)) is not None:
            yield piece

    def feed(self, text: str) -> Iterator[str]:
        for piece in self.push(text):
            yield piece.text

    def flush(self) -> Iterator[str]:
        for piece in self.finish():
            if piece.text:
                yield piece.text

    def _take(self, cut: int, *, final: bool) -> SpeechPiece | None:
        raw, self._buffer = self._buffer[:cut], self._buffer[cut:]
        mine = [(pos, item) for pos, item in self._marks if pos <= cut]
        self._marks = [(pos - cut, item) for pos, item in self._marks if pos > cut]
        spoken = clean_for_speech(raw)
        if not spoken:
            if mine and final:
                return SpeechPiece("", tuple((0, item) for _pos, item in mine))
            self._marks[:0] = [(0, item) for _pos, item in mine]   # no words here: they belong to the next piece
            return None
        self._emitted += 1
        return SpeechPiece(spoken, tuple((_spoken_offset(raw, pos, spoken), item) for pos, item in mine))

    def _next_cut(self) -> int | None:
        buffer = self._buffer
        sentences = _sentence_ends(buffer)
        if self._emitted == 0:
            clauses = [m.end() for m in _CLAUSE_PUNCT.finditer(buffer)]
            long_enough = [end for end in (*clauses, *sentences) if end >= self.first_chunk_min_chars]
            if long_enough:
                return min(long_enough)
            if sentences:
                return sentences[0]            # a short complete sentence ("Sure.") goes out as is
        elif sentences:
            return sentences[0]
        if len(buffer) > self.max_chars:
            window = buffer[: self.max_chars]
            clauses = [m.end() for m in _CLAUSE_PUNCT.finditer(window) if m.end() > self.max_chars // 2]
            if clauses:
                return clauses[-1]
            space = window.rfind(" ")
            return space if space > 0 else self.max_chars
        return None
