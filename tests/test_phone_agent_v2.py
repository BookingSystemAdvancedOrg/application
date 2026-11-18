"""Phone agent v2 (owner 2026-11-17): polite and natural in any language the
caller speaks, books tables only from tool results and only after a read-back
and a clear yes. Language hint, hysteresis and unsupported languages; the
English instruction prompt; the tool checks before any read (past date,
horizon, large party, card guarantee), next free days, confirmed writes, the
caller-ID rule; the turn budget, history compaction and retry memory; the
agent principal's booking areas."""

import json
import re
import time

import pytest

from shared import staff_access, tenant
from shared.agent_model import AgentModelError, ToolUse, Turn
from shared.features import phone_agent as pa
from shared.features.phone_agent import agent, copy, language
from shared.features.phone_agent.tools import TOOLS, Executor
from test_phone_agent_calls import KEY, NUMBER, Phone, live  # noqa: F401 (fixture)
from test_phone_agent_support import CALLER, L, LOC_A, TENANT_A, TODAY, call, enable, env, webhook

assert live and env
TOMORROW = "2026-11-03"
DASHES = re.compile("[\u2013\u2014]|&[nm]dash;")


# --- language: the hint, the tag and the decision -----------------------------------------------

@pytest.mark.parametrize("text, lang", [
    ("Здравствуйте, я хотел бы забронировать столик на завтра", "ru"),
    ("Привіт, мені потрібен столик на чотирьох, будь ласка", "uk"),
    ("Waxaan rabaa miis berri fadlan, afar qof", "so"),
    ("Merhaba, yarın akşam için dört kişilik bir masa istiyorum", "tr"),
    ("سلام، می‌خواهم برای فردا یک میز رزرو کنم", "fa"),
    ("سڵاو، دەمەوێت مێزێک بۆ سبەی ڕیزەرڤ بکەم", "ckb"),
    ("ሰላም፣ ለነገ ጠረጴዛ ማስያዝ እፈልጋለሁ", None),        # Ethiopic: Amharic or Tigrinya, the model's tag decides
    ("مرحبا أريد حجز طاولة لأربعة أشخاص", "ar"),
    ("Hallo, ich möchte einen Tisch für morgen reservieren", "de"),
])
def test_hint_reads_scripts_and_more_word_lists(text, lang):
    assert language.hint(text) == lang


@pytest.mark.parametrize("text", ["okay", "Ahmed", "Ja", "Kungsgatan", "mm", "Danke"])
def test_one_word_never_switches_even_when_the_model_tags_it(text):
    allowed = language.V4_LANGUAGES
    assert language.decide("sv", language.hint(text), "de", text, allowed=allowed) == ("sv", None)


def test_switch_needs_a_second_sign():
    allowed = language.V4_LANGUAGES
    # three words in the new language
    assert language.decide("sv", None, "so", "waan ka xumahay saaxiib", allowed=allowed) == ("so", None)
    # the hint agrees
    assert language.decide("sv", "ru", "ru", "Здравствуйте", allowed=allowed) == ("ru", None)
    # the caller asked for a language, in Swedish
    assert language.decide("sv", "sv", "en", "Kan vi prata engelska?", allowed=allowed) == ("en", None)
    # Swedish words (the hint says so): a wrong tag never flips the call
    assert language.decide("sv", "sv", "en", "Jag vill boka ett bord", allowed=allowed) == ("sv", None)
    # the second turn tagged the same language switches
    assert language.decide("sv", None, "de", "Danke", allowed=allowed, previous_tag="de") == ("de", None)


def test_a_language_the_voice_cannot_speak_is_reported_not_switched():
    flash = language.FLASH_LANGUAGES
    assert language.decide("sv", "so", "so", "Waxaan rabaa miis berri", allowed=flash) == ("sv", "so")
    assert language.decide("sv", None, "ti", "ሰላም ጠረጴዛ ክሕዝ ደልየ", allowed=language.V4_LANGUAGES) == ("sv", "ti")
    assert language.tag("[xx] Hej") == (None, "Hej")         # not a language: ignored
    assert language.tag("[nb] Hei")[0] == "no" and language.tag("[ku] Silav")[0] == "kmr"


def test_voice_languages_follow_the_tts_model(monkeypatch):
    monkeypatch.delenv("PHONE_AGENT_TTS_MODEL", raising=False)
    assert "so" not in language.voice_languages() and "de" in language.voice_languages()
    monkeypatch.setenv("PHONE_AGENT_TTS_MODEL", "eleven_v4_turbo")
    assert {"so", "fa", "am", "ckb"} <= set(language.voice_languages())
    assert set(language.voice_languages()) <= set(pa.LANGUAGES)       # the owner's config may list every one


# --- copy and the instruction prompt -------------------------------------------------------------

def test_fixed_sentences_are_polite_and_have_no_dashes():
    for name in dir(copy):
        value = getattr(copy, name)
        texts = value.values() if isinstance(value, dict) else [value] if isinstance(value, str) else []
        for text in texts:
            if isinstance(text, str):
                assert not DASHES.search(text), name
                assert "!" not in text and "toppen" not in text.lower(), name
    assert copy.pick(copy.ERROR, "sw") is None and copy.text(copy.ERROR, "sw") == copy.ERROR["en"]
    assert copy.unsupported_language().count("arabiska") == 1


