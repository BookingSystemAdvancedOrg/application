"""Phone agent client ports and simulators (CONTRACTS.md 13 addendum rows,
ADR-L): voice provider, telephony and agent model."""

import json
import time
from datetime import date
from pathlib import Path

import pytest

from shared import agent_model, telephony, voice_provider
from shared.agent_model import SimulatedAgentModel, ToolUse, Turn
from shared.telephony import SimulatedTelephony
from shared.voice_provider import SimulatedVoiceProvider, SipTarget, TransferResult

CLIENTS = [  # (contract name, env var, module, simulator class, real class, error class, a port call)
    ("voice", "VOICE_CLIENT", voice_provider, SimulatedVoiceProvider, "RealVoiceProvider",
     voice_provider.VoiceProviderError, lambda c: c.end("call-1", "done")),
    ("telephony", "TELEPHONY_CLIENT", telephony, SimulatedTelephony, "RealTelephony", telephony.TelephonyError,
     lambda c: c.verify_forwarding("+46701234567")),
    ("agent_model", "AGENT_MODEL_CLIENT", agent_model, SimulatedAgentModel, "RealAgentModel",
     agent_model.AgentModelError, lambda c: c.converse("s", [{"role": "user", "content": "hej"}], [], max_tokens=5)),
]
METHOD = {"voice": "end", "telephony": "verify_forwarding", "agent_model": "converse"}


@pytest.mark.parametrize("name, env, module, sim, real_cls, err, call", CLIENTS)
def test_factory_picks_simulator_by_default_and_when_asked(monkeypatch, name, env, module, sim, real_cls, err, call):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.delenv(env, raising=False)
    assert isinstance(module.client(), sim)
    monkeypatch.setenv(env, "simulator")
    assert isinstance(module.client(), sim)


@pytest.mark.parametrize("name, env, module, sim, real_cls, err, call", CLIENTS)
def test_simulator_refused_in_prod(monkeypatch, name, env, module, sim, real_cls, err, call):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv(env, "simulator")
    with pytest.raises(RuntimeError, match=f"{name} simulator is not allowed in prod"):
        module.client()


@pytest.mark.parametrize("name, env, module, sim, real_cls, err, call", CLIENTS)
def test_unknown_value_refused(monkeypatch, name, env, module, sim, real_cls, err, call):
    monkeypatch.setenv(env, "mock")
    with pytest.raises(ValueError):
        module.client()


@pytest.mark.parametrize("name, env, module, sim, real_cls, err, call", CLIENTS)
def test_real_is_a_skeleton(monkeypatch, name, env, module, sim, real_cls, err, call):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv(env, "real")
    client = module.client()
    assert type(client).__name__ == real_cls
    with pytest.raises(NotImplementedError):
        call(client)


@pytest.mark.parametrize("name, env, module, sim, real_cls, err, call", CLIENTS)
def test_fail_injection(name, env, module, sim, real_cls, err, call):
    client = sim(fail={METHOD[name]: 2})
    for _ in range(2):
        with pytest.raises(err) as exc:
            call(client)
        assert exc.value.retryable and exc.value.code == "unavailable"
    call(client)


# --- voice provider ---------------------------------------------------------------------

def test_voice_agents_are_idempotent_by_name():
    voice = SimulatedVoiceProvider()
    first = voice.create_agent({"name": "dev-phone-agent-loc-a-1", "voice": "sv"})
    assert voice.create_agent({"name": "dev-phone-agent-loc-a-1", "voice": "other"}) == first
    assert first.startswith("sim-agent-") and len(first) == len("sim-agent-") + 10
    assert voice.create_agent({"name": "dev-phone-agent-loc-a-2"}) != first
    assert voice.sip_target(first) == SipTarget(f"sip:{first}@sim.invalid")
    voice.update_agent(first, {"name": "dev-phone-agent-loc-a-1", "voice": "en"})
    assert voice.agents[first]["voice"] == "en"
    voice.delete_agent(first)
    voice.delete_agent(first)                         # purge retried: no error
    for method in (voice.sip_target, lambda a: voice.update_agent(a, {})):
        with pytest.raises(voice_provider.VoiceProviderError, match="not_found"):
            method(first)
    with pytest.raises(voice_provider.VoiceProviderError, match="invalid_config"):
        voice.create_agent({})


