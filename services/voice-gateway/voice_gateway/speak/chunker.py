"""Sentence chunking of streamed agent text. Owner: builder 'converse'
(used by builder 'brain' inside brain/streaming.py).

SentenceChunker.feed(delta) returns every complete sentence the deltas so
far allow; flush() returns the rest at the end of a model round. Rules:
  * a sentence ends at . ? ! (and the agent's _SENTENCE_END set) followed
    by whitespace or the end of the round, NOT inside "kl. 18.30", "t.ex.",
    "bl.a.", "ca.", "nr.", "Mr.", decimals, times "18.30" / "18:30",
    dates "24.12.", ellipses, or a quoted name;
  * the first chunk of a turn is at least SpeakConfig.first_chunk_min_chars
    (a lone "Okej." waits for the next sentence and goes out joined);
  * a sentence over clause_split_chars may be cut at a comma or a
    semicolon past the middle, so TTS starts sooner on long read-backs;
  * whitespace normalised; empty chunks never returned.
Language agnostic apart from the abbreviation lists (sv, en, de, fr, es).
"""

from __future__ import annotations

from voice_gateway.config import SpeakConfig


class SentenceChunker:
    def __init__(self, cfg: SpeakConfig, language: str = "sv") -> None:
        self.cfg, self.language = cfg, language

    def feed(self, delta: str) -> list[str]:
        raise NotImplementedError

    def flush(self) -> list[str]:
        raise NotImplementedError

    def reset(self, *, first_of_turn: bool = False) -> None:
        raise NotImplementedError