def test_prompt_is_english_names_the_language_and_lists_every_bookable_day():
    from datetime import date, timedelta
    today = date(2026, 11, 17)
    prompt = agent.system_prompt("de", "Sara", "Bistro A", today, "formal", last_day=today + timedelta(days=21))
    assert "answer only in German" in prompt and "Sie in German" in prompt
    assert "2026-11-17 Tuesday (today)" in prompt and "2026-11-18 Wednesday (tomorrow)" in prompt
    assert "2026-12-08 Tuesday" in prompt and "2026-12-09" not in prompt
    assert "until 2026-12-08 (Tuesday)" in prompt
    assert "{" not in prompt and "}" not in prompt and not DASHES.search(prompt)
    assert prompt.count("clear yes") >= 2 and "Never invent" in prompt
    hidden = agent.system_prompt("so", "Sara", "Bistro A", today, "friendly", disclose_now=True, phone_known=False,
                                 unsupported="ti")
    assert "answer only in Somali" in hidden and "Speech recognition is weak in Somali" in hidden
    assert "first say in one short sentence, in Somali, that you are the AI" in hidden
    assert "number is hidden" in hidden and 'language:ti' in hidden and "Tigrinya" in hidden
    assert "Use du in Swedish" in hidden


def test_tool_schemas_name_formats_and_need_a_confirmation():
    by = {t["name"]: t["input_schema"] for t in TOOLS}
    assert "confirmed" in by["create_reservation"]["required"]
    assert "confirmed" in by["move_reservation"]["required"] and "confirmed" in by["cancel_reservation"]["required"]
    assert "YYYY-MM-DD" in by["get_availability"]["properties"]["date"]["description"]
    assert by["end_call"]["properties"]["outcome"]["enum"][0] == "booked"


# --- tools ---------------------------------------------------------------------------------------

def _ex(e, *, caller=CALLER, location=None, loc=None, tenant_row=None, state=None):
    return Executor(tenant_id=TENANT_A, tenant_row=tenant_row or {"tenantId": TENANT_A},
                    location={"locationId": LOC_A, **(location or {})}, loc_config=loc or {}, invoke=e.invoke,
                    state={} if state is None else state, caller_phone=caller, today=TODAY)


def test_past_and_too_far_dates_are_answered_before_any_read(env):
    ex = _ex(env)
    past = ex.run("get_availability", {"date": "2026-11-01", "partySize": 2})
    assert past["code"] == "pastDate" and past["say"] and past["times"] == []
    far = ex.run("hold_table", {"date": "2026-12-07", "time": "19:00", "partySize": 2})
    assert far["code"] == "beyondHorizon" and far["lastDate"] == "2026-11-23" and far["held"] is False
    assert ex.run("get_availability", {"date": "2026-11-23", "partySize": 2}).get("code") is None   # last day
    assert len([c for c in env.invoke.calls if c[0] == "get-availability"]) >= 1
    assert not [c for c in env.invoke.calls if json.loads(json.dumps(c[2]["queryStringParameters"] or {})).get(
        "date") in ("2026-11-01", "2026-12-07")]


def test_large_party_and_card_guarantee_are_told_before_any_offer(env, monkeypatch):
    from shared import guarantee
    ex = _ex(env, location={"maxPartySizeOnline": 8})
    big = ex.run("get_availability", {"date": TOMORROW, "partySize": 9})
    assert big["code"] == "largeParty" and big["largeParty"] is True and "request_callback" in big["say"]
    assert ex.state["needsCallback"] == "largeParty"
    monkeypatch.setattr(guarantee, "required", lambda location, tenant_row, party: party >= 4)
    env.availability.set(TOMORROW, ["19:00"])
    held = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 4})
    assert held["code"] == "guaranteeRequired" and "holdId" not in held
    assert not [c for c in env.invoke.calls if c[0] == "get-availability"]


def test_phone_max_party_size_books_above_the_online_limit_from_the_staff_view(env):
    env.availability.set(TOMORROW, [], staff={"19:00": {"t6": 6, "t2a": 2}, "20:00": {"t2a": 2}})
    ex = _ex(env, location={"maxPartySizeOnline": 4}, loc={"phoneMaxPartySize": 8})
    answer = ex.run("get_availability", {"date": TOMORROW, "partySize": 6, "time": "19:00"})
    assert answer["times"] == ["19:00"] and answer["suggest"] == ["19:00"]
    held = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 6})
    assert held["holdId"] and ex.state["proposals"][held["holdId"]]["tableIds"] == ["t6"]
    assert "tableIds" not in held                               # no table ids in the model's view
    assert ex.run("get_availability", {"date": TOMORROW, "partySize": 9})["code"] == "largeParty"


def test_full_day_lists_the_next_free_days_in_one_call(env):
    env.availability.set("2026-11-05", ["18:00", "19:30", "21:00"])
    env.availability.set("2026-11-06", ["19:00"])
    env.availability.set("2026-11-07", ["17:00"])
    ex = _ex(env)
    answer = ex.run("get_availability", {"date": TOMORROW, "partySize": 4, "time": "19:00"})
    assert answer["dayFull"] is True and answer["weekday"] == "Tuesday"
    assert answer["nextDays"] == [{"date": "2026-11-05", "weekday": "Thursday", "suggest": ["18:00", "19:30"]},
                                  {"date": "2026-11-06", "weekday": "Friday", "suggest": ["19:00"]}]
    assert ex.state["furthest"] == "next_day"


