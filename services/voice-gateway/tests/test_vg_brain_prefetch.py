"""Speculative availability and the one-round booking turn (brain/prefetch.py): slots read with the phone agent's own
parsers, a cache in front of the tool Lambdas that only ever holds GET answers, the first model round answered with
the lookup the prompt asks for, and the guards unchanged (holds and writes stay the model's own calls)."""

import asyncio
import json
from concurrent.futures import Future
from datetime import date

import pytest

from shared.agent_model import ToolUse, Turn
from test_phone_agent_calls import live  # noqa: F401 (fixture)
from test_phone_agent_support import env  # noqa: F401 (fixture)
from test_vg_brain_agent import HOLD, READ_BACK, Call, Scripted, done, spoken

from voice_gateway.brain.prefetch import (OneRoundModel, PrefetchInvoker, Slots, booking_slots, plain_state_allows)
from voice_gateway.config import BrainConfig, GatewayConfig

assert live and env
MONDAY = date(2026, 11, 2)


# --- slots ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,lang,want", [
    ("Jag vill boka ett bord för fyra i morgon klockan sju", "sv", Slots("2026-11-03", "19:00", 4)),
    ("Hej, jag skulle vilja boka bord för fyra personer på fredag klockan sju.", "sv", Slots("2026-11-06", "19:00", 4)),
    ("boka bord på lördag för sex personer halv sju", "sv", Slots("2026-11-07", "18:30", 6)),
    ("Kan jag boka ett bord för 3 i morgon klockan 18:00?", "sv", Slots("2026-11-03", "18:00", 3)),
    ("vi är fem och vill boka bord i morgon klockan åtta", "sv", Slots("2026-11-03", "20:00", 5)),
    ("I would like to book a table for two tomorrow at eight", "en", Slots("2026-11-03", "20:00", 2)),
    ("Jag vill boka ett bord för fyra i morgon", "sv", None),                    # no time
    ("Jag vill boka ett bord i morgon klockan sju", "sv", None),                 # no party
    ("Jag vill flytta min bokning till i morgon klockan sju för fyra", "sv", None),   # a move, not a new booking
    ("Har ni öppet i morgon klockan sju för fyra?", "sv", None),                 # no booking words
    ("boka bord för fyra på måndag klockan sju", "sv", None),                    # today's weekday: the tool asks
    ("boka bord för fyra nästa fredag klockan sju", "sv", None),                 # which Friday: the tool decides
    ("boka bord för fyra eller fem i morgon klockan sju", "sv", None),           # two party sizes
    ("Ich möchte einen Tisch für zwei Personen morgen um acht Uhr reservieren", "de", Slots("2026-11-03", "20:00", 2)),
    ("Merhaba yarın saat sekizde iki kişilik bir masa", "tr", None),             # not parsed here: the model asks
])
def test_booking_slots(text, lang, want):
    assert booking_slots(text, lang, MONDAY) == want


def test_plain_state_allows_only_a_fresh_booking_request():
    assert plain_state_allows({}) and plain_state_allows({"proposals": {"p1": {"released": True}}})
    assert not plain_state_allows({"proposals": {"p1": {"date": "2026-11-03", "time": "19:00"}}})   # a live hold
    for key in ("found", "unsupported", "askedSwitch", "discloseNext", "resume", "guaranteePending"):
        assert not plain_state_allows({key: True})
    assert not plain_state_allows({"outcome": "booked"}) and not plain_state_allows({}, {"locationPending": True})


# --- the cache --------------------------------------------------------------------------------------

def _event(method, path, query=None):
    return {"routeKey": f"{method} x", "rawPath": path, "queryStringParameters": query,
            "requestContext": {"http": {"method": method, "path": path}}}


class Lambdas:
    def __init__(self):
        self.calls = []

    def __call__(self, fn, event):
        self.calls.append((fn, event["requestContext"]["http"]["method"], event.get("queryStringParameters")))
        return 200, {"n": len(self.calls)}


