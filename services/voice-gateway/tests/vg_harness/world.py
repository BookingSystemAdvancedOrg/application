"""The fake restaurant the replay runs against. Owner: builder 'harness'.

The same world the application's phone agent tests use (tests/test_phone_agent_support.py): moto DynamoDB tables as
Terraform creates them, two tenants, the fake Lambda invoker that answers the existing functions' routes (availability,
layout, the manual reservation route, ...), the simulated voice provider that hands out the line's number. Nothing in
it is real, so a scenario never writes dev data and there is nothing to clean up. A scenario's `tenant` dict tunes it:

  guaranteeFromPartySize  the location asks a card guarantee from this party size (fees 150 / 75 kr per person,
                          cut-off 24 h), so the fee rule has to be spoken
  fullSlots               ["fri 19:00", ...] times that are taken (the rest of the evening stays free)

`HarnessModel` is the agent model of fake mode: the application's scripted simulator (shared.agent_model), plus the
five things the simulator does not do and a real model does (the fifth: the prompt's step 2, the day's availability
is looked up before a hold, and a free requested time is then held at once): it reads a piece's `gloss` for languages it does not
know, understands "klockan sju" and "halv sju", tags every reply `[xx]`, and says the card guarantee's terms and the
question before the read-back. Everything else (tools, read-back and clear-yes guard, availability) is the real code.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from types import SimpleNamespace

WEEKDAYS = {"mon": "2026-11-02", "tue": "2026-11-03", "wed": "2026-11-04", "thu": "2026-11-05", "fri": "2026-11-06",
            "sat": "2026-11-07", "sun": "2026-11-08"}
EVENING = ["17:00", "17:30", "18:00", "18:30", "19:00", "19:30", "20:00", "20:30", "21:00"]
GUARANTEE_URL = "https://checkout.stripe.com/c/pay/cs_test_harness"
DIALED = "+46100000001"          # the number the simulated voice provider assigns on enable


def parse_slot(slot: str) -> tuple[str, str]:
    """"fri 19:00" -> ("2026-11-06", "19:00")."""
    day, time = slot.split()
    return WEEKDAYS[day[:3].lower()], time


class TimedInvoker:
    """FakeInvoker plus a log of every call (at_ms on the call clock, status, party, date, time) and the start times
    the tools were ever shown (what the agent may honestly offer)."""

    def __init__(self, inner, now_ms=lambda: 0):
        self.inner, self.now_ms = inner, now_ms
        self.log: list[dict] = []
        self.offered_times: set[str] = set()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __call__(self, fn, event):
        status, body = self.inner(fn, event)
        query = event.get("queryStringParameters") or {}
        try:
            request = json.loads(event.get("body") or "null") or {}
        except ValueError:
            request = {}
        if fn == "create-pending-reservation" and status == 201 and request.get("guarantee") == "link":
            # the manual route's answer for a card guarantee booking: pending, with the Stripe link the agent texts
            body = {**body, "status": "pending", "date": request["date"], "startTime": request["startTime"],
                    "partySize": request["partySize"], "language": request.get("language"),
                    "guarantee": {"noShowFee": 40000, "lateCancelFee": 0, "cancelCutoffHours": 0},
                    "guaranteeLink": {"url": GUARANTEE_URL, "minutes": 30, "expiresAt": "2026-11-03T18:32:00Z"}}
        party = query.get("partySize") or request.get("partySize")
        entry = {"fn": fn, "route": event.get("routeKey"), "at_ms": self.now_ms(), "status": status,
                 "party": int(party) if str(party or "").isdigit() else None,
                 "date": query.get("date") or request.get("date"), "time": query.get("time") or request.get("startTime")}
        if isinstance(body, dict):
            for slot in body.get("slots") or ():
                if isinstance(slot, dict) and slot.get("startTime") and (slot.get("options") or slot.get("tables")):
                    self.offered_times.add(slot["startTime"])
            if fn == "create-pending-reservation" and status == 201:
                entry["reservation"] = body.get("reservationId")
        self.log.append(entry)
        return status, body


# --- the model ----------------------------------------------------------------------------------------------

_NUM_WORDS = {"ett": 1, "en": 1, "två": 2, "tre": 3, "fyra": 4, "fem": 5, "sex": 6, "sju": 7, "åtta": 8, "nio": 9,
              "tio": 10, "elva": 11, "tolv": 12, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_ALT = "|".join(sorted(_NUM_WORDS, key=len, reverse=True))
_CLOCK_WORD = re.compile(rf"\b(klockan|kl\.?|at) ({_ALT})\b(?! ?(personer|people|guests|pers))", re.IGNORECASE)
_HALF_SV = re.compile(rf"\b(?:klockan |kl\.? )?halv ({_ALT})\b", re.IGNORECASE)
_HALF_EN = re.compile(rf"\bhalf past ({_ALT})\b", re.IGNORECASE)


def pivot(text: str) -> str:
    """What the simulator can parse: clock words as digits ("klockan sju" -> "klockan 7", "halv sju" -> "18:30",
    "half past seven" -> "19:30" after the evening rule the simulator applies to a bare hour)."""
    def half_sv(m):
        hour = _NUM_WORDS[m.group(1).lower()] - 1 or 12
        return f"klockan {hour % 12 + 12}:30"

    def half_en(m):
        return f"at {_NUM_WORDS[m.group(1).lower()] % 12 + 12}:30"

    text = _HALF_SV.sub(half_sv, text)
    text = _HALF_EN.sub(half_en, text)
    return _CLOCK_WORD.sub(lambda m: f"{m.group(1)} {_NUM_WORDS[m.group(2).lower()]}", text)


def _blocks(message):
    content = message.get("content")
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])


class HarnessModel:
    """Built lazily on the application's SimulatedAgentModel (the import needs the application on sys.path)."""

    def __new__(cls, *, glosses: dict[str, str], languages: dict[str, str], today):
        from shared.agent_model import SimulatedAgentModel, Turn

        class _Model(SimulatedAgentModel):
            def converse(self, system, messages, tools, *, max_tokens, timeout_s=None):
                self._seen = messages
                turn = super().converse(system, self._read(messages), tools, max_tokens=max_tokens, timeout_s=timeout_s)
                if turn.text:
                    turn = Turn(f"[{self._language(messages)}] {turn.text}", turn.tool_uses, turn.stop_reason)
                return turn

            def _read(self, messages):
                out = []
                for m in messages:
                    if m.get("role") != "user":
                        out.append(m)
                        continue
                    blocks = []
                    for b in _blocks(m):
                        if b.get("type") == "text":
                            text = glosses.get(b.get("text", ""), b.get("text", ""))
                            b = {**b, "text": pivot(text)}
                        blocks.append(b)
                    out.append({**m, "content": blocks})
                return out

            def _language(self, messages):
                """The language the reply is tagged with: that of the latest caller text of three words or more
                (a bare "Okay." or "Yes." does not change the language, as a real model keeps the call's)."""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        for b in _blocks(m):
                            text = b.get("text") if b.get("type") == "text" else None
                            if text in languages and len(text.split()) >= 3:
                                return languages[text]
                return "sv"

            def _booking_step(self, slots, offered, lang, *, hold):
                """The prompt's step 2: the availability of the day first, also when the caller named the time (the
                simulator holds at once); a free asked time is then held (_after_tool)."""
                if hold and "time" in slots and "partySize" in slots and "date" in slots \
                        and not self._looked_up(slots["date"], slots["partySize"]):
                    return self._use("get_availability", {k: slots[k] for k in ("date", "partySize", "time")},
                                     offered, lang)
                return super()._booking_step(slots, offered, lang, hold=hold)

            def _looked_up(self, day, party):
                """get_availability for this day and party since the caller's last words."""
                for m in reversed(getattr(self, "_seen", None) or []):
                    blocks = _blocks(m)
                    if m.get("role") == "user" and any(b.get("type") == "text" for b in blocks):
                        return False
                    for b in blocks:
                        args = b.get("input") or {}
                        if b.get("type") == "tool_use" and b.get("name") == "get_availability" \
                                and args.get("date") == day and str(args.get("partySize")) == str(party):
                            return True
                return False

            def _after_tool(self, messages, block, offered, lang):
                from shared.agent_model import ToolUse, _result
                result = _result(block)
                last = self._context(messages[:-1])[3] or {}
                if last.get("name") == "get_availability" and result.get("requestedTimeFree") is True \
                        and result.get("date") and result.get("partySize") and not result.get("code"):
                    # The gateway looked availability up first (brain/prefetch.py, one round): the caller's own time
                    # is free, so the model holds it, as the simulator does when it skips the lookup.
                    return Turn(tool_uses=(ToolUse("h-" + str(block.get("tool_use_id")), "hold_table", {
                        "date": result["date"], "time": result["requestedTime"],
                        "partySize": result["partySize"]}),), stop_reason="tool_use")
                terms = result.get("guaranteeTerms") if isinstance(result, dict) else None
                last_use = self._context(messages[:-1])[3]
                if terms and result.get("holdId") and (last_use or {}).get("name") == "hold_table" and terms.get("question"):
                    return Turn(text=f"{terms['sentence']} {terms['question']}")
                return super()._after_tool(messages, block, offered, lang)

            def _after_caller(self, messages, offered, lang):
                from shared.agent_model import _text, match_intent
                text = _text(messages[-1])
                asked_info = len(messages) >= 2 and (_text(messages[-2]).endswith("Vill du ha mer information om det?")
                                                     or _text(messages[-2]).endswith("Would you like more information about that?"))
                if asked_info and match_intent(text)[0] == "no":
                    _, slots, _, last_use, last_result = self._context(messages)
                    if (last_use or {}).get("name") == "hold_table" and (last_result or {}).get("holdId"):
                        return self._say("hold_table", lang, date=last_result.get("date", slots.get("date", "")),
                                         time=last_result.get("time", slots.get("time", "")),
                                         partySize=last_result.get("partySize", slots.get("partySize", "")))
                return super()._after_caller(messages, offered, lang)

        return _Model(today=today)


