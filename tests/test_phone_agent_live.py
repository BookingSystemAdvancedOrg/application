"""Feature L, the parts that take the agent to real calls (owner 2026-11-13):
automatic language switching, the location from the called number (and the
question on a shared number), per-location knowledge and isolation between
locations and tenants, the counter-offers end to end through the wizard's
simulator (route L6, what dev shows), and a whole call through the real
ElevenLabs adapter's webhooks and turn endpoint (HTTP faked)."""

import hashlib
import hmac
import json
import time

import pytest

from shared.agent_model import Turn
from shared.features import phone_agent as pa
from shared.features.phone_agent import language
from shared.voice_provider_real import RealVoiceProvider
from test_phone_agent_calls import KEY, NUMBER, Phone, live  # noqa: F401 (fixture)
from test_phone_agent_support import (CALLER, L, LOC_A, LOC_A2, LOC_B, OWNER_B, TENANT_A, TENANT_B, call,
                                      enable, env, webhook)
from tenant_support import LOCATION_TABLE_DEFAULT

assert live and env
TOMORROW = "2026-11-03"
NUMBER_A2 = "+46100000002"
TOOLS = [{"type": "function", "function": {"name": n}} for n in ("language_detection", "end_call",
                                                                 "transfer_to_number")]


def turn(e, body, *, auth=None):
    out = e.live.handler({"routeKey": "$default", "rawPath": "/v1/chat/completions",
                          "headers": auth or {"x-booqy-agent-key": KEY},
                          "requestContext": {"http": {"method": "POST"}}, "body": json.dumps(body)})
    if out["statusCode"] != 200:
        return out["statusCode"], None, []
    chunks = [json.loads(line.removeprefix("data: ")) for line in out["body"].split("\n\n")
              if line.startswith("data: {")]
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    tools = [tc["function"] for c in chunks for tc in c["choices"][0]["delta"].get("tool_calls") or []]
    return 200, text, [{"name": t["name"], "arguments": json.loads(t["arguments"])} for t in tools]


def name_locations(e, **names):
    for location_id, name in names.items():
        e.res.Table(LOCATION_TABLE_DEFAULT).update_item(
            Key={"PK": f"TENANT#{TENANT_A if location_id != LOC_B else TENANT_B}", "SK": f"LOCATION#{location_id}"},
            UpdateExpression="SET #n = :n", ExpressionAttributeNames={"#n": "name"},
            ExpressionAttributeValues={":n": name})


def dial(e, ref, to):
    """A call to `to` (Phone dials the first location's number)."""
    status, body = webhook(e, "voice", [{"kind": "call_started", "call_ref": ref, "from": CALLER, "to": to}])
    assert status == 200
    phone = Phone.__new__(Phone)
    phone.e, phone.ref, phone.ids = e, ref, body["dynamicVariables"]
    phone.call_id = phone.ids["callId"]
    return phone


def tool_events(e, since=0):
    return e.invoke.calls[since:]


# --- language -------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, lang", [
    ("Hej, jag vill boka ett bord för fyra imorgon", "sv"),
    ("Hi, I would like to book a table for four tomorrow", "en"),
    ("Hallo, ich möchte einen Tisch für vier Personen reservieren", "de"),
    ("Hei, jeg vil gjerne bestille et bord", "no"),
    ("Hej, jeg vil gerne bestille et bord", "da"),
    ("Bonjour, je voudrais réserver une table pour demain", "fr"),
    ("Hola, quisiera reservar una mesa para mañana", "es"),
    ("Ciao, vorrei prenotare un tavolo per domani", "it"),
    ("Dzień dobry, chciałbym zarezerwować stolik na jutro", "pl"),
    ("Hei, haluaisin varata pöydän huomenna", "fi"),
    ("مرحبا أريد حجز طاولة لأربعة أشخاص", "ar"),
])
def test_language_of_a_caller_sentence(text, lang):
    assert language.detect(text) == lang


@pytest.mark.parametrize("text", ["ok", "Ja", "Anna", "19.30", "Malmö", "mm"])
def test_short_answers_never_switch_the_call(text):
    assert language.next_language("sv", text) == "sv"
    assert language.next_language("en", text) == "en"