def test_times_are_capped_and_a_spoken_time_is_put_on_the_slot_grid(env):
    times = [f"{h:02d}:{m:02d}" for h in range(11, 23) for m in (0, 30)]
    env.availability.set(TOMORROW, times)
    answer = _ex(env).run("get_availability", {"date": TOMORROW, "partySize": 2, "time": "19.15"})
    assert len(answer["times"]) == 12 and answer["times"][0] == "11:00" and answer["times"][-1] == "22:30"
    assert answer["requestedTime"] == "19:00" and answer["suggest"] == ["18:30", "19:00", "19:30"]


def test_create_needs_the_confirmation_and_books_under_the_caller_id(env):
    env.availability.set(TOMORROW, ["19:00"])
    ex = _ex(env)
    hold = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 2})["holdId"]
    assert ex.run("create_reservation", {"holdId": hold, "name": "Anna"})["code"] == "notConfirmed"
    assert ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": "yes"})["code"] == \
        "notConfirmed"
    assert not env.invoke.reservations
    done = ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": True,
                                         "phone": "+46709998877"})
    assert done["booked"] is True and done["weekday"] == "Tuesday"
    assert env.invoke.reservations["r1"]["phone"] == CALLER      # never a number the caller merely says
    again = ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": True})
    assert again["reservationId"] == "r1" and len(env.invoke.reservations) == 1


def test_a_hidden_caller_id_books_under_the_confirmed_spoken_number(env):
    env.availability.set(TOMORROW, ["19:00"])
    ex = _ex(env, caller="anonymous")
    hold = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 2})["holdId"]
    bad = ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": True, "phone": "12"})
    assert bad["code"] == "phoneInvalid"
    ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": True, "phone": "070 999 88 77"})
    assert env.invoke.reservations["r1"]["phone"] == "+46709998877"


def test_a_400_from_the_booking_route_is_never_called_a_taken_time(env, monkeypatch):
    env.availability.set(TOMORROW, ["19:00"])
    ex = _ex(env)
    hold = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 2})["holdId"]
    real = env.invoke.__call__

    def refuse(fn, event):
        if fn == "create-pending-reservation":
            env.invoke.calls.append((fn, event["routeKey"], event))
            return 400, {"error": "name must be at most 80 characters"}
        return real(fn, event)

    monkeypatch.setattr(type(env.invoke), "__call__", lambda self, fn, event: refuse(fn, event))
    out = ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": True})
    assert out["code"] == "nameInvalid" and "held" not in out


def test_move_checks_the_new_time_and_offers_others(env):
    env.invoke.listed[TOMORROW] = [{"reservationId": "r9", "customerPhone": CALLER, "status": "reserved",
                                    "date": TOMORROW, "startTime": "19:00", "partySize": 4}]
    env.availability.set("2026-11-04", ["18:00", "21:00"])
    ex = _ex(env)
    found = ex.run("find_reservations", {})
    assert found["reservations"][0]["weekday"] == "Tuesday"
    assert ex.run("move_reservation", {"reservationId": "r9", "date": "2026-11-04", "time": "20:00"})["code"] == \
        "notConfirmed"
    moved = ex.run("move_reservation", {"reservationId": "r9", "date": "2026-11-04", "time": "21:00",
                                        "confirmed": True})
    assert moved["moved"] is True
    (_, _, event), = [c for c in env.invoke.calls if c[0] == "mark-arrived"]
    assert json.loads(event["body"]) == {"date": "2026-11-04", "startTime": "21:00", "tableIds": ["t4"]}
    assert ex.run("cancel_reservation", {"reservationId": "r9"})["code"] == "notConfirmed"


def test_find_reads_only_the_callers_bookings_in_one_bounded_query(env):
    env.invoke.listed["2026-11-20"] = [{"reservationId": "r7", "customerPhone": CALLER, "status": "reserved",
                                        "date": "2026-11-20", "startTime": "18:00", "partySize": 2},
                                       {"reservationId": "r8", "customerPhone": "+46709999999", "status": "reserved",
                                        "date": "2026-11-20", "startTime": "19:00", "partySize": 2}]
    found = _ex(env).run("find_reservations", {})
    assert [r["reservationId"] for r in found["reservations"]] == ["r7"]
    (query,) = [c[2]["queryStringParameters"] for c in env.invoke.calls if c[0] == "get-reservation"]
    assert query == {"from": "2026-11-02", "to": "2026-11-23", "phone": CALLER}


def test_find_falls_back_to_the_day_reads_while_get_reservation_is_older(env):
    env.invoke.range_query_missing = True
    env.invoke.listed["2026-11-20"] = [{"reservationId": "r7", "customerPhone": CALLER, "status": "reserved",
                                        "date": "2026-11-20", "startTime": "18:00", "partySize": 2}]
    found = _ex(env).run("find_reservations", {})
    assert [r["reservationId"] for r in found["reservations"]] == ["r7"]
    days = {c[2]["queryStringParameters"].get("date") for c in env.invoke.calls if c[0] == "get-reservation"}
    assert len(days - {None}) == 22