def test_voice_say_transfer_end_are_recorded():
    voice = SimulatedVoiceProvider(transfer_answered=False)
    voice.say("call-1", "Hej, du pratar med en AI-assistent.")
    assert voice.transfer("call-1", "+46701234567", 20) == TransferResult(answered=False)
    voice.transfer_answered = True
    assert voice.transfer("call-1", "+46701234567", 20).answered is True
    voice.end("call-1", "done")
    assert ("say", "call-1", "Hej, du pratar med en AI-assistent.") in voice.calls
    assert ("transfer", "call-1", "+46701234567", 20) in voice.calls and ("end", "call-1", "done") in voice.calls


def test_voice_recording_only_with_opt_in():
    assert SimulatedVoiceProvider().recording_url("call-1") is None
    url = SimulatedVoiceProvider(recording=True).recording_url("call-1")
    assert url.startswith("file://")
    path = Path(url.removeprefix("file://"))
    assert path.exists() and path.read_bytes()[:4] == b"RIFF"


def test_voice_webhook_accepts_only_signed_simulator_json():
    voice = SimulatedVoiceProvider()
    body = json.dumps({"events": [
        {"kind": "call_started", "call_ref": "c1", "provider_agent_id": "sim-agent-x", "from": "+46701", "to": "+4610"},
        {"kind": "transcript", "call_ref": "c1", "role": "agent", "text": "Hej", "final": True},
        {"kind": "call_ended", "call_ref": "c1", "usage": {"seconds": 61}}]})
    events = voice.parse_webhook({"X-Sim-Signature": "ok"}, body)
    assert [e.kind for e in events] == ["call_started", "transcript", "call_ended"]
    assert events[0].call_ref == "c1" and events[0].data["provider_agent_id"] == "sim-agent-x"
    assert events[2].data == {"usage": {"seconds": 61}}
    assert voice.parse_webhook({"x-sim-signature": "ok"}, body.encode())[1].data["text"] == "Hej"
    for headers, raw in [({}, body), ({"X-Sim-Signature": "bad"}, body), ({"X-Sim-Signature": "ok"}, "not json"),
                         ({"X-Sim-Signature": "ok"}, json.dumps({"events": [{"kind": "hacked"}]})),
                         ({"X-Sim-Signature": "ok"}, json.dumps({"other": []}))]:
        with pytest.raises(voice_provider.VoiceProviderError, match="unauthorized"):
            voice.parse_webhook(headers, raw)


# --- telephony --------------------------------------------------------------------------

def test_numbers_are_handed_out_in_order_until_none_left():
    tel = SimulatedTelephony(numbers_left=2)
    first = tel.provision_number("SE", "loc-a-1")
    second = tel.provision_number("SE", "loc-a-2")
    assert (first.e164, second.e164) == ("+46100000001", "+46100000002")
    assert first.provider_id != second.provider_id
    with pytest.raises(telephony.TelephonyError) as exc:
        tel.provision_number("SE", "loc-a-3")
    assert exc.value.code == "number_unavailable" and not exc.value.retryable
    assert SimulatedTelephony(numbers_left=0).numbers_left == 0
    with pytest.raises(telephony.TelephonyError, match="number_unavailable"):
        SimulatedTelephony(numbers_left=0).provision_number("SE", "x")


def test_inbound_target_release_and_transfer():
    tel = SimulatedTelephony(transfer_answered=False)
    number = tel.provision_number("SE", "loc-a-1")
    sip = SipTarget("sip:sim-agent-x@sim.invalid")
    tel.set_inbound(number.provider_id, sip, "+46701234567")
    assert tel.inbound[number.provider_id] == {"sip": sip, "fallback": "+46701234567"}
    assert tel.transfer("c1", "+46701234567", 20).answered is False
    tel.release_number(number.provider_id)
    tel.release_number(number.provider_id)           # second release: no error
    assert number.provider_id not in tel.numbers and number.provider_id not in tel.inbound
    with pytest.raises(telephony.TelephonyError, match="not_found"):
        tel.set_inbound(number.provider_id, sip, "+46701234567")
    assert tel.verify_forwarding("+46701234567").status == "unsupported"


def test_telephony_webhook_needs_basic_sim_auth():
    tel = SimulatedTelephony()
    body = json.dumps({"events": [{"kind": "inbound", "call_ref": "c1", "from": "+46701234567", "to": "+46100000001",
                                   "at": "2026-11-02T18:00:00Z"},
                                  {"kind": "ended", "call_ref": "c1", "duration_s": 90, "cause": "hangup"}]})
    events = tel.parse_webhook({"Authorization": "Basic c2ltOnNpbQ=="}, body)
    assert [(e.kind, e.call_ref) for e in events] == [("inbound", "c1"), ("ended", "c1")]
    assert events[1].data == {"duration_s": 90, "cause": "hangup"}
    for headers, raw in [({}, body), ({"Authorization": "Basic c2ltOndyb25n"}, body),
                         ({"Authorization": "Basic c2ltOnNpbQ=="}, "{"),
                         ({"Authorization": "Basic c2ltOnNpbQ=="}, json.dumps({"events": [{"kind": "x"}]}))]:
        with pytest.raises(telephony.TelephonyError, match="unauthorized"):
            tel.parse_webhook(headers, raw)