def test_caller_in_english_gets_the_disclosure_in_english_and_the_provider_switches(live):
    phone = Phone(live)
    status, text, tools = turn(live, {"booqy": phone.ids, "tools": TOOLS, "messages": [
        {"role": "user", "content": "Hi, I'd like to book a table for four tomorrow at 7 pm"}]})
    assert status == 200
    assert text.startswith("I am an AI assistant for")
    assert {"name": "language_detection", "arguments": {"reason": "The caller speaks English", "language": "en"}} \
        in tools
    row = phone.row()
    assert row["language"] == "en" and row["disclosure"]["languages"] == ["sv", "en"]
    turns = [t["text"] for t in phone.turns()]
    assert "AI-assistent" in turns[0] and turns[2].startswith("I am an AI assistant")   # greeting, caller, disclosure
    # the second English sentence: no second disclosure, no second switch
    status, text, tools = turn(live, {"booqy": phone.ids, "tools": TOOLS, "messages": [
        {"role": "user", "content": "My name is Anna"}]})
    assert "AI assistant" not in text and not [t for t in tools if t["name"] == "language_detection"]


def test_any_language_reaches_the_model_and_the_disclosure_follows(live):
    seen = []
    converse = live.model.converse

    def spy(system, messages, tools, *, max_tokens):
        seen.append(system)
        return converse(system, messages, tools, max_tokens=max_tokens)

    live.model.converse = spy
    live.model.responses = [Turn(text="Gerne. Für wie viele Personen?")]
    phone = Phone(live)
    reply = phone.say("Hallo, ich möchte einen Tisch für morgen reservieren")
    assert reply == "Ich bin ein KI-Assistent von Location loc-a-1. Gerne. Für wie viele Personen?"
    assert "Answer in German" in seen[0]
    assert phone.row()["language"] == "de"


def test_any_language_the_model_answers_in_is_followed(live):
    """A language the word lists do not know (Turkish): the model's [tr] tag
    switches the call, Booqy says the fixed AI sentence in Turkish first."""
    live.model.responses = [Turn(text="[tr] Tabii, kaç kişi için?")]
    phone = Phone(live)
    status, text, tools = turn(live, {"booqy": phone.ids, "tools": TOOLS, "messages": [
        {"role": "user", "content": "Merhaba, yarın akşam için masa ayırtmak istiyorum"}]})
    assert text == "Location loc-a-1 için bir yapay zekâ asistanıyım. Tabii, kaç kişi için?"
    assert tools == [{"name": "language_detection", "arguments": {"reason": "The caller speaks Turkish",
                                                                  "language": "tr"}}]
    assert phone.row()["language"] == "tr" and phone.row()["disclosure"]["languages"] == ["sv", "tr"]
    live.model.responses = [Turn(text="[tr] Harika.")]
    status, text, tools = turn(live, {"booqy": phone.ids, "tools": TOOLS, "messages": [
        {"role": "user", "content": "Dört kişi"}]})
    assert text == "Harika." and tools == []      # same language: no second disclosure, no second switch


def test_a_tag_naming_a_language_the_voice_cannot_speak_is_ignored(live):
    live.model.responses = [Turn(text="[xx] Hej hej.")]
    phone = Phone(live)
    assert phone.say("Hej, har ni öppet ikväll?") == "Hej hej."
    assert phone.row()["language"] == "sv"


def test_every_voice_language_has_the_fixed_disclosure():
    from shared.features.phone_agent import copy
    assert set(language.VOICE_LANGUAGES) <= set(copy.DISCLOSURE_SHORT)
    for lang in language.VOICE_LANGUAGES:
        assert "Bistro" in copy.disclosure_short(lang, "Bistro")


# --- location from the number, one number for several locations ------------------------------------