def test_info_and_menu_are_structured_english(env):
    env.invoke.site = {"locations": [{"locationId": LOC_A, "name": "Bistro A", "address": "Storgatan 1",
                                      "businessHours": {"tue": [{"opensAt": "11:00", "closesAt": "22:00"}],
                                                        "mon": [{"opensAt": "17:00", "closesAt": "01:00"}]},
                                      "specialDays": [{"date": "2026-12-24", "kind": "closed", "note": "Julafton"}],
                                      "maxPartySizeOnline": 8}]}
    ex = _ex(env, location={"maxPartySizeOnline": 8})
    info = ex.run("get_restaurant_info", {})
    assert info["hours"] == [{"weekday": "Monday", "open": "17:00", "close": "01:00"},
                             {"weekday": "Tuesday", "open": "11:00", "close": "22:00"}]
    assert info["hoursNote"] and info["specialDays"][0] == {"date": "2026-12-24", "weekday": "Thursday",
                                                            "kind": "closed", "note": "Julafton"}
    assert info["lastBookableDate"] == "2026-11-23" and info["largestPartyByPhone"] == 8
    menu = ex.run("get_menu", {"query": "lasagne"})
    assert menu == {"dishes": [{"name": "Vegetarisk lasagne", "price": 189, "currency": "SEK"}]}


def test_callback_topics_are_tagged(env):
    calls = []
    ex = _ex(env)
    ex.callbacks = lambda **kw: calls.append(kw) or "cb1"
    ex.run("request_callback", {"topic": "language:ti"})
    ex.run("request_callback", {"topic": "wants a cake"})
    assert [c["topic"] for c in calls] == ["language:ti", "other wants a cake"]


# --- the agent loop ------------------------------------------------------------------------------

class Scripted:
    def __init__(self, turns, delay=0.0):
        self.turns, self.delay, self.calls = list(turns), delay, 0

    def converse(self, system, messages, tools, *, max_tokens, timeout_s=None):
        self.calls += 1
        time.sleep(self.delay)
        item = self.turns.pop(0)
        if isinstance(item, SlowTurn):
            time.sleep(item.delay)
            item = item.turn
        if isinstance(item, Exception):
            raise item
        return item


class SlowTurn:
    """A model answer that arrives after `delay` seconds (Scripted sleeps on it)."""

    def __init__(self, turn, delay):
        self.turn, self.delay = turn, delay


class NoTools:
    def run(self, name, args):
        return {"ok": True}


def test_turn_budget_and_errors_answer_in_the_call_language():
    lookup = Turn(tool_uses=(ToolUse("u1", "get_menu", {}),))
    slow = Scripted([lookup, lookup, Turn(text="[de] Gerne.")], delay=0.05)
    result = agent.run_turn(slow, [], "Hallo", NoTools(), system="s", lang="de", budget_s=0.01)
    assert result.cut and not result.error and result.reply == copy.STILL_CHECKING["de"] and slow.calls == 1
    failing = Scripted([AgentModelError("unavailable", True)])
    assert agent.run_turn(failing, [], "Merhaba", NoTools(), system="s", lang="tr").reply == copy.ERROR["tr"]
    looping = Scripted([lookup] * 6)
    result = agent.run_turn(looping, [], "Hej", NoTools(), system="s", lang="sv")
    assert result.error and looping.calls == agent.MAX_ROUNDS


def test_a_booking_in_the_turn_gets_its_spoken_answer_now_or_in_the_next_turn():
    book = Turn(tool_uses=(ToolUse("u1", "create_reservation", {}),))
    model = Scripted([book, Turn(text="[sv] Klart, ni är bokade.")])
    result = agent.run_turn(model, [], "Ja", NoTools(), system="s", lang="sv")
    assert not result.error and result.reply == "Klart, ni är bokade."
    # The model is slow after the write: the write is kept, the caller hears the sentence, the next turn confirms.
    model = Scripted([book, SlowTurn(Turn(text="[sv] Klart."), 3), Turn(text="[sv] Klart, ni är bokade.")])
    messages = []
    started = time.monotonic()
    result = agent.run_turn(model, messages, "Ja", NoTools(), system="s", lang="sv", budget_s=1.2)
    assert time.monotonic() - started < 1.5
    assert result.cut and result.reply == copy.STILL_CHECKING["sv"] and result.tool_calls == [
        {"name": "create_reservation", "ok": True}]
    assert agent.run_turn(model, messages, "Okej", NoTools(), system="s", lang="sv").reply == "Klart, ni är bokade."


def test_text_beside_a_tool_use_is_spoken():
    bye = Turn(text="[sv] Tack för samtalet, hej då.", tool_uses=(ToolUse("u1", "end_call", {}),))
    result = agent.run_turn(Scripted([bye, Turn(text=None)]), [], "Hej då", NoTools(), system="s", lang="sv")
    assert result.reply == "Tack för samtalet, hej då." and result.language == "sv" and not result.error


def test_an_announcement_before_a_lookup_is_left_out_when_the_answer_follows():
    look = Turn(text="[sv] Ett ögonblick, jag kontrollerar.", tool_uses=(ToolUse("u1", "get_menu", {}),))
    model = Scripted([look, Turn(text="[sv] Det finns lasagne i dag.")])
    assert agent.run_turn(model, [], "Har ni lasagne?", NoTools(), system="s", lang="sv").reply == \
        "Det finns lasagne i dag."
    # the model said the AI sentence itself (a language without a fixed one): kept
    look = Turn(text="[ta] நான் AI உதவியாளர்.", tool_uses=(ToolUse("u1", "get_menu", {}),))
    model = Scripted([look, Turn(text="[ta] லசானியா உள்ளது.")])
    assert agent.run_turn(model, [], "x", NoTools(), system="s", lang="ta", keep_preamble=True).reply == \
        "நான் AI உதவியாளர். லசானியா உள்ளது."