# --- agent model ------------------------------------------------------------------------

TODAY = date(2026, 11, 2)             # a Monday
TOOLS = [{"name": n} for n in ("get_restaurant_info", "get_availability", "hold_table", "create_reservation",
                               "send_guarantee_link", "find_reservations", "move_reservation",
                               "cancel_reservation", "get_menu", "transfer_to_staff", "request_callback",
                               "end_call")]


class Call:
    """A scripted conversation against the simulator: caller says, tools answer."""

    def __init__(self, **kwargs):
        self.model = SimulatedAgentModel(today=TODAY, **kwargs)
        self.messages = []

    def _turn(self):
        turn = self.model.converse("system", self.messages, TOOLS, max_tokens=300)
        content = ([{"type": "text", "text": turn.text}] if turn.text else []) + [
            {"type": "tool_use", "id": u.id, "name": u.name, "input": u.input} for u in turn.tool_uses]
        self.messages.append({"role": "assistant", "content": content})
        return turn

    def say(self, text):
        self.messages.append({"role": "user", "content": text})
        return self._turn()

    def tool(self, result):
        use = next(b for b in reversed(self.messages[-1]["content"]) if b["type"] == "tool_use")
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": use["id"], "content": json.dumps(result)}]})
        return self._turn()


def _use(turn):
    (use,) = turn.tool_uses
    assert turn.stop_reason == "tool_use" and turn.text is None
    return use.name, use.input


@pytest.mark.parametrize("opening, name_line, lang, booked", [
    ("Hej, jag skulle vilja boka ett bord för fyra personer imorgon klockan 19", "Jag heter Anna", "sv", "bokad"),
    ("Hi, I'd like to book a table for four people tomorrow at 7 pm", "My name is Anna", "en", "booked"),
])
def test_scenario_book_inside_rules(opening, name_line, lang, booked):
    call = Call()
    assert _use(call.say(opening)) == ("hold_table", {"date": "2026-11-03", "time": "19:00", "partySize": 4})
    proposal = call.tool({"holdId": "h1", "date": "2026-11-03", "time": "19:00", "partySize": 4})
    assert "19:00" in proposal.text and not proposal.tool_uses
    asked = call.say("Ja" if lang == "sv" else "Yes")
    assert asked.text == agent_model.SENTENCES["ask_name"][0 if lang == "sv" else 1]
    assert _use(call.say(name_line)) == ("create_reservation", {"holdId": "h1", "name": "Anna", "partySize": 4,
                                                                "language": lang})
    assert booked in call.tool({"reservationId": "r1", "guaranteeRequired": False}).text


@pytest.mark.parametrize("opening, lang", [("Finns det lediga bord för två på fredag?", "sv"),
                                           ("Do you have any tables for two on Friday?", "en")])
def test_scenario_full_day_offers_the_next_day(opening, lang):
    call = Call()
    assert _use(call.say(opening)) == ("get_availability", {"date": "2026-11-06", "partySize": 2})
    full = call.tool({"times": []})
    assert full.text == agent_model.CONFIRMATIONS["get_availability_full"][0 if lang == "sv" else 1]
    assert _use(call.say("Ja, gärna" if lang == "sv" else "Yes please")) == (
        "get_availability", {"date": "2026-11-07", "partySize": 2})
    offer = call.tool({"times": ["17:00", "18:30", "20:00", "21:00"]})
    assert offer.text.count(":") == 3 and "21:00" not in offer.text       # at most three times


@pytest.mark.parametrize("opening, lang", [("Jag vill boka för tolv personer på lördag klockan 18", "sv"),
                                           ("I want to book for twelve people on Saturday at 6 pm", "en")])
def test_scenario_large_party_gets_the_guarantee_link_never_card_details(opening, lang):
    call = Call()
    assert _use(call.say(opening))[1] == {"date": "2026-11-07", "time": "18:00", "partySize": 12}
    call.tool({"holdId": "h2"})
    call.say("Jag heter Bo" if lang == "sv" else "My name is Bo")
    assert _use(call.tool({"reservationId": "r2", "guaranteeRequired": True})) == (
        "send_guarantee_link", {"reservationId": "r2"})
    sent = call.tool({"sent": True})
    assert sent.text == agent_model.CONFIRMATIONS["send_guarantee_link"][0 if lang == "sv" else 1]