def test_shared_number_asks_which_location_first_and_scopes_every_tool(live):
    name_locations(live, **{LOC_A: "Brasserie Malmö", LOC_A2: "Brasserie Norrmalm"})
    live.invoke.menus = {LOC_A: {"items": [{"name": "Sill", "price": 99}]},
                         LOC_A2: {"items": [{"name": "Svamprisotto", "price": 210}]}}
    body = enable(live, location=LOC_A2, shareNumberWith=LOC_A)
    assert body["number"] == NUMBER and body["numberSharedWith"] == LOC_A
    assert len(live.tel.numbers) == 1          # nothing provisioned for the second location
    phone = Phone(live)
    greeting = phone.turns()[0]["text"]
    assert greeting.endswith("Vilken av våra restauranger gäller det, Brasserie Malmö eller Brasserie Norrmalm?")
    since = len(live.invoke.calls)
    assert phone.say("Vet inte riktigt") == ("Förlåt, jag hörde inte vilken. Är det Brasserie Malmö eller "
                                             "Brasserie Norrmalm?")
    assert phone.say("Det gäller Norrmalm") == "Brasserie Norrmalm, toppen. Vad kan jag hjälpa dig med?"
    assert tool_events(live, since) == []      # no tool before the caller chose
    assert phone.row()["locationId"] == LOC_A2 and not phone.row().get("locationPending")
    reply = phone.say("Har ni något vegetariskt på menyn?")
    assert "Svamprisotto" in reply and "Sill" not in reply
    for fn, _, event in tool_events(live, since):
        assert event["pathParameters"]["locationId"] == LOC_A2, fn


def test_shared_number_with_the_whole_request_in_one_sentence(live):
    name_locations(live, **{LOC_A: "Brasserie Malmö", LOC_A2: "Brasserie Norrmalm"})
    enable(live, location=LOC_A2, shareNumberWith=LOC_A)
    live.availability.set(TOMORROW, ["19:00"])
    phone = Phone(live)
    reply = phone.say("Malmö, jag vill boka ett bord för fyra imorgon klockan 19")
    assert phone.row()["locationId"] == LOC_A
    assert "klockan 19:00" in reply and "Ska jag boka det?" in reply


def test_shared_number_skips_the_question_when_only_one_location_answers(live):
    name_locations(live, **{LOC_A: "Brasserie Malmö", LOC_A2: "Brasserie Norrmalm"})
    enable(live, location=LOC_A2, shareNumberWith=LOC_A)
    status, loc = call(live, "GET", L, location=LOC_A)
    status, _ = call(live, "POST", L + "/kill-switch", location=LOC_A, body={"on": True, "version": loc["version"]})
    assert status == 200
    phone = Phone(live)
    assert "Vilken av" not in phone.turns()[0]["text"] and "Brasserie Norrmalm" in phone.turns()[0]["text"]
    assert phone.row()["locationId"] == LOC_A2


def test_sharing_location_disabled_keeps_the_number_for_the_owner(live):
    enable(live, location=LOC_A2, shareNumberWith=LOC_A)
    status, loc = call(live, "GET", L, location=LOC_A2)
    body = enable(live, location=LOC_A2, enabled=False, version=loc["version"])
    assert body["number"] is None
    row = pa.table().get_item(Key=pa.number_key(TENANT_A, NUMBER))["Item"]
    assert row["locationIds"] == [LOC_A]
    status, owner = call(live, "GET", L, location=LOC_A)
    assert owner["number"] == NUMBER


def test_share_needs_an_enabled_location_of_the_same_tenant(live):
    status, body = call(live, "PUT", L, as_=OWNER_B, location=LOC_B,
                        body={"enabled": True, "mode": "always", "agentHours": {}, "maxCallSeconds": 480,
                              "version": 0, "shareNumberWith": LOC_A})
    assert status == 409 and body["code"] == "share_unavailable"   # tenant B has no LOC_A
    assert len(live.tel.numbers) == 1


# --- per-location knowledge and isolation ----------------------------------------------------------

def _two_locations(e):
    name_locations(e, **{LOC_A: "Brasserie Malmö", LOC_A2: "Brasserie Norrmalm"})
    e.invoke.menus = {LOC_A: {"items": [{"name": "Sill", "price": 99}]},
                      LOC_A2: {"items": [{"name": "Svamprisotto", "price": 210}]}}
    e.invoke.site = {"locations": [
        {"locationId": LOC_A, "name": "Brasserie Malmö", "address": "Stortorget 1, Malmö",
         "businessHours": {"mon": [{"opensAt": "11:00", "closesAt": "22:00"}]},
         "specialDays": [{"date": "2026-12-24", "kind": "closed", "note": "Julafton"}]},
        {"locationId": LOC_A2, "name": "Brasserie Norrmalm", "address": "Vasagatan 9, Stockholm",
         "businessHours": {"mon": [{"opensAt": "16:00", "closesAt": "23:30"}]},
         "specialDays": [{"date": "2026-12-31", "kind": "hours",
                          "intervals": [{"opensAt": "18:00", "closesAt": "02:00"}]}]}]}
    enable(e, location=LOC_A2)