def test_the_cache_serves_a_prefetched_get_once_and_never_a_write():
    inner = Lambdas()
    cache = PrefetchInvoker(inner, now_ms=lambda: 1000)
    gen = cache.new_generation()
    get = _event("GET", "/locations/l/availability", {"date": "2026-11-03", "partySize": "4"})
    assert cache.prefetching(gen)("get-availability", get) == (200, {"n": 1})
    assert cache("get-availability", get) == (200, {"n": 1}) and cache.hits == [0]      # warm, no Lambda call
    assert cache("get-availability", get) == (200, {"n": 2})                           # served once only
    with pytest.raises(PermissionError):
        cache.prefetching(gen)("create-pending-reservation", _event("POST", "/locations/l/reservations/manual"))
    assert cache("create-pending-reservation", _event("POST", "/locations/l/reservations/manual"))[1] == {"n": 3}
    assert [c[1] for c in inner.calls] == ["GET", "GET", "POST"]


def test_a_newer_prefetch_drops_other_days_but_keeps_the_layout():
    inner = Lambdas()
    cache = PrefetchInvoker(inner)
    gen = cache.new_generation()
    day = _event("GET", "/locations/l/availability", {"date": "2026-11-03", "partySize": "4"})
    layout = _event("GET", "/locations/l/layout/active")
    cache.prefetching(gen)("get-availability", day)
    cache.prefetching(gen)("list-layout-version", layout)
    cache.new_generation()
    cache("get-availability", day)
    cache("list-layout-version", layout)
    assert len(inner.calls) == 3 and len(cache.hits) == 1                      # the day again, the layout from cache


def test_a_turn_waits_for_a_prefetch_still_in_flight_instead_of_asking_twice():
    inner = Lambdas()
    cache = PrefetchInvoker(inner)
    gen = cache.new_generation()
    get = _event("GET", "/locations/l/availability", {"date": "2026-11-03", "partySize": "4"})
    pending = Future()
    cache._entries[("get-availability", get["rawPath"], json.dumps(get["queryStringParameters"], sort_keys=True))] = \
        (pending, __import__("time").monotonic(), 0, gen)
    pending.set_result((200, {"prefetched": True}))
    assert cache("get-availability", get) == (200, {"prefetched": True}) and inner.calls == []


# --- the one-round model ----------------------------------------------------------------------------

class Counting:
    def __init__(self):
        self.calls = 0

    def converse(self, system, messages, tools, *, max_tokens):
        self.calls += 1
        return Turn(text="[sv] Klockan sju är ledig.")


def test_the_first_round_is_the_lookup_and_the_rest_is_the_model():
    inner = Counting()
    model = OneRoundModel(inner, Slots("2026-11-03", "19:00", 4))
    tools = [{"name": "get_availability"}, {"name": "hold_table"}]
    first = model.converse("s", [], tools, max_tokens=10, timeout_s=8)
    assert first.stop_reason == "tool_use" and first.tool_uses[0].name == "get_availability"
    assert first.tool_uses[0].input == {"date": "2026-11-03", "partySize": 4, "time": "19:00"} and inner.calls == 0
    assert model.converse("s", [], tools, max_tokens=10, timeout_s=7).text and inner.calls == 1
    assert model.real_rounds == 1 and model.answered_first
    plain = OneRoundModel(inner, None)
    plain.converse("s", [], tools, max_tokens=10)
    assert inner.calls == 2 and not plain.answered_first


# --- through the real agent (moto, the real tools) --------------------------------------------------

