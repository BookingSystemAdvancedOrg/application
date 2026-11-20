"""The agent loop (ADR-L decision 2): one caller utterance in, one spoken
reply out. Builds the system prompt, sends the conversation and the tool
schemas to the `AgentModel` port, runs the tool uses inside Booqy
(`tools.Executor`), feeds the results back and returns the final text.
The voice provider never sees a tool definition, a tool result or tenant
data: it gets only the returned text."""

from __future__ import annotations

import inspect
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import date, timedelta

from shared import agent_model
from shared.features.phone_agent import copy, language, readback
from shared.features.phone_agent.tools import TOOLS, read_as

MAX_ROUNDS = 4          # model passes per caller utterance (tool calls in between)
MAX_TOKENS = 400
TURN_BUDGET_S = 8.0     # model and tools per caller utterance, from its start: then the still-checking sentence
MIN_ROUND_S = 1.0       # no new model round with less left than this
KEEP_TOOL_ROUNDS = 2    # tool results older than this many rounds are shortened in the history
MAX_HISTORY_BYTES = 120_000
WRITES = ("create_reservation", "move_reservation", "cancel_reservation", "request_callback", "transfer_to_staff",
          "send_guarantee_link")
# Reads that change no booking state beyond what running them again in order repeats: when a round asks
# for several, their Lambda calls run in parallel first (the answers are cached), then they run in order.
PARALLEL_READS = ("get_restaurant_info", "get_availability", "get_menu")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass
class TurnResult:
    reply: str
    tool_calls: list = field(default_factory=list)   # [{"name", "ok"}]
    latency_ms: int = 0
    error: bool = False
    language: str | None = None        # the model's `[<code>]` tag of the reply (any known language)
    rounds: int = 0
    cut: bool = False                  # the deadline came first: the caller heard the still-checking sentence
    model_ms: int = 0                  # time spent waiting for the model (all rounds)
    tool_ms: int = 0                   # time spent in tools (all rounds)
    cache_read: int = 0                # prompt tokens read from Bedrock's prompt cache (all rounds)
    cache_write: int = 0               # prompt tokens written to it


def calendar(today: date, last: date) -> str:
    """Every bookable day with its weekday, so the model never works out a
    date or a weekday itself (English; the model translates)."""
    lines = []
    day = today
    while day <= last:
        note = " (today)" if day == today else " (tomorrow)" if day == today + timedelta(days=1) else ""
        lines.append(f"{day.isoformat()} {_WEEKDAYS[day.weekday()]}{note}")
        day += timedelta(days=1)
    return "\n".join(lines)


def _self_name(agent_name):
    """How the model names itself: the restaurant's own agent name, else
    Booqy-assistenten (translated in other languages)."""
    name = copy.agent_name(agent_name)
    if name == copy.DEFAULT_AGENT_NAME:
        return f"{name} (in other languages: the Booqy assistant, translated)"
    return name


def system_prompt(lang, agent_name, restaurant, today: date, tone="formal", *, last_day=None, disclose_now=False,
                  phone_known=True, unsupported=None, resume=False, switched=False, asked=None,
                  locked=False) -> str:
    """The instruction prompt for one turn in `lang` (copy.SYSTEM_PROMPT).

    The static part (the same in every turn of a call in one language) comes
    first; the notes for this turn only follow under copy.TURN_SECTION, so
    the static part is read from Bedrock's prompt cache: `disclose_now`,
    `unsupported`, `resume` (the previous turn was cut by the deadline,
    copy.RESUME), `switched` (the caller just switched to `lang`: read back
    again, copy.SWITCH_NOTE), `asked` (Booqy asked whether to continue in
    that language, copy.ASKED_NOTE) and `locked` (no more switches)."""
    name = language.name(lang) if lang in language.NAMES else "Swedish"
    last_day = last_day or today + timedelta(days=21)
    register = copy.REGISTER.get(tone or "formal", copy.REGISTER["formal"]).format(language_name=name)
    prompt = copy.SYSTEM_PROMPT.format(
        agent=_self_name(agent_name),
        restaurant=" ".join(str(restaurant or "restaurangen").split()),
        language_name=name, register=register, openers=copy.OPENERS.get(lang) or copy.OPENERS_OTHER,
        phone_known=copy.PHONE_KNOWN if phone_known else copy.PHONE_HIDDEN,
        horizon_last=f"{last_day.isoformat()} ({_WEEKDAYS[last_day.weekday()]})",
        calendar=calendar(today, last_day))
    notes = "".join((
        copy.LOW_ASR.format(language_name=name) if lang in language.LOW_ASR else "",
        copy.DISCLOSE_NOW.format(language_name=name) if disclose_now else "",
        copy.SWITCH_NOTE.format(language_name=name) if switched else "",
        copy.ASKED_NOTE.format(language_name=name, asked=asked, asked_name=language.name(asked))
        if asked and asked != lang else "",
        copy.LOCKED_NOTE.format(language_name=name) if locked else "",
        copy.UNSUPPORTED.format(unsupported=unsupported, unsupported_name=language.name(unsupported))
        if unsupported else "",
        copy.RESUME if resume else ""))
    return prompt + copy.TURN_SECTION + notes.strip() if notes else prompt