def test_multi_location_tenant_each_number_knows_only_its_location(live):
    _two_locations(live)
    malmo = Phone(live, ref="ref-malmo")
    norrmalm = dial(live, "ref-norrmalm", NUMBER_A2)
    assert norrmalm.row()["locationId"] == LOC_A2 and malmo.row()["locationId"] == LOC_A
    assert "Brasserie Norrmalm" in norrmalm.turns()[0]["text"]

    since = len(live.invoke.calls)
    hours_a = malmo.say("När har ni öppet?")
    menu_a = malmo.say("Vad finns på menyn?")
    assert "11:00" in hours_a and "Julafton" in hours_a and "Stortorget" in hours_a
    assert "16:00" not in hours_a and "Vasagatan" not in hours_a and "2026-12-31" not in hours_a
    assert "Sill" in menu_a and "Svamprisotto" not in menu_a
    for fn, _, event in tool_events(live, since):
        assert event["pathParameters"].get("locationId") in (LOC_A, None), fn

    since = len(live.invoke.calls)
    hours_b = norrmalm.say("När har ni öppet?")
    menu_b = norrmalm.say("Vad finns på menyn?")
    assert "16:00" in hours_b and "18:00-02:00" in hours_b and "Vasagatan" in hours_b
    assert "11:00" not in hours_b and "Julafton" not in hours_b and "Stortorget" not in hours_b
    assert "Svamprisotto" in menu_b and "Sill" not in menu_b
    for fn, _, event in tool_events(live, since):
        assert event["pathParameters"].get("locationId") in (LOC_A2, None), fn


def test_bookings_and_availability_are_read_and_written_for_the_called_location_only(live):
    _two_locations(live)
    live.availability.set(TOMORROW, ["19:00"])
    phone = dial(live, "ref-a2", NUMBER_A2)
    since = len(live.invoke.calls)
    phone.say("Hej, jag vill boka ett bord för fyra imorgon klockan 19")
    phone.say("Ja")
    phone.say("Jag heter Anna")
    events = tool_events(live, since)
    assert {fn for fn, _, _ in events} >= {"get-availability", "create-pending-reservation"}
    for fn, _, event in events:
        assert event["pathParameters"]["locationId"] == LOC_A2
        claims = ((event["requestContext"].get("authorizer") or {}).get("jwt") or {}).get("claims")
        if claims:
            assert claims == {"tenant_id": TENANT_A, "role": "staff_user", "sub": "agent:" + LOC_A2,
                              "cognito:groups": ["staff_user"]}


def test_foreign_tenant_is_never_reached(live):
    ids_b = dial(live, "ref-b", enable(live, location=LOC_B, as_=OWNER_B)["number"]).ids
    assert ids_b["tenantId"] == TENANT_B
    phone_a = Phone(live)
    # a turn naming tenant A with B's call (or B with A's) finds no call
    for ids in ({"tenantId": TENANT_A, "callId": ids_b["callId"]},
                {"tenantId": TENANT_B, "callId": phone_a.call_id}):
        status, _, _ = turn(live, {"booqy": ids, "messages": [{"role": "user", "content": "Hej"}]})
        assert status == 404
    # B's agent asked to cancel a booking of tenant A by its id: not found, nothing written
    live.invoke.listed = {TOMORROW: [{"reservationId": "res-of-a", "customerPhone": CALLER, "status": "reserved",
                                      "startTime": "19:00", "partySize": 2}]}
    live.model.responses = [
        {"tool_uses": [{"id": "u1", "name": "cancel_reservation", "input": {"reservationId": "res-of-a"}}]},
        Turn(text="Jag hittar tyvärr ingen sådan bokning."),
        {"tool_uses": [{"id": "u2", "name": "get_restaurant_info", "input": {}}]},
        Turn(text="ok"),
    ]
    since = len(live.invoke.calls)
    status, text, _ = turn(live, {"booqy": ids_b, "messages": [{"role": "user", "content": "Avboka res-of-a"}]})
    assert status == 200 and "ingen sådan bokning" in text
    status, _, _ = turn(live, {"booqy": ids_b, "messages": [{"role": "user", "content": "Var ligger ni?"}]})
    events = tool_events(live, since)
    assert not [fn for fn, _, _ in events if fn == "mark-arrived"]
    for fn, _, event in events:
        assert event["pathParameters"].get("locationId") in (LOC_B, None)
        if fn == "tenant-site-config":
            assert event["queryStringParameters"] == {"tenantId": TENANT_B}
    b_call = live.live.calls.get_call(TENANT_B, ids_b["callId"])
    tool_turns = pa.query_all(pa.tenant_pk(TENANT_B), b_call["SK"] + "#T#")
    assert [t.get("toolOk") for t in tool_turns if t["role"] == "tool"] == [False, False]   # A's site row unseen


