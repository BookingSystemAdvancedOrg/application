"""Checks a Result against its Scenario.expect. Owner: builder 'harness'.
One function per Expect field; check(scenario, result) runs every set
field and returns a list of failures (empty: pass), each naming the field,
the expected and the observed value.

Every number is recomputed from the recorded events (analysis.py), not read
from the brain's MetricsRecorder, so a recorder bug cannot hide a latency
or overlap regression. A check that has nothing to look at (no barge-in
happened, no booking was made where one was expected) FAILS: a scenario that
never exercised its point must not pass.
"""

from __future__ import annotations

from typing import Callable

from vg_harness import analysis as an
from vg_harness.runner import Result
from vg_harness.scenarios import Expect, Scenario

Check = Callable[[Scenario, Result], str | None]


def _step_spans(result: Result, step: int) -> list:
    return [s for s in result.caller_segments if s.step == step]


# --- hesitation and backchannels ---------------------------------------------------------------------------

def no_cutoff_before_ms(sc: Scenario, r: Result) -> str | None:
    """No TurnEnd while the caller is still inside the first step (its pauses and its last piece not yet started),
    and the first TurnEnd is at least `no_cutoff_before_ms` after the caller began."""
    want = sc.expect.no_cutoff_before_ms
    spans = _step_spans(r, 0)
    ends = an.of(r.events, an.TurnEnd)
    if not spans or not ends:
        return f"no_cutoff_before_ms: no caller step 0 or no TurnEnd recorded ({len(spans)} segments, {len(ends)} turns)"
    begin, last_start = spans[0].start_ms, spans[-1].start_ms
    first = ends[0]
    if first.at_ms < last_start:
        return (f"no_cutoff_before_ms: the turn ended at +{first.at_ms - begin} ms, inside the hesitation "
                f"(last words start at +{last_start - begin} ms)")
    if first.at_ms - begin < want:
        return f"no_cutoff_before_ms: first TurnEnd at +{first.at_ms - begin} ms, expected at or after +{want} ms"
    return None


def max_hesitation_acks(sc: Scenario, r: Result) -> str | None:
    spans = _step_spans(r, 0)
    if not spans:
        return "max_hesitation_acks: no caller step 0 recorded"
    begin, last_start = spans[0].start_ms, spans[-1].start_ms
    acks = [b.at_ms for b in an.backchannels(r.events) if begin <= b.at_ms < last_start]
    if len(acks) > sc.expect.max_hesitation_acks:
        return f"max_hesitation_acks: {len(acks)} backchannels inside the hesitation, expected at most {sc.expect.max_hesitation_acks}"
    return None


def backchannels_between(sc: Scenario, r: Result) -> str | None:
    lo, hi = sc.expect.backchannels_between
    spans = _step_spans(r, 0)
    if not spans:
        return "backchannels_between: no caller step 0 recorded"
    begin, end = spans[0].start_ms, spans[-1].end_ms
    n = sum(1 for b in an.backchannels(r.events) if begin <= b.at_ms <= end)
    return None if lo <= n <= hi else f"backchannels_between: {n} backchannels during the monologue, expected {lo} to {hi}"


def backchannel_overlaps(sc: Scenario, r: Result) -> str | None:
    n = an.backchannel_overlaps(r.events)
    return None if n == sc.expect.backchannel_overlaps else \
        f"backchannel_overlaps: {n} backchannels overlapped caller speech, expected {sc.expect.backchannel_overlaps}"


# --- barge-in ----------------------------------------------------------------------------------------------

def barge_in_stop_max_ms(sc: Scenario, r: Result) -> str | None:
    pairs = an.barge_ins(r.events)
    if not pairs:
        return "barge_in_stop_max_ms: the caller talked over the assistant but no BargeIn was recorded"
    worst = []
    for b, f in pairs:
        if f is None:
            return f"barge_in_stop_max_ms: BargeIn at {b.at_ms} ms was never followed by a PlaybackFlushed"
        worst.append(f.stop_ms)
    return None if max(worst) <= sc.expect.barge_in_stop_max_ms else \
        f"barge_in_stop_max_ms: stopped after {max(worst)} ms, expected at most {sc.expect.barge_in_stop_max_ms} ms"


def readback_repeated_after_barge_in(sc: Scenario, r: Result) -> str | None:
    pairs = an.barge_ins(r.events)
    if not pairs:
        return "readback_repeated_after_barge_in: no BargeIn recorded"
    at = pairs[0][0].at_ms
    hold_marks = {id(e) for e in an.holds(r.events)}
    again = [e for e in r.events if e.at_ms > at and (
        (isinstance(e, an.AgentTurnDone) and e.awaiting_confirmation) or id(e) in hold_marks)]
    if not again:
        return "readback_repeated_after_barge_in: no new read-back (hold or awaiting confirmation) after the barge-in"
    spoken = [p for p in an.playbacks(r.events) if p.kind in an.ASSISTANT_KINDS and p.at_ms > again[0].at_ms - 1]
    return None if spoken else "readback_repeated_after_barge_in: the repeated read-back was never played"