def _ok(result) -> bool:
    return isinstance(result, dict) and result.get("ok") is not False


def error_sentence(lang):
    return copy.text(copy.ERROR, lang or "sv")


def still_checking(lang, spoken=(), n=0):
    """The sentence of a turn cut by the deadline: a fixed one in `lang`
    (varied by `n`, the caller turn), else the model's own announcement from
    this turn in that language ("one moment, I am checking"), else English."""
    fixed = copy.still_checking_line(lang or "sv", n)
    if fixed:
        return fixed
    own = [text for tagged, text, _ in spoken if tagged in (None, lang)]
    return own[-1] if own else copy.text(copy.STILL_CHECKING, lang)


class _Late(Exception):
    """The deadline came before the model or a read answered."""


def _within(fn, seconds):
    """`fn()` if it answers within `seconds`, else _Late. The worker is left
    to finish on its own (it holds no state the turn saves)."""
    if seconds <= 0:
        raise _Late
    pool = ThreadPoolExecutor(1)
    future = pool.submit(fn)
    pool.shutdown(wait=False)
    try:
        return future.result(timeout=seconds)
    except FutureTimeout:
        raise _Late from None


def _takes_timeout(fn):
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "timeout_s" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def _converse(model, system, messages, seconds):
    snapshot = list(messages)     # the worker never sees the list grow
    extra = {"timeout_s": seconds} if _takes_timeout(model.converse) else {}
    return _within(lambda: model.converse(system, snapshot, TOOLS, max_tokens=MAX_TOKENS, **extra), seconds)