# --- counter-offers end to end through the wizard's simulator (what dev shows) ---------------------

SIM = {
    "sv": ["Hej, jag vill boka ett bord för fyra imorgon klockan 19", "Klockan 18", "Ja", "Jag heter Anna"],
    "en": ["Hi, I'd like to book a table for four tomorrow at 7 pm", "At 6 pm", "Yes", "My name is Anna"],
}
EXPECT = {
    "sv": ["Klockan 19:00 är tyvärr fullt. Jag kan erbjuda 17:30, 18:00 eller 20:30. Vad passar?",
           "Jag håller ett bord", "Vilket namn", "Klart, du är bokad"],
    "en": ["I'm afraid 19:00 is fully booked. I can offer 17:30, 18:00 or 20:30. What suits you?",
           "I'm holding a table", "What name", "Done, you're booked"],
}


@pytest.mark.parametrize("lang", ["sv", "en"])
def test_simulator_counter_offer_end_to_end(live, lang):
    live.invoke.layout = None   # no floor plan: times only
    live.availability.set(TOMORROW, ["17:30", "18:00", "20:30", "21:00"])
    session = f"demo-{lang}-1"
    replies = []
    for text in SIM[lang]:
        status, body = call(live, "POST", L + "/simulate", body={"sessionId": session, "text": text})
        assert status == 200, body
        replies.append(body)
    for body, expected in zip(replies, EXPECT[lang]):
        assert expected in body["reply"], body["reply"]
    assert replies[0]["disclosure"] and "AI-assistent" in replies[0]["reply"]
    if lang == "en":   # the simulator starts in Swedish and switches, like a call
        assert "I am an AI assistant for" in replies[0]["reply"] and replies[0]["language"] == "en"
    assert replies[-1]["state"]["counterOffer"] == "other_time" and replies[-1]["state"]["outcome"] == "booked"
    assert live.invoke.reservations == {}      # dry run: nothing booked
    assert not [c for c in live.invoke.calls if c[0] == "create-pending-reservation"]


# --- a whole call through the real ElevenLabs adapter (HTTP faked) --------------------------------

WEBHOOK_SECRET, INIT_TOKEN = "whsec-test", "init-token-test"


def eleven(e, monkeypatch):
    voice = RealVoiceProvider(secret={"apiKey": "k", "webhookSecret": WEBHOOK_SECRET,
                                      "initiationToken": INIT_TOKEN, "callUrl": "https://x.lambda-url.on.aws/"},
                              llm_key=KEY, transport=lambda *a: (500, {}))
    e.live._clients["voice"] = voice
    return voice


def fn_url(e, path, body, headers):
    out = e.live.handler({"routeKey": "$default", "rawPath": path, "headers": headers,
                          "requestContext": {"http": {"method": "POST", "path": path, "sourceIp": "1.2.3.4"}},
                          "body": body if isinstance(body, str) else json.dumps(body)})
    return out["statusCode"], json.loads(out["body"] or "{}") if out.get("body") else {}


def signed(payload, *, at=None):
    raw = json.dumps(payload)
    stamp = str(int(at or time.time()))
    sig = hmac.new(WEBHOOK_SECRET.encode(), f"{stamp}.{raw}".encode(), hashlib.sha256).hexdigest()
    return raw, {"ElevenLabs-Signature": f"t={stamp},v0={sig}"}


