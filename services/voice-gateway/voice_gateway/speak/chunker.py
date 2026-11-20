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

Ambiguous periods (after a number, "osv.", "etc.") are decided by the next
word: a capital letter starts a new sentence ("kl 18.30. Passar det?"),
anything else continues it ("den 24.12. klockan 18"). Such a period waits
for the next delta (or the end of the round) before it is cut.
"""

from __future__ import annotations

import re

from voice_gateway.config import SpeakConfig

SENTENCE_END = ".?!。？！؟።"      # the agent's _SENTENCE_END
_NO_SPACE_NEEDED = "。？！"       # CJK: no whitespace after the stop
_CLOSERS = "\"'”’»)]"
_OPEN_QUOTES = "“«„"
_CLOSE_QUOTES = "”»"
_MAX_QUOTE_CHARS = 120             # an unclosed quote longer than this is not a quote

# The word before the period, lowercased, without that period. Never a sentence end.
ABBREVIATIONS = frozenset({
    # sv
    "kl", "t.ex", "bl.a", "ca", "nr", "dvs", "d.v.s", "s.k", "fr.o.m", "t.o.m", "p.g.a", "pga", "inkl", "exkl",
    "tel", "resp", "jfr", "fr", "ev", "f.d",
    # en
    "mr", "mrs", "ms", "dr", "prof", "e.g", "i.e", "approx", "vs", "jr", "sr", "st", "mt",
    # de
    "z.b", "bzw", "u.a", "ggf", "d.h", "str", "hr", "fr", "nr", "inkl", "evtl", "ca",
    # fr
    "m", "mme", "mlle", "env", "p.ex", "n°",
    # es
    "sr", "sra", "srta", "aprox", "núm", "p.ej", "ud", "uds",
})
# Abbreviations that may also end a sentence: decided by the next word's case.
AMBIGUOUS = frozenset({"osv", "o.s.v", "etc", "m.m", "m.fl", "usw", "inc", "ltd", "ab", "kr", "sek", "st"})
_MONTHS = frozenset("januar februar märz april mai juni juli august september oktober november dezember jan feb "
                    "mär apr jun jul aug sep sept okt nov dez".split())
_NUMBERISH = re.compile(r"^\d+([.:,]\d+)*$")
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


class SentenceChunker:
    def __init__(self, cfg: SpeakConfig, language: str = "sv") -> None:
        self.cfg, self.language = cfg, language
        self._buf = ""
        self._held = ""             # short first sentences waiting for company
        self._first = True

    # -- public -------------------------------------------------------------------------------
    def feed(self, delta: str) -> list[str]:
        self._buf += delta or ""
        out: list[str] = []
        while True:
            end = self._boundary(final=False)
            if end is None:
                break
            sentence, self._buf = self._buf[:end], self._buf[end:].lstrip()
            out += self._emit(_norm(sentence), complete=True)
        pending = _norm(self._buf)
        if len(pending) > self.cfg.clause_split_chars:
            cut = self._clause_cut(pending)
            if cut is not None:
                head = pending[:cut]
                self._buf = pending[cut:].lstrip()
                out += self._emit(head, complete=False)
        return out

    def flush(self) -> list[str]:
        out: list[str] = []
        while True:
            end = self._boundary(final=True)
            if end is None:
                break
            sentence, self._buf = self._buf[:end], self._buf[end:].lstrip()
            out += self._emit(_norm(sentence), complete=True)
        rest = _norm(self._buf)
        self._buf = ""
        if rest:
            out += self._emit(rest, complete=True)
        if self._held:
            out.append(self._held)
            self._held, self._first = "", False
        return out

    def reset(self, *, first_of_turn: bool = False) -> None:
        self._buf, self._held, self._first = "", "", first_of_turn

    # -- emitting -----------------------------------------------------------------------------
    def _emit(self, text: str, *, complete: bool) -> list[str]:
        if not text:
            return []
        pieces = self._split_long(text) if complete else [text]
        out = []
        for piece in pieces:
            if self._first:
                self._held = f"{self._held} {piece}".strip()
                if len(self._held) >= self.cfg.first_chunk_min_chars:
                    out.append(self._held)
                    self._held, self._first = "", False
            else:
                out.append(piece)
        return out

    def _split_long(self, text: str) -> list[str]:
        pieces = []
        while len(text) > self.cfg.clause_split_chars:
            cut = self._clause_cut(text)
            if cut is None:
                break
            pieces.append(text[:cut])
            text = text[cut:].lstrip()
        if text:
            pieces.append(text)
        return pieces

    @staticmethod
    def _clause_cut(text: str) -> int | None:
        """Index just after the first ", " or "; " past the middle (outside
        quotes and not between digits), None when there is none."""
        middle = len(text) // 2
        quote = False
        for i, c in enumerate(text):
            if c in '"“”«»„':
                quote = not quote if c == '"' else c in _OPEN_QUOTES
            if i < middle or quote or c not in ",;":
                continue
            if i + 1 < len(text) and text[i + 1] == " " and i + 2 < len(text):
                return i + 1
        return None

    # -- boundaries ---------------------------------------------------------------------------
    def _boundary(self, *, final: bool) -> int | None:
        """End index (exclusive) of the first complete sentence in the buffer,
        None when none can be decided yet (final: the round is over)."""
        buf = self._buf
        n = len(buf)
        quote_open_at: int | None = None
        i = 0
        while i < n:
            c = buf[i]
            if c == '"':
                quote_open_at = None if quote_open_at is not None else i
            elif c in _OPEN_QUOTES:
                quote_open_at = i
            elif c in _CLOSE_QUOTES:
                quote_open_at = None
            if c == "…":
                i += 1
                continue
            if c not in SENTENCE_END:
                i += 1
                continue
            j = i + 1
            if c == "." and j < n and buf[j] == ".":
                while j < n and buf[j] == ".":     # an ellipsis is a pause, not an end
                    j += 1
                i = j
                continue
            in_quote = quote_open_at is not None and i - quote_open_at <= _MAX_QUOTE_CHARS
            k = j
            while k < n and buf[k] in _CLOSERS:
                if buf[k] in '"”»':
                    in_quote = False
                k += 1
            if in_quote:
                i += 1
                continue
            if k >= n:
                return n if final else None
            if c in _NO_SPACE_NEEDED:
                return k
            if not buf[k].isspace():
                i += 1
                continue
            if c != ".":
                return k
            verdict = self._period(buf, i, k, final)
            if verdict is None:
                return None
            if verdict:
                return k
            i += 1
        return None

    def _period(self, buf: str, i: int, after: int, final: bool) -> bool | None:
        """Is the period at `i` a sentence end? None: wait for more text."""
        start = i
        while start > 0 and not buf[start - 1].isspace():
            start -= 1
        word = buf[start:i].lower().lstrip("(\"'“«„")
        if word in ABBREVIATIONS:
            return False
        if len(word) == 1 and word.isalpha():
            return False            # an initial: "Anna B. Svensson"
        if not (word in AMBIGUOUS or _NUMBERISH.match(word)):
            return True
        m = re.compile(r"\S+").search(buf, after)
        if m is None:
            return True if final else None
        nxt = m.group(0).strip("(\"'“«„")
        if not nxt:
            return True
        if nxt.lower().rstrip(".,") in _MONTHS:
            return False            # "am 5. Mai"
        return nxt[0].isupper()
