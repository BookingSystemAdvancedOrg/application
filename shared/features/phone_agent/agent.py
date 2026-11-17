"""The agent loop (ADR-L decision 2): one caller utterance in, one spoken
reply out. Builds the system prompt, sends the conversation and the tool
schemas to the `AgentModel` port, runs the tool uses inside Booqy
(`tools.Executor`), feeds the results back and returns the final text.
The voice provider never sees a tool definition, a tool result or tenant
data: it gets only the returned text."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from shared import agent_model
from shared.features.phone_agent import copy, language
from shared.features.phone_agent.tools import TOOLS, read_as

MAX_ROUNDS = 4          # model passes per caller utterance (tool calls in between)
MAX_TOKENS = 400
TURN_BUDGET_S = 9.0     # no new model round after this; the caller hears the error sentence
KEEP_TOOL_ROUNDS = 2    # tool results older than this many rounds are shortened in the history
MAX_HISTORY_BYTES = 120_000
WRITES = ("create_reservation", "move_reservation", "cancel_reservation", "request_callback", "transfer_to_staff")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass
class TurnResult:
    reply: str
    tool_calls: list = field(default_factory=list)   # [{"name", "ok"}]
    latency_ms: int = 0
    error: bool = False
    language: str | None = None        # the model's `[<code>]` tag of the reply (any known language)
    rounds: int = 0


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


def system_prompt(lang, agent_name, restaurant, today: date, tone="formal", *, last_day=None, disclose_now=False,
                  phone_known=True, unsupported=None) -> str:
    """The instruction prompt for one turn in `lang` (copy.SYSTEM_PROMPT)."""
    name = language.name(lang) if lang in language.NAMES else "Swedish"
    last_day = last_day or today + timedelta(days=21)
    register = copy.REGISTER.get(tone or "formal", copy.REGISTER["formal"]).format(language_name=name)
    return copy.SYSTEM_PROMPT.format(
        agent=" ".join(str(agent_name or copy.DEFAULT_AGENT_NAME).split()),
        restaurant=" ".join(str(restaurant or "restaurangen").split()),
        language_name=name, register=register,
        disclose_now=copy.DISCLOSE_NOW.format(language_name=name) if disclose_now else "",
        low_asr=copy.LOW_ASR.format(language_name=name) if lang in language.LOW_ASR else "",
        unsupported=copy.UNSUPPORTED.format(unsupported=unsupported, unsupported_name=language.name(unsupported))
        if unsupported else "",
        phone_known=copy.PHONE_KNOWN if phone_known else copy.PHONE_HIDDEN,
        horizon_last=f"{last_day.isoformat()} ({_WEEKDAYS[last_day.weekday()]})",
        calendar=calendar(today, last_day))


def _ok(result) -> bool:
    return isinstance(result, dict) and result.get("ok") is not False


def error_sentence(lang):
    return copy.text(copy.ERROR, lang or "sv")


_KEEP_KEYS = ("ok", "code", "holdId", "reservationId", "date", "weekday", "time", "partySize", "reservations")
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
    place) and returns the reply. A model error, too many rounds or a turn
    over its time budget ends the turn with the fixed error sentence in the
    call's language (the caller is offered a callback). A booking, move or
    cancellation that already succeeded in this turn always gets its last
    round, so the caller hears what was done.

    Text the model writes before a lookup or a write ("one moment, I am
    checking") is left out when a later round answers: the caller hears the
    whole reply only once the turn ends, so it would only repeat. The
    goodbye written with end_call is kept; `keep_preamble` keeps it all
    (the model was asked to say the AI sentence itself in this reply)."""
    started = time.monotonic()
    compact(messages)
    messages.append({"role": "user", "content": caller_text})
    calls = []

    def failed(rounds):
        text = error_sentence(lang)
        messages.append({"role": "assistant", "content": text})
        return TurnResult(text, calls, int((time.monotonic() - started) * 1000), error=True, rounds=rounds)

    rounds, spoken, code = 0, [], None
    for round_ in range(MAX_ROUNDS + 1):
        wrote = any(c["ok"] and c["name"] in WRITES for c in calls)
        if round_ == MAX_ROUNDS and not wrote:
            break
        if round_ and time.monotonic() - started > budget_s and not wrote:
            break
        rounds = round_ + 1
        try:
            turn = model.converse(system, messages, TOOLS, max_tokens=MAX_TOKENS)
        except agent_model.AgentModelError:
            return failed(rounds)
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
            if text and text not in [t for _, t, _ in spoken]:
                spoken.append((tagged, text, preamble and not keep_preamble))
        if not turn.tool_uses:
            if not spoken:
                return failed(rounds)          # never silence: the error sentence offers a callback
            return TurnResult(_said(spoken, code), calls, int((time.monotonic() - started) * 1000),
                              language=code, rounds=rounds)
        results = []
        for use in turn.tool_uses:
            result = executor.run(use.name, use.input)
            calls.append({"name": use.name, "ok": _ok(result)})
            results.append({"type": "tool_result", "tool_use_id": use.id,
                            "content": json.dumps(read_as(result), ensure_ascii=False, default=str)})
        messages.append({"role": "user", "content": results})
        if spoken and all(u.name == "end_call" for u in turn.tool_uses):
            # The goodbye was said with end_call: no further round (it only repeats the goodbye).
            messages.append({"role": "assistant", "content": [{"type": "text", "text": "..."}]})
            return TurnResult(_said(spoken, code), calls, int((time.monotonic() - started) * 1000),
                              language=code, rounds=rounds)
    return failed(rounds)


_SENTENCE_END = ".?!。？！؟።"


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
