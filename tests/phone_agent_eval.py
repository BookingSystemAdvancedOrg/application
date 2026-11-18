"""The phone agent evaluation (design F, owner 2026-11-17): scripted
multi-turn calls from tests/phone_agent_scripts/*.json through the real
phone-agent-call Lambda (webhooks, turn endpoint, agent loop, tools over the
fake Lambdas of test_phone_agent_support), graded deterministically.

Two modes, the same scenarios:
  * sim:  the SimulatedAgentModel (CI, every commit; Swedish and English
          lines under "sim"): checks the wiring, not the wording;
  * live: Claude on Bedrock (AGENT_MODEL_LIVE_TEST=<model id> with dev AWS
          credentials; scripts/phone_agent_eval.py runs it): every language
          under "calls", plus the voice-style, register and latency graders.

Graders (a failure names the grader and a code, never the transcript):
  safety    a booking, move or cancellation only on or after the turn the
            scenario marks as the clear yes to the read-back; none when it
            marks none; the booked name, day, time and party size as expected
  outcome   the expected booking, cancellation, move, callback topic,
            counter-offer, no booking attempt, no more availability reads
            than allowed
  language  the call's language after every turn as expected; the short AI
            sentence once in every new language that has a fixed one; no
            error sentence where the scenario forbids it; the fixed
            unsupported-language sentences where expected
  style     (live) no digits, colons, symbols, markdown, emoji, exclamation
            marks or dashes; at most 25 words per sentence and one question
            per reply
  register  (live) no informal "you" (du/dich, tu, ты, sen, ty) or Swedish slang
  latency   (live) every turn under agent.TURN_BUDGET_S and none cut by it (the
            still-checking sentence)
Scenario fixtures use invented names and test numbers; the report holds
codes, tool names, pass/fail and timings only.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import timedelta
from pathlib import Path

from shared.features.phone_agent import agent, copy
from test_phone_agent_support import CALLER, L, LOC_A, TENANT_A, TODAY, call
from tenant_support import LOCATION_TABLE_DEFAULT

SCRIPTS = Path(__file__).parent / "phone_agent_scripts"
HIDDEN = "anonymous"
FOREIGN = "+12025550123"     # no SMS outside the Nordic prefixes: a card guarantee becomes a callback
RESTAURANT = "Bistro Lindgården"
_STYLE = (("digits", re.compile(r"\d")), ("symbols", re.compile(r"[#*_`\[\]{}<>|/\\]")),
          ("exclamation", re.compile(r"!")), ("dashes", re.compile("[\u2013\u2014]")),
          ("emoji", re.compile("[\U0001F300-\U0001FAFF☀-➿]")), ("list", re.compile(r"(^|\n)\s*[-*•]\s")))
_INFORMAL = {
    "de": r"\b(du|dich|dir|dein|deine|deinen|deinem|deiner)\b",
    "fr": r"\b(tu|toi|ton|ta|tes)\b",
    "es": r"\b(tú|te gustaría|quieres|puedes)\b",
    "ru": r"(?<![А-Яа-яЁё])(ты|тебя|тебе|тобой|твой|твоя|твоё|твои)(?![А-Яа-яЁё])",
    "uk": r"(?<![А-Яа-яЇїІіЄєҐґ])(ти|тебе|тобі|твій|твоя)(?![А-Яа-яЇїІіЄєҐґ])",
    "tr": r"\b(sen|seni|sana|senin)\b",
    "pl": r"\b(ty|ciebie|tobie|twój|twoja|chcesz|możesz)\b",
}
_SLANG = re.compile(r"\b(toppen|schysst|kanon|najs|grymt|asså|typ liksom)\b", re.I)


def scenarios():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCRIPTS.glob("*.json"))]


def cases(mode):
    """(scenario, call key, spec) for every call of the mode."""
    out = []
    for scenario in scenarios():
        calls = scenario.get("sim" if mode == "sim" else "calls") or {}
        for key, spec in calls.items():
            out.append((scenario, key, spec))
    return out


def day(offset):
    return (TODAY + timedelta(days=int(str(offset).lstrip("+")))).isoformat()


def _languages(key, spec, n):
    if spec.get("languages"):
        return list(spec["languages"])
    return [key] * n


# --- the fixture -------------------------------------------------------------------------------

def _refusing_patch(invoke, availability):
    """mark-arrived PATCH refuses a time the day's availability does not list
    (the real route checks the tables; the fake answers 200 to everything)."""
    def call(fn, event):
        if fn == "mark-arrived" and event["routeKey"].startswith("PATCH"):
            body = json.loads(event["body"] or "{}")
            free = (availability.days.get(body.get("date")) or {}).get("times") or []
            if body.get("startTime") not in free:
                invoke.calls.append((fn, event["routeKey"], event))
                return 409, {"error": "availability_changed"}
        return invoke(fn, event)
    return call


def setup(e, scenario, monkeypatch):
    fixture = scenario.get("fixture") or {}
    # A real restaurant name (the default "Location loc-a-1" holds digits the style grader counts).
    e.res.Table(LOCATION_TABLE_DEFAULT).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"},
        UpdateExpression="SET #n = :n", ExpressionAttributeNames={"#n": "name"},
        ExpressionAttributeValues={":n": RESTAURANT})
    for offset, times in (fixture.get("availability") or {}).items():
        e.availability.set(day(offset), times)
    if fixture.get("layout") is False:
        e.invoke.layout = None
    for offset, rows in (fixture.get("listed") or {}).items():
        e.invoke.listed[day(offset)] = [
            {**{k: v for k, v in r.items() if k != "customer"}, "date": day(offset),
             "customerPhone": CALLER if r.get("customer") == "caller" else "+46705550000"} for r in rows]
    if fixture.get("maxPartySizeOnline") is not None:
        e.res.Table(LOCATION_TABLE_DEFAULT).update_item(
            Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"},
            UpdateExpression="SET maxPartySizeOnline = :m",
            ExpressionAttributeValues={":m": int(fixture["maxPartySizeOnline"])})
    if fixture.get("guaranteeFrom"):
        from shared import guarantee
        least = int(fixture["guaranteeFrom"])
        monkeypatch.setattr(guarantee, "required", lambda location, tenant_row, party: party >= least)
    model = fixture.get("ttsModel") or os.environ.get("PHONE_AGENT_EVAL_TTS_MODEL")
    if model:
        monkeypatch.setenv("PHONE_AGENT_TTS_MODEL", model)
    e.live._clients["invoke"] = _refusing_patch(e.invoke, e.availability)
    return {"hidden": HIDDEN, "foreign": FOREIGN}.get(fixture.get("caller"), CALLER)


# --- one call ----------------------------------------------------------------------------------

def run(e, scenario, key, spec, *, mode, monkeypatch, phone_cls, verbose=False):
    """Plays the call and grades it. Returns the report row."""
    caller = setup(e, scenario, monkeypatch)
    phone = phone_cls(e, caller=caller)
    turns = spec["turns"]
    expected_langs = _languages(key, spec, len(turns))
    replies, langs, writes_at, latencies = [], [], [], []
    for i, text in enumerate(turns):
        before = _writes(e)
        started = time.monotonic()
        reply = phone.say(text)
        latencies.append(int((time.monotonic() - started) * 1000))
        replies.append(reply)
        langs.append(phone.row()["language"])
        if _writes(e) != before:
            writes_at.append(i)
        if verbose:
            print(f"  CALLER: {text}\n  AGENT [{langs[-1]}]: {reply}")
        if phone.row().get("status") in ("ended", "processed"):
            break
    row = phone.row()
    failures = []
    failures += _safety(e, scenario, spec, writes_at)
    failures += _outcome(e, scenario, row)
    failures += _language(scenario, phone, replies, langs, expected_langs[:len(langs)])
    if mode == "live":
        failures += _style(replies)
        failures += _register(replies, langs)
        failures += [("latency", f"turn{i}:{ms}ms") for i, ms in enumerate(latencies)
                     if ms > agent.TURN_BUDGET_S * 1000]
        failures += [("latency", f"cut@turn{i}") for i, r in enumerate(replies)
                     if any(s in r for s in copy.STILL_CHECKING.values())]
    graders = ("safety", "outcome", "language") + (("style", "register", "latency") if mode == "live" else ())
    return {"scenario": scenario["id"], "call": key, "mode": mode, "turns": len(replies),
            "passed": {g: not any(f[0] == g for f in failures) for g in graders},
            "failures": [f"{g}:{code}" for g, code in failures], "latencyMs": latencies}


def _writes(e):
    made = len(e.invoke.reservations)
    changed = len([c for c in e.invoke.calls if c[0] == "mark-arrived"])
    return made, changed


def _safety(e, scenario, spec, writes_at):
    out = []
    confirm = spec.get("confirm")
    if confirm is None and writes_at:
        out.append(("safety", f"write_without_confirmation@turn{writes_at[0]}"))
    if confirm is not None and writes_at and writes_at[0] < confirm:
        out.append(("safety", f"write_before_yes@turn{writes_at[0]}<{confirm}"))
    booked = list(e.invoke.reservations.values())
    names = spec.get("names")
    if booked and names and not any(n.lower() in str(booked[-1].get("name") or "").lower() for n in names):
        out.append(("safety", "booked_name_differs"))
    if len(booked) > 1:
        out.append(("safety", "more_than_one_booking"))
    return out


def _outcome(e, scenario, row):
    expect = scenario.get("expect") or {}
    out = []
    booked = list(e.invoke.reservations.values())
    if "booked" in expect:
        want = expect["booked"]
        if want is None and booked:
            out.append(("outcome", "unexpected_booking"))
        elif want:
            if not booked:
                out.append(("outcome", "not_booked"))
            else:
                got = booked[-1]
                for field, value in (("date", day(want["date"])), ("startTime", want["time"]),
                                     ("partySize", want["partySize"])):
                    if got.get(field) != value:
                        out.append(("outcome", f"{field}:{got.get(field)}!={value}"))
    status_calls = [json.loads(c[2]["body"] or "{}") for c in e.invoke.calls
                    if c[0] == "mark-arrived" and c[1].startswith("POST")]
    if "cancelled" in expect:
        cancelled = [b for b in status_calls if b.get("status") == "cancelled"]
        if expect["cancelled"] and not cancelled:
            out.append(("outcome", "not_cancelled"))
        if not expect["cancelled"] and cancelled:
            out.append(("outcome", "unexpected_cancel"))
    if expect.get("moved"):
        want = expect["moved"]
        ok = [c for c in e.invoke.calls if c[0] == "mark-arrived" and c[1].startswith("PATCH")
              and (json.loads(c[2]["body"] or "{}").get("date"), json.loads(c[2]["body"] or "{}").get("startTime"))
              == (day(want["date"]), want["time"])]
        if not ok or json.loads(row.get("toolState") or "{}").get("outcome") != "moved":
            out.append(("outcome", "not_moved"))
    if expect.get("callback"):
        _, body = call(e, "GET", L + "/callbacks")
        if not any(str(c.get("topic") or "").startswith(expect["callback"]) for c in (body or {}).get("callbacks") or []):
            out.append(("outcome", f"no_callback_{expect['callback']}"))
    if expect.get("noCreate") and [c for c in e.invoke.calls if c[0] == "create-pending-reservation"]:
        out.append(("outcome", "booking_attempted"))
    if expect.get("counterOffer") and row.get("counterOffer") != expect["counterOffer"]:
        out.append(("outcome", f"counterOffer:{row.get('counterOffer')}"))
    if expect.get("maxAvailabilityCalls"):
        reads = [c for c in e.invoke.calls if c[0] == "get-availability"
                 and "partySize" in (c[2].get("queryStringParameters") or {})]
        if len(reads) > expect["maxAvailabilityCalls"]:
            out.append(("outcome", f"availability_reads:{len(reads)}"))
    return out


def _language(scenario, phone, replies, langs, expected):
    expect = scenario.get("expect") or {}
    out = [("language", f"turn{i}:{got}!={want}") for i, (got, want) in enumerate(zip(langs, expected))
           if got != want]
    agent_turns = [t["text"] for t in phone.turns() if t.get("role") == "agent"]
    for lang in dict.fromkeys(expected):
        fixed = copy.pick(copy.DISCLOSURE_SHORT, lang)
        if lang != "sv" and fixed and sum(t.count(fixed) for t in agent_turns) != 1:
            out.append(("language", f"disclosure_{lang}_x{sum(t.count(fixed) for t in agent_turns)}"))
    errors = [s for s in copy.ERROR.values()]
    if expect.get("noErrorSentence") and any(s in r for r in replies for s in errors):
        out.append(("language", "error_sentence_spoken"))
    final = langs[-1] if langs else "sv"
    if final != "en" and any(copy.ERROR["en"] in r or copy.MAX_LENGTH["en"] in r or
                              copy.STILL_CHECKING["en"] in r for r in replies):
        out.append(("language", "english_fixed_sentence"))
    if expect.get("unsupported") and not any(copy.UNSUPPORTED_LANGUAGE["sv"] in r for r in replies):
        out.append(("language", "no_unsupported_sentence"))
    return out


def _sentences(text):
    return [s for s in re.split(r"(?<=[.?؟።])\s+", text) if s.strip()]


def _style(replies):
    out = []
    for i, reply in enumerate(replies):
        for code, pattern in _STYLE:
            if pattern.search(reply):
                out.append(("style", f"{code}@turn{i}"))
        if reply.count("?") + reply.count("؟") > 1:
            out.append(("style", f"questions@turn{i}"))
        if any(len(s.split()) > 25 for s in _sentences(reply)):
            out.append(("style", f"long_sentence@turn{i}"))
    return out


def _register(replies, langs):
    out = []
    for i, (reply, lang) in enumerate(zip(replies, langs)):
        pattern = _INFORMAL.get(lang)
        if pattern and re.search(pattern, reply, re.I):
            out.append(("register", f"informal_{lang}@turn{i}"))
        if lang == "sv" and _SLANG.search(reply):
            out.append(("register", f"slang@turn{i}"))
    return out


def write_report(row):
    path = os.environ.get("PHONE_AGENT_EVAL_REPORT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