def _prefetch(executor, uses, seconds):
    """Several reads in one round (two days, the menu and the hours): their
    Lambda calls run at the same time on forks, whose GET answers warm the
    executor's cache, so the reads in order (which change the call's state)
    take one round trip instead of several (live test 2026-11-20: p95 6.8 s
    for tool turns). Anything late is simply run again in order."""
    reads = [u for u in uses if u.name in PARALLEL_READS]
    if len(reads) < 2 or not hasattr(executor, "fork") or seconds <= 0:
        return
    twins = [executor.fork() for _ in reads]
    pool = ThreadPoolExecutor(len(reads))
    futures = [pool.submit(twin.run, use.name, use.input) for twin, use in zip(twins, reads)]
    pool.shutdown(wait=False)
    deadline = time.monotonic() + seconds
    for twin, future in zip(twins, futures):
        try:
            future.result(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:  # noqa: BLE001 - a late or failed prefetch is run again in order
            continue
        if hasattr(executor, "_cache"):
            executor._cache.update(twin._cache)


def _heard(executor, text, lang, *, error=False):
    """Tells the executor what the caller heard at the end of the turn (the
    read-back and the card terms are checked against it: tools.Executor.heard)."""
    if hasattr(executor, "heard"):
        executor.heard(text, lang, error=error)


def _tool(executor, use, seconds):
    """A write always runs to its end on the call's state (never left
    half-known: no double booking); a read runs on a fork that is joined
    only when it answers in time."""
    if use.name in WRITES:
        return executor.run(use.name, use.input)
    if not hasattr(executor, "fork"):
        return _within(lambda: executor.run(use.name, use.input), seconds)
    twin = executor.fork()
    result = _within(lambda: twin.run(use.name, use.input), seconds)
    executor.join(twin)
    return result


_LATE = {"ok": False, "code": "timeout", "say": "This did not finish in time. Run it again."}


_KEEP_KEYS = ("ok", "code", "holdId", "reservationId", "date", "weekday", "time", "partySize", "reservations",
              "guaranteeRequired", "guaranteeTerms", "pending")   # the terms: amounts are said only when asked
SHORT_RESULT = 400       # bytes: an older tool result above this is shortened


def _shorten(content):
    try:
        data = json.loads(content) if isinstance(content, str) else content
    except ValueError:
        return json.dumps({"summary": "older result"})
    if not isinstance(data, dict):
        return json.dumps({"summary": "older result"})
    kept = {k: data[k] for k in _KEEP_KEYS if k in data}
    return json.dumps({**kept, "summary": "older result, shortened"}, ensure_ascii=False, default=str)


def compact(messages):
    """Keeps the stored conversation small (each round re-sends it): large
    tool results older than the last KEEP_TOOL_ROUNDS rounds keep only their
    ids and the booking facts, and above MAX_HISTORY_BYTES the oldest caller
    turns are dropped whole (a caller text up to the next one), so tool uses
    and their results stay paired."""
    rounds = [i for i, m in enumerate(messages) if m.get("role") == "user" and isinstance(m.get("content"), list)
              and any(b.get("type") == "tool_result" for b in m["content"])]
    for i in rounds[:-KEEP_TOOL_ROUNDS]:
        messages[i] = {"role": "user", "content": [
            {**b, "content": _shorten(b.get("content"))}
            if b.get("type") == "tool_result" and len(str(b.get("content") or "")) > SHORT_RESULT else b
            for b in messages[i]["content"]]}
    while len(dumps(messages).encode("utf-8")) > MAX_HISTORY_BYTES:
        starts = [i for i, m in enumerate(messages) if m.get("role") == "user" and isinstance(m.get("content"), str)]
        if len(starts) < 2:
            break
        del messages[:starts[1]]
    return messages


def run_turn(model, messages: list, caller_text: str, executor, *, system: str, lang="sv",
             budget_s=TURN_BUDGET_S, keep_preamble=False) -> TurnResult:
    """Appends the caller's text and the agent's turns to `messages` (in
    place) and returns the reply. A model error or too many rounds ends the
    turn with the fixed error sentence in the call's language (the caller is
    offered a callback).

    The deadline: model rounds and tools together get `budget_s` from the
    start of the turn, enforced here (a model call or a read that has not
    answered in time is no longer waited for) and passed to the model as its
    timeout. When it runs out the caller hears the still-checking sentence
    and the work is kept: the conversation holds every finished tool result
    (a read that did not finish answers `timeout`), what this turn looked up
    counts as looked up in the next one (`executor.not_heard`: a write still
    needs a read-back) and the next turn resumes (copy.RESUME). A write is
    never abandoned half-way.

    Text the model writes before a lookup or a write ("one moment, I am
    checking") is left out when a later round answers: the caller hears the
    whole reply only once the turn ends, so it would only repeat. The
    goodbye written with end_call is kept, and so is text that carries the
    card guarantee's fee rule (live test 2026-11-20: dropped, never said);
    `keep_preamble` keeps it all (the model was asked to say the disclosure
    sentence itself in this reply). The reply the caller hears is passed to
    `executor.heard` (the read-back and the terms are checked against it)."""
    started = time.monotonic()
    deadline = started + budget_s
    compact(messages)
    messages.append({"role": "user", "content": caller_text})
    calls = []
    if hasattr(executor, "caller_text"):
        executor.caller_text = caller_text      # the server checks holds against what the caller said
    already = copy.pick(copy.ALREADY_BOOKED, lang)
    if already and not keep_preamble and hasattr(executor, "repeated_yes") and executor.repeated_yes(caller_text):
        # A yes after the booking was made: no model round, nothing booked again (retest 2026-11-20).
        messages.append({"role": "assistant", "content": already})
        _heard(executor, already, lang)
        return TurnResult(already, [], int((time.monotonic() - started) * 1000), language=lang)
    reask = executor.day_reask(caller_text, lang) if not keep_preamble and hasattr(executor, "day_reask") else None
    if reask:
        # A bare yes or no to "i dag eller nästa fredag?": Booqy asks in other words, without a model round.
        messages.append({"role": "assistant", "content": reask})
        _heard(executor, reask, lang)
        return TurnResult(reask, [], int((time.monotonic() - started) * 1000), language=lang)

    def ms():
        return int((time.monotonic() - started) * 1000)

    def failed(rounds):
        text = error_sentence(lang)
        messages.append({"role": "assistant", "content": text})
        _heard(executor, text, lang, error=True)
        return done(TurnResult(text, calls, ms(), error=True, rounds=rounds))

    def answered(rounds):
        text = _said(spoken, code)
        if _holding(executor) and not any(c["name"] == "request_callback" for c in calls):
            text = without_call_promise(text)     # the confirmation is the text message, never a call
        _heard(executor, text, code or lang)
        return done(TurnResult(text, calls, ms(), language=code, rounds=rounds))

    def cut(rounds):
        text = still_checking(lang, spoken, max(0, int(getattr(executor, "turn", None) or 1) - 1))
        messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        if hasattr(executor, "not_heard"):
            executor.not_heard(text, lang)
        said_in = lang if copy.pick(copy.STILL_CHECKING, lang) or text != copy.text(copy.STILL_CHECKING, lang) \
            else None
        return done(TurnResult(text, calls, ms(), rounds=rounds, cut=True, language=said_in))

    timing = {"model": 0.0, "tool": 0.0, "read": 0, "write": 0}

    def done(result):
        result.model_ms, result.tool_ms = int(timing["model"] * 1000), int(timing["tool"] * 1000)
        result.cache_read, result.cache_write = timing["read"], timing["write"]
        return result

    rounds, spoken, code = 0, [], None
    for round_ in range(MAX_ROUNDS + 1):
        wrote = any(c["ok"] and c["name"] in WRITES for c in calls)
        if round_ == MAX_ROUNDS and not wrote:
            break
        left = deadline - time.monotonic()
        if round_ and left < MIN_ROUND_S:
            return cut(rounds)
        rounds = round_ + 1
        asked = time.monotonic()
        try:
            turn = _converse(model, system, messages, left)
        except _Late:
            timing["model"] += time.monotonic() - asked
            return cut(rounds)
        except agent_model.AgentModelError as exc:
            timing["model"] += time.monotonic() - asked
            if exc.code == "timeout":
                return cut(rounds)
            return failed(rounds)
        timing["model"] += time.monotonic() - asked
        usage = getattr(turn, "usage", None) or {}
        timing["read"] += int(usage.get("cacheReadInputTokens") or 0)
        timing["write"] += int(usage.get("cacheWriteInputTokens") or 0)
        content = ([{"type": "text", "text": turn.text}] if turn.text else []) + [
            {"type": "tool_use", "id": u.id, "name": u.name, "input": dict(u.input)} for u in turn.tool_uses]
        messages.append({"role": "assistant", "content": content or [{"type": "text", "text": "..."}]})
        if turn.text:
            # Text beside a tool use is spoken too (a goodbye with end_call, "one moment" before a lookup).
            tagged, text = language.tag(turn.text)
            code = tagged or code
            text = _whole_sentences(text) if turn.stop_reason == "max_tokens" else (text or "").strip()
            text = spoken_text(text, tagged or lang)
            preamble = bool(turn.tool_uses) and not all(u.name == "end_call" for u in turn.tool_uses)
            if preamble and readback.mentions_terms(text):
                preamble = False     # the card guarantee's fee rule written before a tool: the caller must hear it
            if text and text not in [t for _, t, _ in spoken]:
                spoken.append((tagged, text, preamble and not keep_preamble))
        if not turn.tool_uses:
            if not spoken:
                return failed(rounds)          # never silence: the error sentence offers a callback
            return answered(rounds)
        results, late = [], False
        tools_started = time.monotonic()
        _prefetch(executor, turn.tool_uses, deadline - time.monotonic())
        for use in turn.tool_uses:
            left = deadline - time.monotonic()
            if late or left <= 0:
                late, result = True, _LATE      # not run: the next turn runs it again
            else:
                try:
                    result = _tool(executor, use, left)
                    calls.append({"name": use.name, "ok": _ok(result)})
                except _Late:
                    late, result = True, _LATE
            results.append({"type": "tool_result", "tool_use_id": use.id,
                            "content": json.dumps(read_as(result), ensure_ascii=False, default=str)})
        messages.append({"role": "user", "content": results})
        timing["tool"] += time.monotonic() - tools_started
        if late:
            return cut(rounds)
        if spoken and all(u.name == "end_call" for u in turn.tool_uses):
            # The goodbye was said with end_call: no further round (it only repeats the goodbye).
            messages.append({"role": "assistant", "content": [{"type": "text", "text": "..."}]})
            return answered(rounds)
    return failed(rounds)


_SENTENCE_END = ".?!。？！؟።"


def _holding(executor):
    live = getattr(executor, "_live", None)
    return bool(live and live()[0] is not None)


# "We will call you on the number you are calling from" (retest 2026-11-20, German read-back): a call
# verb and a number word in one sentence. The confirmation is the SMS link.
_CALL_YOU = re.compile(r"\b(ring(er|a|e)?\s+(dig|er|deg|jer)|rufen\s+sie|call\s+you|bellen\s+(u|je)|"
                       r"vous\s+(rappel|appel)|le\s+llamar)", re.I)
_NUMBER_WORD = re.compile(r"\b(numret|nummer|nummeret|nummeren|number|numéro|numero|número)\b", re.I)


def without_call_promise(text):
    """The reply without a sentence promising to call the caller at a number."""
    parts = re.split(r"(?<=[.?!])\s+", str(text or ""))
    kept = [p for p in parts if not (_CALL_YOU.search(p) and _NUMBER_WORD.search(p))]
    return " ".join(kept).strip() if kept else str(text or "")


# A welcome after the greeting was already said (retest 2 on dev 2026-11-20: the first reply greeted again
# with another greeting): "Hej och välkommen till ...", "Välkommen!", "Hello and welcome to ...".
_WELCOME = re.compile(r"^\s*(?:(?:hej|hejsan|hallå|hello|hi)\b[\s,]*(?:och|and)?\s*)?(?:varmt\s+)?"
                      r"(?:välkommen|välkomna|welcome)\b", re.I)


def without_welcome(text, lang=None):
    """The reply without a sentence that welcomes the caller (a plain "Hej"
    stays); empty when the reply was nothing but a welcome."""
    parts = re.split(r"(?<=[.?!])\s+", str(text or "").strip())
    return " ".join(p for p in parts if not (_WELCOME.match(p) and not _carries_booking(p, lang))).strip()


def _carries_booking(sentence, lang):
    """A sentence that names a time, day, date or party, or the card terms: never dropped, since
    executor.heard already counted it as said (a read-back must be what the caller heard)."""
    return bool(readback.clock_times(sentence, lang) or readback.weekdays(sentence, lang)
                or readback._month_day(sentence, lang) or readback.party_sizes(sentence, lang)
                or readback.mentions_terms(sentence, fees=False))


def _said(spoken, code):
    """The spoken reply: every piece in the reply's language (a piece the
    model tagged with another language before a tool call is left out, so
    the caller never hears two languages in one reply), without the
    announcements written before a lookup when a later piece answers."""
    pieces = [(text, preamble) for tagged, text, preamble in spoken if tagged in (None, code)]
    answer = [text for text, preamble in pieces if not preamble]
    return " ".join(answer or [text for text, _ in pieces])


# A tag inside the reply ("...\n[de] Am Mittwoch..."): the model started a new text block.
_INNER_TAG = re.compile(r"\[([A-Za-z]{2,3})(?:[-_][A-Za-z]{2,4})?\]\s*")
# A note to itself in English ("Let me correct that: ..."): never spoken.
_META = re.compile(r"^\s*(let me (correct|fix|rephrase|try)|correction\b|note\s*:|wait\b|actually,|oops|i (made a mistake|"
                   r"need to correct|should (have )?(say|said|use|used))|sorry, (that|i (made|used|said)))", re.I)
# The same inside a sentence ("... Corrected: Haa, ..."): only what follows is meant.
_META_INLINE = re.compile(r"\b(corrected|correction|let me (correct|rephrase)( that)?)\s*:\s*", re.I)
_DASHES = re.compile(r"\s*[\u2013\u2014]\s*")
# Thinking aloud (retest 2026-11-20): "Halv åtta... nej, klockan sju", "tre personer till? Nej, två": the
# sentence up to the trailing dots or the question and its "no" is dropped; only the final answer is said.
_SELF_CORRECTION = re.compile(r"[^.?!…]*(?:\.\.\.|…|\?)\s*(?:nej|nei|nein|nee|no|nope)\s*[,.]\s*", re.I)


# A correction said aloud (retest 2 on dev 2026-11-20: "halv sex ... Rättelse: det är fredag den
# tjugosjunde november, klockan fem"): a marker at the start of a sentence or clause, with something said
# before it and a restatement after it. Only the restatement is said. "Nej, förlåt, vi har fullt" at the
# start of a reply corrects nothing and stays.
_CORRECTION = re.compile(
    r"(?:^|(?<=[.?!…,;:]))\s*(?:rättelse|korrigering|korrektur|correction|nej,?\s+förlåt|förlåt,?\s+jag\s+menar|"
    r"jag\s+menar|ich\s+meine|jeg\s+mener|sorry,?\s+i\s+mean|i\s+mean)\s*[:,.]\s*", re.I)


def _restated(text):
    """The text after the last correction marker that has words before and after it."""
    for m in reversed(list(_CORRECTION.finditer(text))):
        before, after = text[:m.start()], text[m.end():]
        if re.search(r"\w", before) and re.search(r"\w", after):
            return after[:1].upper() + after[1:]
    return text


def _final_answer(text):
    """The text without a spoken self-correction; what follows it starts a sentence."""
    m = _SELF_CORRECTION.search(text)
    while m:
        lead = re.match(r"\s*", m.group(0)).group(0)
        rest = text[m.end():]
        text = text[:m.start()] + lead + rest[:1].upper() + rest[1:]
        m = _SELF_CORRECTION.search(text)
    return text


def spoken_text(text, code=None):
    """What the voice may say of one model text: no inner language tags, no
    English note the model wrote to itself (a self-correction keeps only
    what follows it), no dashes or markdown (they are read aloud or break
    the sentence melody), no exclamation marks (an exclaimed voice sounds
    informal)."""
    text = str(text or "")
    paragraphs = [p for p in re.split(r"\n\s*", text) if p.strip()]
    if code != "en":
        meta = [i for i, p in enumerate(paragraphs) if _META.match(_INNER_TAG.sub("", p))]
        if meta:
            after = paragraphs[meta[-1] + 1:]
            paragraphs = after or [p for i, p in enumerate(paragraphs) if i not in meta]
    text = " ".join(paragraphs)
    if code != "en":
        inline = list(_META_INLINE.finditer(text))
        if inline and text[inline[-1].end():].strip():
            text = text[inline[-1].end():]
    text = _INNER_TAG.sub(lambda m: "" if language.normalise(m.group(1)) in language.NAMES else m.group(0), text)
    text = _final_answer(text)
    text = _restated(text)
    text = _DASHES.sub(", ", text)
    text = re.sub(r"[*#`]+", "", text)
    text = re.sub(r"[!¡]+", lambda m: "" if m.group(0) == "¡" else ".", text)   # calm, never exclaimed
    text = re.sub(r",\s*([.?!])", r"\1", text)
    return " ".join(text.split()).strip()


def _whole_sentences(text):
    """A reply cut by max_tokens, up to its last finished sentence."""
    text = (text or "").strip()
    cut = max(text.rfind(c) for c in _SENTENCE_END)
    return text[:cut + 1].strip() if cut > 0 else text


def replace_last_reply(messages, text):
    """The spoken reply differs from the model's (a language the voice cannot
    speak): the history keeps what the caller heard."""
    for m in reversed(messages):
        if m.get("role") == "assistant":
            m["content"] = text
            return


def dumps(messages) -> str:
    return json.dumps(messages, ensure_ascii=False, default=str)


def loads(value) -> list:
    try:
        out = json.loads(value or "[]")
    except ValueError:
        return []
    return out if isinstance(out, list) else []
