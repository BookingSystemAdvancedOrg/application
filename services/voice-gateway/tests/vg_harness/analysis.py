"""Facts derived from a recorded call. Owner: builder 'harness'.

Pure functions over `Result.events` (the frozen events of voice_gateway/events.py), so the assertions and the CLI
timeline do not depend on the brain builder's MetricsRecorder: they recompute the numbers from the raw events, which
also cross-checks the recorder. Times are integer ms on the call clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from voice_gateway.events import (AgentSentence, AgentTurnDone, BargeIn, CallEnded, LanguageAction, LanguageDecided, LanguageEvidence,
                                  PlaybackFinished, PlaybackFlushed, PlaybackStarted, SttFinal, ToolFinished,
                                  ToolStarted, TurnEnd, TurnHold, VadPause, VadResume, VadStart)

START_LANGUAGE = "sv"
ASSISTANT_KINDS = ("assistant", "fixed")


def of(events, *types):
    return [e for e in events if isinstance(e, types)]


def percentile(values: list[int], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


@dataclass(frozen=True)
class Reply:
    """One caller turn and the first audio that answered it."""
    turn_end: TurnEnd
    first_audio_ms: int | None          # first assistant or fixed PlaybackStarted at or after the TurnEnd

    @property
    def eos_to_first_audio(self) -> int | None:
        return None if self.first_audio_ms is None else self.first_audio_ms - self.turn_end.eos_ms

    @property
    def turn_detect(self) -> int:
        return self.turn_end.at_ms - self.turn_end.eos_ms


def replies(events) -> list[Reply]:
    ends = of(events, TurnEnd)
    starts = [e for e in of(events, PlaybackStarted) if e.kind in ASSISTANT_KINDS]
    out = []
    for i, end in enumerate(ends):
        limit = ends[i + 1].at_ms if i + 1 < len(ends) else None
        first = next((s.at_ms for s in starts if s.at_ms >= end.at_ms and (limit is None or s.at_ms < limit)), None)
        out.append(Reply(end, first))
    return out


def eos_to_first_audio(events) -> list[int]:
    return [r.eos_to_first_audio for r in replies(events) if r.eos_to_first_audio is not None]


def backchannels(events) -> list[PlaybackStarted]:
    return [e for e in of(events, PlaybackStarted) if e.kind == "backchannel"]


def backchannel_spans(events) -> list[tuple[int, int]]:
    """(start, end) of every backchannel that played, end from its PlaybackFinished (else start + duration)."""
    finished = {e.item_id: e for e in of(events, PlaybackFinished) if e.kind == "backchannel"}
    spans = []
    for s in backchannels(events):
        f = finished.get(s.item_id)
        spans.append((s.at_ms, f.at_ms if f else s.at_ms + s.duration_ms))
    return spans


def caller_onsets(events, resume_confirm_ms: int = 60) -> list[int]:
    """When the caller's voice (re)started: VadStart.speech_start_ms and VadResume at_ms minus its confirm time."""
    onsets = [e.speech_start_ms for e in of(events, VadStart)]
    onsets += [e.at_ms - resume_confirm_ms for e in of(events, VadResume)]
    return sorted(onsets)


def backchannel_overlaps(events, resume_confirm_ms: int = 60) -> int:
    """A backchannel counts as an overlap when the caller's voice started while it was playing."""
    onsets = caller_onsets(events, resume_confirm_ms)
    return sum(1 for a, b in backchannel_spans(events) if any(a < t < b for t in onsets))


def barge_ins(events) -> list[tuple[BargeIn, PlaybackFlushed | None]]:
    flushes = of(events, PlaybackFlushed)
    out = []
    for b in of(events, BargeIn):
        out.append((b, next((f for f in flushes if f.at_ms >= b.at_ms), None)))
    return out