# --- the world ----------------------------------------------------------------------------------------------

@contextmanager
def fake_world(tenant: dict | None = None, *, now_ms=lambda: 0, glosses=None, languages=None, real_credentials=None):
    """The moto world of the phone agent tests as a context manager (a pytest fixture cannot be used from the CLI).
    Yields a namespace: api, live (the phone-agent-call module, GatewayVoice installed), invoke (TimedInvoker), lam
    (the notification Lambda: SMS notices), model, res (DynamoDB resource), number, callbacks()."""
    import boto3
    from _pytest.monkeypatch import MonkeyPatch
    from moto import mock_aws

    import test_phone_agent_support as sup
    from shared import dynamo as shared_dynamo
    from shared import events
    from shared import tenant as shared_tenant
    from shared.features import phone_agent as pa
    from shared.features.phone_agent import session
    from tenant_support import (LOC_A, REGION, TENANT_A, TENANT_B, USER_TABLE_DEFAULT, features_on, location_row,
                                seed_tenancy, tenant_row)
    from voice_gateway.brain.voice_port import GatewayVoice

    tenant = tenant or {}
    env_vars = {"ENVIRONMENT": "dev", "AWS_DEFAULT_REGION": REGION, "PHONE_AGENT_TABLE_NAME": sup.TABLE,
                "USER_TABLE_NAME": USER_TABLE_DEFAULT, "RECORDINGS_BUCKET": sup.BUCKET,
                "NOTIFICATION_FUNCTION_NAME": "dev-notification:live", "NUMBER_INDEX_NAME": "byNumber",
                "RESERVATION_INDEX_NAME": "byReservation"}
    creds = real_credentials or {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                                 "AWS_SESSION_TOKEN": "testing"}
    with MonkeyPatch.context() as mp, mock_aws():
        for key, value in {**env_vars, **creds}.items():
            mp.setenv(key, value)
        shared_dynamo._resource = None
        shared_dynamo._client = None
        on = features_on("phoneAgent")
        policy = None
        if tenant.get("guaranteeFromPartySize"):
            policy = {"enabled": True, "minPartySize": int(tenant["guaranteeFromPartySize"]),
                      "noShowFeePerPerson": 150, "lateCancelFeePerPerson": 75, "cancelCutoffHours": 24}
        loc_a = location_row(TENANT_A, LOC_A, **({"guarantee": policy, "stripeAccountId": "acct_harness"} if policy else {}))
        seed_tenancy(mp, tenant_a=tenant_row(TENANT_A, features=on, limits={pa.LIMIT_KEY: 300}),
                     tenant_b=tenant_row(TENANT_B, features=on, limits={pa.LIMIT_KEY: 300}),
                     locations_a=[loc_a, location_row(TENANT_A, sup.LOC_A2)])
        sup.phone_table(boto3.client("dynamodb", region_name=REGION))
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=sup.BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        clock = sup.Clock(sup.NOW)
        mp.setattr(pa, "now", clock)
        mp.setattr(session, "local_today", lambda location: sup.TODAY)
        published = []
        checked = events.publish
        mp.setenv("EVENT_BUS_NAME", "test-bus")
        mp.setattr(events, "client", lambda: sup.FakeBus())
        mp.setattr(events, "publish", lambda f, t, d: (checked(f, t, d), published.append((t, d)))[0])
        api, live = sup.load("phone-agent"), sup.load("phone-agent-call")
        availability = sup.Availability()
        for day in (f"2026-11-0{d}" for d in range(2, 10)):
            availability.set(day, EVENING, staff={t: {"t6": 6, "t4p": 4} for t in EVENING})
        for slot in tenant.get("fullSlots") or ():
            day, time = parse_slot(slot)
            times = [t for t in EVENING if t != time]
            availability.set(day, times, staff={t: {"t6": 6, "t4p": 4} for t in times}, unavailable={time: {"t4": "booked"}})
        invoke = TimedInvoker(sup.FakeInvoker(availability), now_ms)
        voice, tel = sup.SimulatedVoiceProvider(), sup.SimulatedTelephony()
        model = HarnessModel(glosses=glosses or {}, languages=languages or {}, today=sup.TODAY)
        lam = sup.FakeLambda()
        for module in (api, live):
            module._clients.update({"voice": voice, "telephony": tel, "model": model, "invoke": invoke, "lambda": lam})
        mp.setattr(live, "_endpoint_key", lambda: "harness-key")
        status, body = sup.call(SimpleNamespace(api=api), "PUT", sup.L, body=dict(sup.ALWAYS))
        assert status == 200, body
        live._clients["voice"] = GatewayVoice()       # the line is the gateway now: it speaks and transfers itself
        res = boto3.resource("dynamodb", region_name=REGION)

        def callbacks():
            items = res.Table(sup.TABLE).scan().get("Items") or []
            return [dict(i) for i in items if str(i.get("SK", "")).startswith("CALLBACK#")]

        yield SimpleNamespace(api=api, live=live, invoke=invoke, lam=lam, model=model, res=res, number=DIALED,
                              published=published, callbacks=callbacks, availability=availability, clock=clock,
                              caller=sup.CALLER)
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()