def test_inner_tags_self_corrections_and_dashes_are_never_spoken():
    assert agent.spoken_text("Leider ist morgen ausgebucht.\n[de] Am Mittwoch ist um sieben frei.", "de") == \
        "Leider ist morgen ausgebucht. Am Mittwoch ist um sieben frei."
    said = agent.spoken_text("Свободно в шесть или half past seven?\n\nLet me correct that: the times are ...\n\n"
                             "[ru] Свободно в шесть или в половине восьмого. Какое время вам подходит?", "ru")
    assert said == "Свободно в шесть или в половине восьмого. Какое время вам подходит?"
    assert agent.spoken_text("Номер для связи \u2014 тот, с которого вы звоните.", "ru") == \
        "Номер для связи, тот, с которого вы звоните."
    assert not DASHES.search(agent.spoken_text("**Klart** \u2013 välkommen.", "sv"))
    assert agent.spoken_text("Hej då och välkommen!", "sv") == "Hej då och välkommen."
    assert agent.spoken_text("¡Hasta luego!", "es") == "Hasta luego."
    assert agent.spoken_text("Berri, Talaado, sagaal... Corrected: Berri, Talaado, saddexaad. Midkee?", "so") == \
        "Berri, Talaado, saddexaad. Midkee?"
    # English callers may say "actually"
    assert agent.spoken_text("Actually, seven is fine.", "en") == "Actually, seven is fine."


def test_tool_results_carry_how_to_say_their_times_and_dates():
    from shared.features.phone_agent.tools import clock_words, date_words, read_as
    assert clock_words("19:30") == "half past seven in the evening"
    assert clock_words("18:00") == "six o'clock in the evening"
    assert clock_words("11:45") == "quarter to twelve in the morning"
    assert clock_words("12:00") == "twelve noon"
    assert date_words("2026-11-04") == "Wednesday the fourth of November"
    out = read_as({"date": "2026-11-04", "suggest": ["17:30", "19:30"], "holdId": "p1"})
    assert out["readAs"] == {"2026-11-04": "Wednesday the fourth of November",
                             "17:30": "half past five in the evening", "19:30": "half past seven in the evening"}
    assert read_as({"ok": True}) == {"ok": True}
    seen = []

    class Tools:
        def run(self, name, args):
            return {"times": ["19:00"]}

    class Model(Scripted):
        def converse(self, system, messages, tools, *, max_tokens, timeout_s=None):
            seen.append(messages[-1])
            return super().converse(system, messages, tools, max_tokens=max_tokens)
    model = Model([Turn(tool_uses=(ToolUse("u1", "get_availability", {}),)), Turn(text="[sv] Klockan sju.")])
    agent.run_turn(model, [], "Imorgon?", Tools(), system="s", lang="sv")
    assert "seven o'clock in the evening" in seen[-1]["content"][0]["content"]


def test_old_tool_results_are_shortened_but_keep_ids():
    big = json.dumps({"holdId": "p1", "times": ["18:00"] * 200, "date": TOMORROW})
    messages = []
    for i in range(4):
        messages += [{"role": "user", "content": f"turn {i}"},
                     {"role": "assistant", "content": [{"type": "tool_use", "id": f"u{i}", "name": "x", "input": {}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"u{i}", "content": big}]}]
    agent.compact(messages)
    first = json.loads(messages[2]["content"][0]["content"])
    assert first["holdId"] == "p1" and "times" not in first
    assert json.loads(messages[-1]["content"][0]["content"])["times"]          # the last rounds stay whole


# --- whole calls: switching, unsupported languages, retries ---------------------------------------

def _say(phone, text):
    return phone.say(text)