def test_a_booking_request_is_answered_in_one_model_round_and_the_hold_stays_the_models(live):
    live.availability.set("2026-11-03", ["19:00", "20:00"])
    offer = Turn(text="[sv] Klockan sju i morgon är ledig för fyra. Vilket namn ska bokningen stå på?")
    c = Call(live, Scripted([offer, Turn(tool_uses=(ToolUse("u1", "hold_table", HOLD),)), Turn(text=READ_BACK)]),
             cfg=GatewayConfig())
    assert c.brain.lookup_planned("Jag vill boka ett bord för fyra i morgon klockan sju")
    first = c.say("Jag vill boka ett bord för fyra i morgon klockan sju")
    assert done(first).tools == ("get_availability",) and c.brain.last_turn == {"bedrock_rounds": 1,
                                                                                 "lookup_first": True}
    assert "Klockan sju i morgon är ledig" in spoken(first)
    state = c.state()
    assert state["first"] == {"date": "2026-11-03", "time": "19:00"}            # the real executor ran it
    assert not [p for p in state.get("proposals", {}).values()]                 # nothing held by the gateway
    second = c.say("Anna")                                                      # the model holds, then reads back
    assert done(second).tools == ("hold_table",) and done(second).awaiting_confirmation
    assert not c.brain.lookup_planned("boka för fyra i morgon klockan sju")     # a live hold: the model decides
    assert not live.invoke.reservations
    c.close()


def test_one_round_off_leaves_the_turn_to_the_model(live):
    c = Call(live, Scripted([Turn(tool_uses=(ToolUse("u1", "hold_table", HOLD),)), Turn(text=READ_BACK)]),
             cfg=GatewayConfig(brain=BrainConfig(one_round=False)))
    assert not c.brain.lookup_planned("Jag vill boka ett bord för fyra i morgon klockan sju")
    items = c.say("Jag vill boka ett bord för fyra i morgon klockan sju")
    assert done(items).tools == ("hold_table",) and c.brain.last_turn["bedrock_rounds"] == 2
    c.close()


def test_a_prefetch_from_the_partial_warms_the_turns_lookup_and_saves_nothing(live):
    live.availability.set("2026-11-03", ["19:00", "20:00"])
    offer = Turn(text="[sv] Klockan sju i morgon är ledig. Vilket namn ska bokningen stå på?")
    c = Call(live, Scripted([offer]), cfg=GatewayConfig())
    before = json.loads(c.row().get("toolState") or "{}")

    async def prefetch():
        started = c.brain.prefetch("jag vill boka bord för fyra i morgon klockan sju")
        again = c.brain.prefetch("jag vill boka bord för fyra i morgon klockan sju")   # the same slots: once
        await asyncio.gather(*list(c.brain._prefetch_tasks))
        return started, again
    assert c.run(prefetch()) == (True, False)
    assert json.loads(c.row().get("toolState") or "{}") == before                     # a throwaway executor
    fetched = len(live.invoke.calls)
    assert c.brain.invoker().fetched >= 1
    c.say("Jag vill boka bord för fyra i morgon klockan sju.")
    assert len(c.brain.invoker().hits) >= 1
    turn_calls = [x for x in live.invoke.calls[fetched:] if x[0] == "get-availability"]
    assert not any((x[2].get("queryStringParameters") or {}).get("partySize") == "4" for x in turn_calls)
    c.close()


def test_the_booking_words_of_an_earlier_turn_count_for_the_lookup(live):
    """Live voice-A: "Fyra personer på fredag klockan 7." answered the agent's question after "boka bord för"."""
    live.availability.set("2026-11-06", ["19:00", "20:00"])
    ask = Turn(text="[sv] Absolut. För hur många personer och vilken dag?")
    offer = Turn(text="[sv] Klockan sju på fredag är ledig. Vilket namn ska bokningen stå på?")
    c = Call(live, Scripted([ask, offer]), cfg=GatewayConfig())
    assert not c.brain.lookup_planned("Fyra personer på fredag klockan 7.")
    c.say("Jag skulle vilja boka bord för")
    assert c.brain.lookup_planned("Fyra personer på fredag klockan 7.")
    items = c.say("Fyra personer på fredag klockan 7.")
    assert done(items).tools == ("get_availability",) and c.brain.last_turn["bedrock_rounds"] == 1
    c.close()