def readback_in_new_language_first(sc: Scenario, r: Result) -> str | None:
    """A switch happened before the yes: the confirmation must have been asked again in the new language."""
    want = sc.expect.final_language
    path = an.language_path(r.events)
    if len(path) < 2:
        return f"readback_in_new_language_first: the call never switched language (path {path})"
    booking = next((c for c in r.tool_calls if c["fn"] == "create-pending-reservation"), None)
    cutoff = booking["at_ms"] if booking else 1 << 60
    switch_at = next((e.at_ms for e in r.events if isinstance(e, (an.LanguageDecided, an.AgentTurnDone))
                      and getattr(e, "language", getattr(e, "current", None)) == path[1]), 0)
    again = [e for e in r.events if isinstance(e, an.AgentTurnDone) and e.awaiting_confirmation and
             switch_at <= e.at_ms <= cutoff and e.language == (want or path[-1])]
    return None if again else \
        "readback_in_new_language_first: no read-back in the new language before the booking was made"


# --- booking -----------------------------------------------------------------------------------------------

def _reservations(r: Result) -> list[dict]:
    return [c for c in r.tool_calls if c["fn"] == "create-pending-reservation" and c.get("status") == 201]


def party_size(sc: Scenario, r: Result) -> str | None:
    made = _reservations(r)
    held = [c for c in r.tool_calls if c.get("party") is not None]
    seen = made[-1]["party"] if made else (held[-1]["party"] if held else None)
    return None if seen == sc.expect.party_size else \
        f"party_size: the booking or last hold was for {seen}, expected {sc.expect.party_size}"


def booked(sc: Scenario, r: Result) -> str | None:
    n = len(_reservations(r))
    if sc.expect.booked and n != 1:
        return f"booked: {n} reservations were made, expected exactly 1"
    if sc.expect.booked is False and n:
        return f"booked: {n} reservations were made, expected none"
    return None


def booked_only_after_yes(sc: Scenario, r: Result) -> str | None:
    """Every reservation write came after a read-back the caller heard and a clear yes to it. A hold created after
    the yes (a new read-back) or a barge-in over the read-back resets it (the guard of tools.Executor, seen from
    outside)."""
    writes = _reservations(r)
    for w in writes:
        confirmed, held, heard = False, False, True
        hold_marks = {id(e) for e in an.holds(r.events)}
        for e in r.events:
            if e.at_ms > w["at_ms"]:
                break
            if id(e) in hold_marks:
                held, heard, confirmed = True, True, False
            elif isinstance(e, an.BargeIn) and held and not confirmed:
                heard = False
            elif isinstance(e, an.TurnEnd) and held and heard and an.is_clear_yes(e.text):
                confirmed = True
        if not confirmed:
            return f"booked_only_after_yes: a reservation was written at {w['at_ms']} ms without a heard read-back and a clear yes"
    return None


def no_invented_times(sc: Scenario, r: Result) -> str | None:
    allowed = set()
    for t in r.offered_times:
        h, m = t.split(":")
        allowed.add((int(h), int(m)))
    for text in an.caller_texts(r.events):
        allowed |= {(h, m) for h, m in an.times_in(text)}
    bad = []
    for s in an.sentences(r.events):
        for spoken in an.times_in(s.text):
            if not an.time_allowed(spoken, allowed):
                bad.append(f"{spoken[0]:02d}:{'--' if spoken[1] is None else f'{spoken[1]:02d}'}")
    return None if not bad else f"no_invented_times: the assistant said times the tools never returned: {sorted(set(bad))}"


def fee_rule(sc: Scenario, r: Result) -> str | None:
    """The fee rule is said (a fee sentence and the question whether the caller wants more) and no amount is spoken
    unless the caller asked for details."""
    texts = [s.text for s in an.sentences(r.events)]
    joined = " ".join(texts)
    has_rule = ("avgift" in joined.lower() and "Vill du ha mer information om det?" in joined) or \
               ("fee" in joined.lower() and "Would you like more information about that?" in joined)
    if not has_rule:
        return "fee_rule: the fee sentence and the question 'Vill du ha mer information om det?' were never said"
    asked = any(w in t.lower() for t in an.caller_texts(r.events)
                for w in ("hur mycket", "vad kostar", "mer information", "how much", "amount", "ja, berätta", "yes, tell"))
    if not asked and an.AMOUNT.search(joined):
        return "fee_rule: an amount was spoken although the caller did not ask for details"
    return None


# --- language ----------------------------------------------------------------------------------------------

def final_language(sc: Scenario, r: Result) -> str | None:
    got = an.final_language(r.events)
    return None if got == sc.expect.final_language else f"final_language: {got}, expected {sc.expect.final_language}"