def test_elevenlabs_call_from_initiation_to_post_call(live, monkeypatch):
    eleven(live, monkeypatch)
    hook = "/public/phone-agent/voice/webhook"
    status, _ = fn_url(live, hook, {"caller_id": CALLER, "called_number": NUMBER, "agent_id": "ag1",
                                    "conversation_id": "conv-1"}, {"x-booqy-webhook-token": "wrong"})
    assert status == 404
    status, init = fn_url(live, hook, {"caller_id": CALLER, "called_number": NUMBER, "agent_id": "ag1",
                                       "call_sid": "sip-1", "conversation_id": "conv-1"},
                          {"x-booqy-webhook-token": INIT_TOKEN})
    assert status == 200 and init["type"] == "conversation_initiation_client_data"
    first = init["conversation_config_override"]["agent"]["first_message"]
    assert "AI-assistent" in first and init["conversation_config_override"]["agent"]["language"] == "sv"
    ids = init["dynamic_variables"]
    assert init["custom_llm_extra_body"] == {"booqy": ids} and ids["tenantId"] == TENANT_A

    # ElevenLabs' custom LLM request: Bearer key, the prompt with the call marker, its system tools
    marker = f"booqy-call tenant={ids['tenantId']} call={ids['callId']}"
    status, text, tools = turn(live, {"model": "booqy-phone-agent", "stream": True, "tools": TOOLS, "messages": [
        {"role": "system", "content": marker}, {"role": "assistant", "content": first},
        {"role": "user", "content": "Hi, can I speak to a person please"}]},
        auth={"Authorization": f"Bearer {KEY}"})
    assert status == 200 and text.startswith("I am an AI assistant")
    names = [t["name"] for t in tools]
    assert names == ["language_detection", "transfer_to_number"]
    transfer = tools[1]["arguments"]
    assert transfer["transfer_number"] == "+46812345678" and transfer["client_message"]
    call_row = live.live.calls.get_call(TENANT_A, ids["callId"])
    assert call_row["outcome"] == "transferred" and call_row["status"] == "processed"

    # the post-call webhook: art. 50 evidence from the transcript, metered once
    start = int(pa.now().timestamp())
    payload = {"type": "post_call_transcription", "event_timestamp": start + 70, "data": {
        "agent_id": "ag1", "conversation_id": "conv-1",
        "transcript": [{"role": "agent", "message": first, "time_in_call_secs": 0},
                       {"role": "user", "message": "Hi, can I speak to a person please", "time_in_call_secs": 6}],
        "metadata": {"start_time_unix_secs": start, "call_duration_secs": 70},
        "conversation_initiation_client_data": {"dynamic_variables": {**ids, "system__caller_id": CALLER}}}}
    raw, headers = signed(payload, at=start - 3600)
    assert fn_url(live, hook, raw, headers)[0] == 404          # stale signature
    raw, headers = signed(payload)
    assert fn_url(live, hook, raw, headers)[0] == 200
    assert fn_url(live, hook, raw, headers)[0] == 200          # retried by ElevenLabs: no second metering
    row = live.live.calls.get_call(TENANT_A, ids["callId"])
    assert row["disclosure"]["playedAt"] and row["disclosure"]["verbatim"] is True
    assert len([d for t, d in live.published if t == "PhoneCall.Ended"]) == 1


def test_elevenlabs_refused_call_hears_the_refusal_and_is_hung_up(live, monkeypatch):
    eleven(live, monkeypatch)
    status, loc = call(live, "GET", L)
    call(live, "POST", L + "/kill-switch", body={"on": True, "version": loc["version"]})
    status, init = fn_url(live, "/public/phone-agent/voice/webhook",
                          {"caller_id": CALLER, "called_number": NUMBER, "conversation_id": "conv-2"},
                          {"x-booqy-webhook-token": INIT_TOKEN})
    assert status == 200
    assert init["conversation_config_override"]["agent"]["first_message"].startswith("Tyvärr kan vi inte")
    status, text, tools = turn(live, {"booqy": init["dynamic_variables"], "tools": TOOLS,
                                      "messages": [{"role": "user", "content": "Hallå?"}]})
    assert status == 200 and tools == [{"name": "end_call", "arguments": {"reason": "call_ended"}}]


def test_agent_end_call_hangs_up_through_the_provider(live, monkeypatch):
    eleven(live, monkeypatch)
    _, init = fn_url(live, "/public/phone-agent/voice/webhook",
                     {"caller_id": CALLER, "called_number": NUMBER, "conversation_id": "conv-3"},
                     {"x-booqy-webhook-token": INIT_TOKEN})
    live.model.responses = [
        {"tool_uses": [{"id": "u1", "name": "end_call", "input": {"outcome": "info", "summary": "Frågade om tider"}}]},
        Turn(text="Tack för samtalet, hej då."),
    ]
    status, text, tools = turn(live, {"booqy": init["dynamic_variables"], "tools": TOOLS,
                                      "messages": [{"role": "user", "content": "Tack, det var allt"}]})
    assert text == "Tack för samtalet, hej då."
    assert tools == [{"name": "end_call", "arguments": {"reason": "info"}}]