def test_a_somali_caller_hears_somali_when_the_voice_speaks_it(live, monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_TTS_MODEL", "eleven_v4_turbo")
    seen = []
    live.model.converse = lambda system, messages, tools, *, max_tokens: (
        seen.append(system), Turn(text="[so] Haa, immisa qof?"))[1]
    phone = Phone(live)
    reply = phone.say("Waxaan rabaa miis berri fadlan")
    assert reply == "Waxaan ahay kaaliyaha AI-ga ee makhaayadda. Haa, immisa qof?"
    assert "answer only in Somali" in seen[0] and "Speech recognition is weak in Somali" in seen[0]
    assert phone.row()["language"] == "so"


def test_somali_on_a_voice_that_cannot_speak_it_gets_the_fixed_sentences_then_a_callback(live, monkeypatch):
    monkeypatch.delenv("PHONE_AGENT_TTS_MODEL", raising=False)
    live.model.responses = [Turn(text="[so] Haa, immisa qof?"), Turn(text="[so] Waan ka xumahay.")]
    phone = Phone(live)
    first = phone.say("Waxaan rabaa miis berri fadlan")
    assert first == copy.unsupported_language() and phone.row()["language"] == "sv"
    second = phone.say("Ma garanayo, waxaan rabaa Soomaali")
    assert second == copy.unsupported_language(callback=True)
    _, body = call(live, "GET", L + "/callbacks")
    assert body["callbacks"][0]["topic"] == "language:so"
    messages = agent.loads(phone.row()["agentMessages"])
    assert messages[-1]["content"] == second                           # the history holds what was said


def test_a_language_without_a_fixed_sentence_is_disclosed_by_the_model(live, monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_TTS_MODEL", "eleven_v4_turbo")
    seen = []
    replies = iter([Turn(text="[sw] Habari, mimi ni msaidizi wa AI. Watu wangapi?"), Turn(text="[sw] Sawa.")])
    live.model.converse = lambda system, messages, tools, *, max_tokens: (seen.append(system), next(replies))[1]
    phone = Phone(live)
    phone.say("Habari, nataka kuhifadhi meza kesho jioni tafadhali")
    assert phone.row()["language"] == "sw"
    assert phone.row()["disclosure"]["modelSaid"] == ["sw"] and phone.row()["disclosure"]["languages"] == ["sv", "sw"]
    assert "that you are the AI" not in seen[0]                   # the hint did not know Swahili: next turn
    phone.say("Watu wanne")
    assert "first say in one short sentence, in Swahili" in seen[1]


def test_okay_and_a_name_keep_a_swedish_call_swedish(live):
    live.model.responses = [Turn(text="[en] Okay."), Turn(text="[de] Gut."), Turn(text="[sv] Tack.")]
    phone = Phone(live)
    for text in ("Okay", "Ahmed", "Kungsgatan"):
        phone.say(text)
        assert phone.row()["language"] == "sv"
    assert phone.row()["disclosure"]["languages"] == ["sv"]


def test_a_provider_retry_of_the_same_turn_runs_no_tool_twice(live):
    from test_phone_agent_live import TOOLS as PROVIDER_TOOLS, turn
    live.availability.set(TOMORROW, ["19:00"])
    phone = Phone(live)
    greeting = phone.row()["disclosure"]["text"]
    body = {"booqy": phone.ids, "tools": PROVIDER_TOOLS, "messages": [
        {"role": "assistant", "content": greeting},
        {"role": "user", "content": "Hej, jag vill boka ett bord för fyra imorgon klockan 19"}]}
    first = turn(live, body)
    count = len(live.invoke.calls)
    assert turn(live, body) == first and len(live.invoke.calls) == count
    assert phone.row()["lastTurn"]["key"] and "Hej" not in json.dumps(phone.row()["lastTurn"]["key"])
    phone.hang_up()
    assert "lastTurn" not in phone.row()                          # never kept past the call


def test_lex_line_meters_the_call_when_the_agent_ends_it(live):
    from test_phone_agent_live import lex
    out, attrs = lex(live, "contact-9", "Hej")
    live.model.responses = [
        {"tool_uses": [{"id": "u1", "name": "end_call", "input": {"outcome": "info", "summary": "Hälsade"}}]},
        Turn(text="Tack för samtalet, hej då."),
    ]
    lex(live, "contact-9", "Tack, det var allt", attrs)
    row = live.live.calls.get_call(attrs["tenantId"], attrs["callId"])
    assert row["status"] == "processed" and row["endReason"] == "agent_end"
    out, _ = lex(live, "contact-10", "")
    assert out["messages"][0]["content"]


# --- settings and the agent principal ------------------------------------------------------------

def test_phone_max_party_size_is_validated_and_kept_when_an_older_admin_saves(env):
    assert enable(env, phoneMaxPartySize=12)["phoneMaxPartySize"] == 12
    assert enable(env, version=1)["phoneMaxPartySize"] == 12                 # absent: kept
    assert enable(env, version=2, phoneMaxPartySize=None)["phoneMaxPartySize"] is None
    status, body = call(env, "PUT", L, body={**pa_always(), "version": 3, "phoneMaxPartySize": 51})
    assert status == 400 and body["field"] == "phoneMaxPartySize"


def pa_always():
    from test_phone_agent_support import ALWAYS
    return dict(ALWAYS)


def test_new_configs_default_to_the_formal_tone(env):
    status, body = call(env, "GET", "/tenant/phone-agent")
    assert status == 200 and body["config"]["tone"] == "formal"


def test_agent_principal_may_book_whatever_the_staff_defaults_say():
    row = {"staffAccess": {"defaults": ["statistik"]}}
    assert staff_access.allows(row, "staff_user", tenant.SYSTEM_SUB_PREFIX + LOC_A, ("bokningar", "layout"))
    assert staff_access.AGENT_SUB_PREFIX == tenant.SYSTEM_SUB_PREFIX
    assert not staff_access.allows(row, "staff_user", "human-sub", "bokningar")
    assert staff_access.allows(row, "staff_user", tenant.SYSTEM_SUB_PREFIX + LOC_A, "statistik") is False


# --- review 2026-11-17: a write needs a read-back the caller could have heard -------------------

def _turn(ex, n):
    ex.state["turn"] = n
    ex.turn = n


def test_a_table_checked_in_the_same_utterance_is_never_booked(env):
    env.availability.set(TOMORROW, ["19:00", "20:00"])
    ex = _ex(env, state={"turn": 1})
    hold = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 2})["holdId"]
    same_turn = ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": True})
    assert same_turn["code"] == "readBackFirst" and not env.invoke.reservations
    _turn(ex, 2)
    # "Yes, but at eight": a new table looked up in the yes utterance is read back first.
    changed = ex.run("hold_table", {"date": TOMORROW, "time": "20:00", "partySize": 2})["holdId"]
    assert ex.run("create_reservation", {"holdId": changed, "name": "Anna", "confirmed": True})["code"] == \
        "readBackFirst"
    # The same table re-checked before booking keeps the earlier read-back.
    assert ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 2})["holdId"] == hold
    assert ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": True})["booked"] is True