def switches(sc: Scenario, r: Result) -> str | None:
    path = an.language_path(r.events)
    got = len(path) - 1
    return None if got == sc.expect.switches else f"switches: {got} (path {path}), expected {sc.expect.switches}"


def asked_language(sc: Scenario, r: Result) -> str | None:
    asked = any(isinstance(e, an.LanguageDecided) and e.action == an.LanguageAction.ASK for e in r.events)
    return None if asked == sc.expect.asked_language else f"asked_language: {asked}, expected {sc.expect.asked_language}"


def unsupported_reply(sc: Scenario, r: Result) -> str | None:
    from shared.features.phone_agent import copy
    known = " ".join((copy.unsupported_language(connect=True), copy.unsupported_language(True, connect=True))).lower()
    said = any(s.text.lower()[:40] in known for s in an.sentences(r.events) if s.text) or any(
        isinstance(e, an.LanguageDecided) and e.action == an.LanguageAction.UNSUPPORTED for e in r.events)
    return None if said == sc.expect.unsupported_reply else f"unsupported_reply: {said}, expected {sc.expect.unsupported_reply}"


def callback_topic(sc: Scenario, r: Result) -> str | None:
    topics = [c.get("topic") for c in r.callbacks]
    return None if sc.expect.callback_topic in topics else \
        f"callback_topic: callbacks recorded {topics}, expected {sc.expect.callback_topic}"


def voice_of_each_sentence_matches_language(sc: Scenario, r: Result) -> str | None:
    """Native voice guard: every text synthesised for an AgentSentence used that sentence's language, and that
    language has a voice (never another language's voice as a fallback)."""
    if not sc.expect.voice_of_each_sentence_matches_language:
        return None
    from voice_gateway import languages
    sent = {(s.text, s.language) for s in an.sentences(r.events)}
    texts = {t for t, _ in sent}
    for text, language, *_ in r.tts_calls:
        if text in texts and (text, language) not in sent:
            return f"voice_of_each_sentence_matches_language: a sentence was spoken in {language}, not its own language"
        if not languages.can_speak(language):
            return f"voice_of_each_sentence_matches_language: {language} has no voice but was synthesised"
    unspoken = [s for s in an.sentences(r.events) if not languages.can_speak(s.language)]
    if unspoken:
        return f"voice_of_each_sentence_matches_language: a sentence in {unspoken[0].language} (no voice) reached the speaker"
    return None


def eos_to_first_audio_p50_max_ms(sc: Scenario, r: Result) -> str | None:
    values = an.eos_to_first_audio(r.events)
    if not values:
        return "eos_to_first_audio_p50_max_ms: no reply audio followed any turn"
    p50 = an.percentile(values, 0.5)
    return None if p50 <= sc.expect.eos_to_first_audio_p50_max_ms else \
        f"eos_to_first_audio_p50_max_ms: p50 {p50:.0f} ms over {values}, expected at most {sc.expect.eos_to_first_audio_p50_max_ms} ms"


# --- the runner --------------------------------------------------------------------------------------------

# Expect fields where False is an expectation (no booking, no language question), not "off"
FALSE_MEANS_FALSE = {"booked", "asked_language", "unsupported_reply"}


def _active(name: str, value) -> bool:
    if name in FALSE_MEANS_FALSE:
        return value is not None
    return value is not None and value is not False


# (Expect field, check): in this order the failures are listed
CHECKS: tuple[tuple[str, Check], ...] = (
    ("no_cutoff_before_ms", no_cutoff_before_ms),
    ("max_hesitation_acks", max_hesitation_acks),
    ("backchannels_between", backchannels_between),
    ("backchannel_overlaps", backchannel_overlaps),
    ("barge_in_stop_max_ms", barge_in_stop_max_ms),
    ("readback_repeated_after_barge_in", readback_repeated_after_barge_in),
    ("readback_in_new_language_first", readback_in_new_language_first),
    ("party_size", party_size),
    ("booked", booked),
    ("booked_only_after_yes", booked_only_after_yes),
    ("no_invented_times", no_invented_times),
    ("fee_rule", fee_rule),
    ("final_language", final_language),
    ("switches", switches),
    ("asked_language", asked_language),
    ("unsupported_reply", unsupported_reply),
    ("callback_topic", callback_topic),
    ("voice_of_each_sentence_matches_language", voice_of_each_sentence_matches_language),
    ("eos_to_first_audio_p50_max_ms", eos_to_first_audio_p50_max_ms),
)


def checked_fields(expect: Expect) -> list[str]:
    """The Expect fields that are set (and so checked) for a scenario."""
    return [name for name, _ in CHECKS if _active(name, getattr(expect, name))]


def check(scenario: Scenario, result: Result) -> list[str]:
    if result.error:
        return [f"run: the call raised {result.error}"]
    failures = []
    for name, fn in CHECKS:
        if not _active(name, getattr(scenario.expect, name)):
            continue
        message = fn(scenario, result)
        if message:
            failures.append(message)
    return failures