def test_tool_calls_are_only_sent_when_the_provider_offers_them(live):
    phone = Phone(live)
    status, text, tools = turn(live, {"booqy": phone.ids, "messages": [
        {"role": "user", "content": "Hi, I'd like to book a table for four tomorrow at 7 pm"}]})
    assert status == 200 and tools == []


def test_turn_endpoint_answers_on_the_paths_elevenlabs_may_use(live):
    phone = Phone(live)
    for path in ("/v1/chat/completions", "/chat/completions", "/v1", "/"):
        out = live.live.handler({"routeKey": "$default", "rawPath": path, "headers": {"x-booqy-agent-key": KEY},
                                 "requestContext": {"http": {"method": "POST"}},
                                 "body": json.dumps({"booqy": phone.ids, "messages": [
                                     {"role": "user", "content": "Har ni något vegetariskt på menyn?"}]})})
        assert out["statusCode"] == 200 and out["headers"]["Content-Type"] == "text/event-stream", path



# --- the Amazon Connect + Lex V2 test line ---------------------------------------------------------

def lex(e, session, text, attrs=None):
    event = {"sessionId": session, "inputTranscript": text, "invocationSource": "DialogCodeHook",
             "bot": {"localeId": "sv_SE"}, "sessionState": {
                 "intent": {"name": "FallbackIntent", "state": "InProgress"},
                 "sessionAttributes": attrs or {"callerNumber": CALLER, "dialedNumber": NUMBER}}}
    out = e.live.handler({"lexV2": event})
    return out, out["sessionState"]["sessionAttributes"]


def test_connect_lex_call_books_a_table_in_swedish(live):
    live.invoke.layout = None
    live.availability.set(TOMORROW, ["17:30", "18:00", "20:30", "21:00"])
    out, attrs = lex(live, "contact-1", "Hej, jag vill boka ett bord för fyra imorgon klockan 19")
    assert out["sessionState"]["dialogAction"] == {"type": "ElicitIntent"}
    assert "17:30, 18:00 eller 20:30" in out["messages"][0]["content"]
    call_row = live.live.calls.get_call(attrs["tenantId"], attrs["callId"])
    assert call_row["callRef"] == "connect-contact-1" and call_row["disclosure"]["playedAt"]
    for text in ("Klockan 18", "Ja", "Jag heter Anna"):
        out, attrs = lex(live, "contact-1", text, attrs)
    assert "Klart, du är bokad" in out["messages"][0]["content"]
    assert len(live.invoke.reservations) == 1
    turns = pa.query_all(pa.tenant_pk(TENANT_A), call_row["SK"] + "#T#")
    assert "AI-assistent" in turns[0]["text"]                       # the greeting the flow played comes first


def test_connect_lex_call_ends_when_the_agent_ends_it(live):
    out, attrs = lex(live, "contact-2", "Hej")
    live.model.responses = [
        {"tool_uses": [{"id": "u1", "name": "end_call", "input": {"outcome": "info", "summary": "Hälsade"}}]},
        Turn(text="Tack för samtalet, hej då."),
    ]
    out, _ = lex(live, "contact-2", "Tack, det var allt", attrs)
    assert out["sessionState"]["dialogAction"] == {"type": "Close"}
    assert out["sessionState"]["intent"]["state"] == "Fulfilled"


def test_connect_lex_unknown_number_or_refused_call_is_closed(live):
    out, _ = lex(live, "contact-3", "Hej", {"callerNumber": CALLER, "dialedNumber": "+4699999999"})
    assert out["sessionState"]["dialogAction"]["type"] == "Close"
    status, loc = call(live, "GET", L)
    call(live, "POST", L + "/kill-switch", body={"on": True, "version": loc["version"]})
    out, _ = lex(live, "contact-4", "Hej")
    assert out["sessionState"]["dialogAction"]["type"] == "Close"
    assert out["messages"][0]["content"].startswith("Tyvärr kan vi inte")