@pytest.mark.parametrize("opening, lang", [("Jag vill flytta min bokning till fredag klockan 20", "sv"),
                                           ("I'd like to move my booking to Friday at 8 pm", "en")])
def test_scenario_move_inside_cutoff(opening, lang):
    call = Call()
    assert _use(call.say(opening)) == ("find_reservations", {})
    assert _use(call.tool({"reservations": [{"reservationId": "r3", "date": "2026-11-04", "time": "19:00"}]})) == (
        "move_reservation", {"reservationId": "r3", "date": "2026-11-06", "time": "20:00"})
    assert call.tool({"ok": True}).text == agent_model.CONFIRMATIONS["move_reservation"][0 if lang == "sv" else 1]


@pytest.mark.parametrize("opening, yes, lang", [("Jag vill avboka min bokning", "Ja", "sv"),
                                                ("I need to cancel my reservation", "Yes", "en")])
def test_scenario_cancel_inside_cutoff(opening, yes, lang):
    call = Call()
    assert _use(call.say(opening)) == ("find_reservations", {})
    found = call.tool({"reservations": [{"reservationId": "r4", "date": "2026-11-05", "time": "18:00"}]})
    assert "2026-11-05" in found.text and ("avboka" if lang == "sv" else "cancel") in found.text
    assert _use(call.say(yes)) == ("cancel_reservation", {"reservationId": "r4"})
    assert call.tool({"ok": True}).text == agent_model.CONFIRMATIONS["cancel_reservation"][0 if lang == "sv" else 1]


@pytest.mark.parametrize("question, tool, lang", [
    ("Har ni öppet på julafton?", "get_restaurant_info", "sv"),
    ("Are you open on Christmas Eve?", "get_restaurant_info", "en"),
    ("Har ni något vegetariskt på menyn?", "get_menu", "sv"),
    ("Do you have anything vegetarian on the menu?", "get_menu", "en"),
])
def test_scenario_special_day_and_menu_questions(question, tool, lang):
    call = Call()
    name, payload = _use(call.say(question))
    assert name == tool and (payload == {} or payload == {"query": question})
    answer = call.tool({"summary": "Stängt 24 december." if tool == "get_restaurant_info" else "Svamprisotto"})
    assert ("Stängt" in answer.text or "Svamprisotto" in answer.text) and not answer.tool_uses


@pytest.mark.parametrize("question, yes, lang", [("Kan ni ordna en tårta till ett kalas?", "Ja", "sv"),
                                                 ("Could you arrange a cake for a party?", "Yes", "en")])
def test_scenario_unknown_question_becomes_a_callback(question, yes, lang):
    call = Call()
    assert call.say(question).text == agent_model.SENTENCES["unknown"][0 if lang == "sv" else 1]
    assert _use(call.say(yes)) == ("request_callback", {"topic": question})


@pytest.mark.parametrize("ask, yes, lang", [("Kan jag få prata med en människa?", "Ja tack", "sv"),
                                            ("Can I speak to a human please?", "Yes", "en")])
def test_scenario_human_transfer_and_unanswered_transfer(ask, yes, lang):
    call = Call()
    assert _use(call.say(ask)) == ("transfer_to_staff", {"reason": "caller_asked"})
    resumed = call.tool({"answered": False})
    assert resumed.text == agent_model.CONFIRMATIONS["transfer_to_staff_unanswered"][0 if lang == "sv" else 1]
    assert _use(call.say(yes))[0] == "request_callback"
    answered = Call()
    answered.say(ask)
    assert answered.tool({"answered": True}).text == agent_model.CONFIRMATIONS["transfer_to_staff"][
        0 if lang == "sv" else 1]


@pytest.mark.parametrize("ask, lang", [("Kan ni ringa tillbaka till mig på 070-123 45 67?", "sv"),
                                       ("Can you call me back on 070-123 45 67?", "en")])
def test_callback_with_a_number(ask, lang):
    assert _use(Call().say(ask)) == ("request_callback", {"topic": ask, "phone": "+46701234567"})


def test_booking_asks_for_missing_slots_and_gathers_answers():
    call = Call()
    assert call.say("Jag vill boka ett bord").text == agent_model.SENTENCES["ask_party_size"][0]
    assert call.say("Tre personer").text == agent_model.SENTENCES["ask_date"][0]
    assert _use(call.say("Imorgon")) == ("get_availability", {"date": "2026-11-03", "partySize": 3})
    call.tool({"times": ["18:00", "19:30"]})
    assert _use(call.say("Klockan 19.30")) == ("hold_table", {"date": "2026-11-03", "time": "19:30",
                                                              "partySize": 3})
    english = Call()
    assert english.say("I want to book a table").text == agent_model.SENTENCES["ask_party_size"][1]