def language_path(events, start: str = START_LANGUAGE) -> list[str]:
    """The call language over time, one entry per change (starts with `start`)."""
    path = [start]
    for e in events:
        code = None
        if isinstance(e, LanguageDecided) and e.action in (LanguageAction.SWITCH, LanguageAction.KEEP):
            code = e.current
        elif isinstance(e, AgentTurnDone):
            code = e.language
        if code and code != path[-1]:
            path.append(code)
    return path


def final_language(events, start: str = START_LANGUAGE) -> str:
    return language_path(events, start)[-1]


def switches(events, start: str = START_LANGUAGE) -> int:
    return len(language_path(events, start)) - 1


def sentences(events) -> list[AgentSentence]:
    return of(events, AgentSentence)


def playbacks(events) -> list[PlaybackStarted]:
    return of(events, PlaybackStarted)


def tools(events) -> list[ToolStarted]:
    return of(events, ToolStarted)


def holds(events) -> list[ToolFinished]:
    return [e for e in of(events, ToolFinished) if e.name == "hold_table"]


def turn_holds(events) -> list[TurnHold]:
    return of(events, TurnHold)


def call_end(events) -> CallEnded | None:
    ends = of(events, CallEnded)
    return ends[-1] if ends else None


def caller_texts(events) -> list[str]:
    return [e.text for e in of(events, TurnEnd)]


def final_texts(events) -> list[str]:
    return [e.text for e in of(events, SttFinal)]


# --- words, yes, times -------------------------------------------------------------------------------------

YES = re.compile(r"^\W*(ja|japp|jajamen|absolut|gärna|precis|det stämmer|stämmer|okej|yes|yeah|yep|sure|correct|"
                 r"that is right|that's right|please|naam|نعم|أجل|اجل|صحيح|oui|si|sí|ja bitte|evet|tak)\b", re.IGNORECASE)


def is_clear_yes(text: str) -> bool:
    return bool(YES.search(text or ""))


_NUM_SV = {"ett": 1, "en": 1, "två": 2, "tre": 3, "fyra": 4, "fem": 5, "sex": 6, "sju": 7, "åtta": 8, "nio": 9,
           "tio": 10, "elva": 11, "tolv": 12}
_NUM_EN = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
           "ten": 10, "eleven": 11, "twelve": 12}
_WORDS = "|".join(sorted({*_NUM_SV, *_NUM_EN}, key=len, reverse=True))
_CLOCK = re.compile(rf"\b(\d{{1,2}})[:.]([0-5]\d)\b")
_HOUR = re.compile(rf"\b(?:klockan|kl\.?|at|around|runt|omkring)\s+(\d{{1,2}}|{_WORDS})\b(?![:.]\d)", re.IGNORECASE)


def times_in(text: str) -> set[tuple[int, int | None]]:
    """Clock times mentioned in `text` as (hour, minute); minute None when only the hour was said ("klockan sju").
    The hour is as written (7 can mean 19:00; callers of this compare modulo 12)."""
    found: set[tuple[int, int | None]] = set()
    for m in _CLOCK.finditer(text or ""):
        if int(m.group(1)) < 24:
            found.add((int(m.group(1)), int(m.group(2))))
    for m in _HOUR.finditer(text or ""):
        word = m.group(1).lower()
        hour = int(word) if word.isdigit() else _NUM_SV.get(word) or _NUM_EN.get(word)
        if hour is not None and hour < 24:
            found.add((hour, None))
    return found


def time_allowed(spoken: tuple[int, int | None], allowed: set[tuple[int, int | None]]) -> bool:
    hour, minute = spoken
    for ah, am in allowed:
        if ah % 12 != hour % 12:
            continue
        if minute is None or am is None or minute == am:
            return True
    return False


def normalise_words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", (text or "").lower())


AMOUNT = re.compile(r"\b(kr|kronor|sek|krona|öre|kronors)\b|\d+\s?(kr|:-)|\bSEK\b|\b(crowns|kronor)\b", re.IGNORECASE)