def test_a_second_booking_the_same_day_in_one_call_is_refused_and_the_first_can_be_moved(env):
    env.availability.set(TOMORROW, ["19:00", "20:00"])
    ex = _ex(env, caller="anonymous", state={"turn": 1})
    first = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 2})["holdId"]
    _turn(ex, 2)
    assert ex.run("create_reservation", {"holdId": first, "name": "Anna", "confirmed": True})["booked"] is True
    # "Oh, we are three": the model holds again for three and tries to book a second table.
    again = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 3})["holdId"]
    _turn(ex, 3)
    second = ex.run("create_reservation", {"holdId": again, "name": "Anna", "confirmed": True})
    assert second["code"] == "alreadyBooked" and second["booking"]["reservationId"] == "r1"
    assert len(env.invoke.reservations) == 1
    # The booking made in this call is the caller's own, also with a hidden number.
    assert "r1" in ex.state["found"] and ex.state["foundParty"]["r1"] == 2


def test_cancel_and_move_need_the_booking_found_in_an_earlier_utterance(env):
    env.invoke.listed[TOMORROW] = [{"reservationId": "r9", "customerPhone": CALLER, "status": "reserved",
                                    "date": TOMORROW, "startTime": "19:00", "partySize": 4}]
    env.availability.set("2026-11-04", ["18:00", "21:00"])
    ex = _ex(env, state={"turn": 1})
    ex.run("find_reservations", {})
    assert ex.run("cancel_reservation", {"reservationId": "r9", "confirmed": True})["code"] == "readBackFirst"
    assert ex.run("move_reservation", {"reservationId": "r9", "date": "2026-11-04", "time": "21:00",
                                       "confirmed": True})["code"] == "readBackFirst"
    assert not [c for c in env.invoke.calls if c[0] == "mark-arrived"]
    _turn(ex, 2)
    assert ex.run("move_reservation", {"reservationId": "r9", "date": "2026-11-04", "time": "21:00",
                                       "confirmed": True})["moved"] is True
    assert ex.run("cancel_reservation", {"reservationId": "r9", "confirmed": True})["cancelled"] is True


def test_the_staff_view_never_offers_a_time_that_already_started_today(env):
    today = TODAY.isoformat()
    env.availability.set(today, [], staff={"12:00": {"t6": 6}, "18:00": {"t6": 6}, "20:00": {"t6": 6}})
    ex = Executor(tenant_id=TENANT_A, tenant_row={"tenantId": TENANT_A},
                  location={"locationId": LOC_A, "maxPartySizeOnline": 4}, loc_config={"phoneMaxPartySize": 8},
                  invoke=env.invoke, state={}, caller_phone=CALLER, today=TODAY, now_time="17:40")
    assert ex.run("get_availability", {"date": today, "partySize": 6})["times"] == ["18:00", "20:00"]
    assert ex.run("hold_table", {"date": today, "time": "12:00", "partySize": 6}).get("holdId") is None


def test_every_session_turn_is_counted_for_the_tools(live):
    live.model.responses = [Turn(text="[sv] Hur många blir ni?"), Turn(text="[sv] Tack.")]
    phone = Phone(live)
    phone.say("Hej, jag vill boka ett bord")
    phone.say("Fyra personer")
    assert json.loads(phone.row()["toolState"])["turn"] == 2


# --- the turn deadline: the caller never waits in silence, the work is kept ----------------------

HOLD = {"date": TOMORROW, "time": "19:00", "partySize": 2}


def _slow_reads(ex, seconds):
    """get-availability answers after `seconds` while slow["on"]."""
    slow, real = {"on": True}, ex.invoke

    def invoke(fn, event):
        if slow["on"] and fn == "get-availability":
            time.sleep(seconds)
        return real(fn, event)
    ex.invoke = invoke
    return slow


def test_a_slow_model_gets_the_still_checking_sentence_within_the_budget_and_the_call_recovers():
    model = Scripted([SlowTurn(Turn(text="[ru] Поздно."), 3), Turn(text="[ru] Да, завтра есть столик.")])
    messages, started = [], time.monotonic()
    result = agent.run_turn(model, messages, "Есть столик на завтра?", NoTools(), system="s", lang="ru",
                            budget_s=0.5)
    assert time.monotonic() - started < 0.9
    assert result.cut and not result.error and result.reply == copy.STILL_CHECKING["ru"] and result.language == "ru"
    assert [m["role"] for m in messages] == ["user", "assistant"]
    result = agent.run_turn(model, messages, "Хорошо", NoTools(), system="s", lang="ru")
    assert result.reply == "Да, завтра есть столик." and not result.cut
    # every fixed-sentence language has the sentence; any other gets the model's own announcement, else English
    assert set(copy.REVIEWED) <= set(copy.STILL_CHECKING)
    look = Turn(text="[ta] ஒரு நிமிடம், பார்க்கிறேன்.", tool_uses=(ToolUse("u1", "get_menu", {}),))
    model = Scripted([look, SlowTurn(Turn(text="[ta] x"), 3)])
    assert agent.run_turn(model, [], "x", NoTools(), system="s", lang="ta", budget_s=1.2).reply == \
        "ஒரு நிமிடம், பார்க்கிறேன்."
    assert agent.run_turn(Scripted([SlowTurn(Turn(text="x"), 3)]), [], "x", NoTools(), system="s", lang="ta",
                          budget_s=0.3).reply == copy.STILL_CHECKING["en"]