def test_no_and_tool_errors_and_tools_not_offered():
    call = Call()
    assert call.say("Nej").text == agent_model.SENTENCES["no"][0]
    assert call.say("No thanks").text == agent_model.SENTENCES["no"][1]
    foreign = Call()
    foreign.say("Jag vill avboka min bokning")
    # a tool refusing (e.g. another tenant's reservation id -> 404) makes the agent say so, never invent
    assert foreign.tool({"ok": False, "status": 404}).text == agent_model.CONFIRMATIONS["error"][0]
    model = SimulatedAgentModel(today=TODAY)
    turn = model.converse("s", [{"role": "user", "content": "Kan jag få prata med en människa?"}], [], max_tokens=5)
    assert turn.text == agent_model.SENTENCES["cannot"][0] and not turn.tool_uses


def test_intent_table_covers_every_intent_in_both_languages():
    sentences = {
        "human": ("Koppla mig till personalen", "I want to talk to someone"),
        "callback": ("Ring mig tillbaka", "Please call me back"),
        "cancel": ("Jag vill avboka", "Cancel my table"),
        "move": ("Kan jag flytta min bokning?", "Can I reschedule?"),
        "availability": ("Har ni bord ikväll?", "Any free tables tonight?"),
        "book": ("Jag vill boka", "I want to book"),
        "menu": ("Vad finns på menyn?", "What's on the menu?"),
        "hours": ("När stänger ni?", "What are your opening hours?"),
        "yes": ("Ja", "Yes"),
        "no": ("Nej", "No"),
    }
    assert set(sentences) == {i for i, _, _ in agent_model.INTENTS}
    for intent, (sv, en) in sentences.items():
        assert agent_model.match_intent(sv) == (intent, "sv"), sv
        assert agent_model.match_intent(en) == (intent, "en"), en
    assert agent_model.match_intent("Blablabla") == (None, None)


@pytest.mark.parametrize("text, expected", [
    ("070-123 45 67", {"phone": "+46701234567"}),
    ("+46 70 123 45 67", {"phone": "+46701234567"}),
    ("2026-12-24", {"date": "2026-12-24"}),
    ("på söndag", {"date": "2026-11-08"}),
    ("on Monday", {"date": "2026-11-09"}),
    ("kl 18:45", {"time": "18:45"}),
    ("at 7", {"time": "19:00"}),
    ("7pm", {"time": "19:00"}),
    ("fem personer", {"partySize": 5}),
    ("for 6", {"partySize": 6}),
    ("six guests", {"partySize": 6}),
])
def test_entities(text, expected):
    assert agent_model.entities(text, TODAY) == expected


def test_canned_responses_override_in_order():
    model = SimulatedAgentModel(responses=[
        "Hej!", {"tool_uses": [{"id": "t1", "name": "get_menu", "input": {"query": "fisk"}}]},
        Turn(text="Klart", stop_reason="end_turn")])
    msgs = [{"role": "user", "content": "x"}]
    assert model.converse("s", msgs, TOOLS, max_tokens=5) == Turn(text="Hej!")
    assert model.converse("s", msgs, TOOLS, max_tokens=5) == Turn(
        None, (ToolUse("t1", "get_menu", {"query": "fisk"}),), "tool_use")
    assert model.converse("s", msgs, TOOLS, max_tokens=5).text == "Klart"
    assert model.converse("s", msgs, TOOLS, max_tokens=5).text == agent_model.SENTENCES["unknown"][0]  # sv default


def test_delay_ms_sleeps_for_the_latency_harness():
    model = SimulatedAgentModel(delay_ms=50, today=TODAY)
    start = time.monotonic()
    model.converse("s", [{"role": "user", "content": "Jag vill boka"}], TOOLS, max_tokens=5)
    assert time.monotonic() - start >= 0.05


def test_deterministic():
    script = ["Jag vill boka ett bord för två imorgon klockan 18"]
    first = [SimulatedAgentModel(today=TODAY).converse("s", [{"role": "user", "content": s}], TOOLS, max_tokens=5)
             for s in script]
    second = [SimulatedAgentModel(today=TODAY).converse("s", [{"role": "user", "content": s}], TOOLS, max_tokens=5)
              for s in script]
    assert first == second