def test_a_slow_read_is_cut_its_late_answer_is_dropped_and_the_next_turn_runs_it_again(env):
    env.availability.set(TOMORROW, ["19:00", "20:00"])
    ex = _ex(env, state={"turn": 1})
    slow = _slow_reads(ex, 1.5)
    hold = Turn(text="[sv] Ett ögonblick.", tool_uses=(ToolUse("u1", "hold_table", HOLD),))
    model = Scripted([hold, Turn(tool_uses=(ToolUse("u2", "hold_table", HOLD),)),
                      Turn(tool_uses=(ToolUse("u3", "create_reservation", {"holdId": "p1", "name": "Anna",
                                                                            "confirmed": True}),)),
                      Turn(text="[sv] Ett bord för två i morgon klockan sju, stämmer det?"),
                      Turn(tool_uses=(ToolUse("u4", "create_reservation", {"holdId": "p1", "name": "Anna",
                                                                            "confirmed": True}),)),
                      Turn(text="[sv] Klart, du är bokad.")])
    messages, started = [], time.monotonic()
    result = agent.run_turn(model, messages, "Bord för två i morgon klockan sju", ex, system="s", lang="sv",
                            budget_s=0.5)
    assert time.monotonic() - started < 0.9 and result.cut and result.reply == copy.STILL_CHECKING["sv"]
    late = json.loads(messages[-2]["content"][0]["content"])
    assert late["code"] == "timeout" and messages[-2]["content"][0]["tool_use_id"] == "u1"
    assert ex.state["proposals"] == {} and ex.state["resume"] is True
    time.sleep(1.2)
    assert ex.state["proposals"] == {}        # the read that answered late changed only its fork
    slow["on"] = False
    # The next turn looks the table up again; the booking waits for the read-back the caller hears.
    _turn(ex, 2)
    result = agent.run_turn(model, messages, "Okej", ex, system="s", lang="sv")
    assert result.reply == "Ett bord för två i morgon klockan sju, stämmer det?" and not env.invoke.reservations
    _turn(ex, 3)
    assert agent.run_turn(model, messages, "Ja", ex, system="s", lang="sv").reply == "Klart, du är bokad."
    assert len(env.invoke.reservations) == 1


def test_a_cut_after_a_lookup_never_books_what_the_caller_has_not_heard(env):
    env.availability.set(TOMORROW, ["19:00", "20:00"])
    ex = _ex(env, state={"turn": 1})
    book = Turn(tool_uses=(ToolUse("u2", "create_reservation", {"holdId": "p1", "name": "Anna", "confirmed": True}),))
    model = Scripted([Turn(tool_uses=(ToolUse("u1", "hold_table", HOLD),)), SlowTurn(Turn(text="[sv] x"), 3),
                      book, Turn(text="[sv] Ett bord i morgon klockan sju för två, stämmer det?"),
                      book, Turn(text="[sv] Klart.")])
    messages = []
    result = agent.run_turn(model, messages, "Bord i morgon klockan sju för två", ex, system="s", lang="sv",
                            budget_s=1.2)
    assert result.cut and ex.state["proposals"]["p1"]["turn"] == 2     # looked up where the caller can hear it
    _turn(ex, 2)
    # "Okej" to the still-checking sentence is no yes: the read-back comes first.
    assert agent.run_turn(model, messages, "Okej", ex, system="s", lang="sv").reply.endswith("stämmer det?")
    assert not env.invoke.reservations
    _turn(ex, 3)
    assert agent.run_turn(model, messages, "Ja", ex, system="s", lang="sv").reply == "Klart."
    assert len(env.invoke.reservations) == 1


def test_a_cut_call_turn_is_saved_and_the_next_one_resumes_with_the_note(live, monkeypatch):
    prompts = []

    class Model(Scripted):
        def converse(self, system, messages, tools, *, max_tokens, timeout_s=None):
            prompts.append(system)
            return super().converse(system, messages, tools, max_tokens=max_tokens)
    monkeypatch.setitem(agent.run_turn.__kwdefaults__, "budget_s", 0.4)
    live.live._clients["model"] = Model([SlowTurn(Turn(text="[sv] x"), 2), Turn(text="[sv] Vad roligt.")])
    phone = Phone(live)
    assert phone.say("Hej, jag vill boka ett bord") == copy.STILL_CHECKING["sv"]
    assert json.loads(phone.row()["toolState"])["resume"] is True
    assert phone.say("Okej") == "Vad roligt."
    assert "# Continue" not in prompts[0] and "# Continue" in prompts[1]
    assert "resume" not in json.loads(phone.row()["toolState"])
