"""Live test bugs on dev, 2026-11-20 (Lex recognize-text, Restaurant 1):
the booked time differed from the read-back (three holds in one reply),
the fee rule was written before hold_table and never spoken, Mandarin and
Cantonese did not switch, a Persian "yes" was taken as the callback, and
tool-heavy turns hit the turn cut. Each test reproduces the bug first."""

import json
import re
import time

import pytest

from shared.agent_model import ToolUse, Turn
from shared.features.phone_agent import agent, copy, language, readback, tools
from shared.features.phone_agent.tools import Executor
from test_phone_agent_calls import NUMBER, Phone, live  # noqa: F401 (fixture)
from test_phone_agent_guarantee_link import POLICY, PendingInvoke, Texts
from test_phone_agent_support import CALLER, LOC_A, TENANT_A, TODAY, env  # noqa: F401 (fixture)
from test_phone_agent_v2 import Scripted

assert live and env
TOMORROW = "2026-11-03"          # a Tuesday
DASHES = re.compile("[\u2013\u2014]|&[nm]dash;")


def _ex(e, *, location=None, language_="sv", sms=None, invoke=None):
    return Executor(tenant_id=TENANT_A, tenant_row={"tenantId": TENANT_A, "name": "Bistro"},
                    location={"locationId": LOC_A, "name": "Bistro", **(location or {})}, loc_config={},
                    invoke=invoke or e.invoke, state={"turn": 1}, caller_phone=CALLER, today=TODAY,
                    send_sms=sms, language=language_)


def _next(ex):
    ex.state["turn"] = ex.turn = int(ex.turn) + 1


def _hold(i, at, party=4):
    return ToolUse(f"h{i}", "hold_table", {"date": TOMORROW, "time": at, "partySize": party})


def _create(i, hold_id, name="Anna"):
    return ToolUse(f"c{i}", "create_reservation", {"holdId": hold_id, "name": name, "confirmed": True})


def _result(messages, use_id):
    for m in messages:
        for b in m.get("content") if isinstance(m.get("content"), list) else []:
            if b.get("type") == "tool_result" and b.get("tool_use_id") == use_id:
                return json.loads(b["content"])
    raise AssertionError(use_id)


def _booked(e):
    return [r["startTime"] for r in e.invoke.reservations.values()]


# --- BUG 1: the confirmed booking is what was read back --------------------------------------------

def test_scenario_a_three_holds_read_back_halv_sju_and_yes_books_1830_never_1930(env):
    """The exact live transcript: offers 19:00, 19:30, 20:00; "Halv sju går bra"; the model held 19:00,
    19:30 and 18:30 in one turn, read back "halv sju" and booked holdId p2 (19:30)."""
    env.availability.set(TOMORROW, ["18:30", "19:00", "19:30", "20:00"])
    ex = _ex(env)
    model = Scripted([
        Turn(tool_uses=(ToolUse("a1", "get_availability", {"date": TOMORROW, "partySize": 4, "time": "19:00"}),)),
        Turn(text="[sv] Jag kan erbjuda sju, halv åtta eller åtta på kvällen. Vilken tid passar?"),
        Turn(tool_uses=(_hold(1, "19:00"), _hold(2, "19:30"), _hold(3, "18:30"))),
        Turn(text="[sv] Ett bord för fyra på tisdag klockan halv sju på kvällen, i namnet Anna. Stämmer det?"),
        Turn(tool_uses=(_create(1, "p2"),)),
        Turn(tool_uses=(_create(2, "p3"),)),
        Turn(text="[sv] Klart, ni är bokade."),
    ])
    messages = []
    agent.run_turn(model, messages, "Hej, ett bord för fyra i morgon vid sju, jag heter Anna", ex, system="s")
    _next(ex)
    agent.run_turn(model, messages, "Halv sju går bra", ex, system="s")
    assert [k for k, p in ex.state["proposals"].items() if not p.get("released")] == ["p3"]   # one live hold
    assert ex.state["readBack"] == {"holdId": "p3", "turn": 2}
    _next(ex)
    result = agent.run_turn(model, messages, "Ja", ex, system="s")
    refused = _result(messages, "c1")
    assert refused["ok"] is False and refused["code"] == "holdReplaced"
    assert refused["live"]["holdId"] == "p3" and refused["live"]["time"] == "18:30"
    assert result.reply == "Klart, ni är bokade."
    assert _booked(env) == ["18:30"]


def test_scenario_a_reordered_the_live_hold_is_not_what_was_read_back_so_nothing_is_booked(env):
    env.availability.set(TOMORROW, ["18:30", "19:00", "19:30", "20:00"])
    ex = _ex(env)
    model = Scripted([
        Turn(tool_uses=(_hold(1, "18:30"), _hold(2, "19:00"), _hold(3, "19:30"))),
        Turn(text="[sv] Ett bord för fyra på tisdag klockan halv sju på kvällen, i namnet Anna. Stämmer det?"),
        Turn(tool_uses=(_create(1, "p1"),)),
        Turn(tool_uses=(_create(2, "p3"),)),
        Turn(text="[sv] Förlåt, jag läser upp det igen."),
    ])
    messages = []
    agent.run_turn(model, messages, "Halv sju går bra, Anna", ex, system="s")
    assert "readBack" not in ex.state          # "halv sju" (18:30) is not the live hold (19:30)
    _next(ex)
    agent.run_turn(model, messages, "Ja", ex, system="s")
    assert _result(messages, "c1")["code"] == "holdReplaced"
    mismatch = _result(messages, "c2")
    assert mismatch["code"] == "readBackMismatch" and mismatch["hold"]["time"] == "19:30"
    assert _booked(env) == []


@pytest.mark.parametrize("lang, said", [
    ("sv", "Ett bord för fyra på tisdag, halv sju på kvällen. Stämmer det?"),
    ("no", "Et bord for fire på tirsdag, halv syv om kvelden. Stemmer det?"),
    ("da", "Et bord til fire på tirsdag klokken halv syv om aftenen. Passer det?"),
    ("de", "Ein Tisch für vier am Dienstag um halb sieben am Abend. Stimmt das?"),
    ("nl", "Een tafel voor vier op dinsdag om half zeven in de avond. Klopt dat?"),
    ("en", "A table for four on Tuesday at half past six in the evening. Is that correct?"),
    ("zh", "星期二晚上六点半，四位。对吗？"),
])
@pytest.mark.parametrize("held, books", [("18:30", True), ("19:30", False)])
def test_the_half_hour_read_back_books_only_the_hold_it_names(env, lang, said, held, books):
    env.availability.set(TOMORROW, ["18:30", "19:30"])
    ex = _ex(env, language_=lang)
    model = Scripted([Turn(tool_uses=(_hold(1, held),)), Turn(text=f"[{lang}] {said}"),
                      Turn(tool_uses=(_create(1, "p1"),)), Turn(text=f"[{lang}] ..")])
    messages = []
    agent.run_turn(model, messages, "x", ex, system="s", lang=lang)
    _next(ex)
    agent.run_turn(model, messages, "ja", ex, system="s", lang=lang)
    assert _booked(env) == (["18:30"] if books else [])
    if not books:
        assert _result(messages, "c1")["code"] == "readBackMismatch"


def test_half_hour_conventions_are_read_as_the_caller_means_them():
    assert readback.clock_times("halv sju", "sv") == [(6, 30)]
    assert readback.clock_times("halb sieben", "de") == [(6, 30)]
    assert readback.clock_times("half zeven", "nl") == [(6, 30)]
    assert readback.clock_times("halv syv", "no") == readback.clock_times("halv syv", "da") == [(6, 30)]
    assert readback.clock_times("half past six", "en") == [(6, 30)]
    assert readback.clock_times("half seven", "en") == [(7, 30)]                  # British English
    assert readback.clock_times("kvart i åtta", "sv") == [(7, 45)]
    assert readback.clock_times("at seven thirty in the evening", "en") == [(7, 30)]   # never also "at seven"
    assert readback.clock_times("neunzehn Uhr dreißig", "de") == [(19, 30)]
    assert readback.clock_times("klockan 18.30", "sv") == [(18, 30)]
    assert readback.clock_times("晚上六點半", "yue") == [(6, 30)]
    assert readback.clock_times("senare än tjugofyra timmar före besöket", "sv") == []   # the cutoff is no time
    assert readback.same_clock((6, 30), "18:30") and not readback.same_clock((6, 30), "19:30")
    prompt = agent.system_prompt("sv", "Sara", "Bistro", TODAY, "formal")
    for words in ('"halv sju"', '"halv syv"', '"halb sieben"', '"half zeven"'):
        assert words in prompt
    assert "18:30, half past six, never 19:30" in prompt and "never hold several times" in prompt
    assert not DASHES.search(prompt) and "{" not in prompt


def test_a_new_hold_replaces_the_previous_one_and_says_so(env):
    env.availability.set(TOMORROW, ["18:30", "19:00"])
    ex = _ex(env)
    ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 2})
    second = ex.run("hold_table", {"date": TOMORROW, "time": "18:30", "partySize": 2})
    assert second["holdId"] == "p2" and second["replaced"] == ["p1"] and "only p2 can be booked" in second["note"]
    assert ex.state["proposals"]["p1"]["released"] is True
    by = {t["name"]: t["description"] for t in tools.TOOLS}
    assert "a new hold replaces the previous one" in by["hold_table"]


def test_a_reply_that_names_another_time_cancels_the_read_back(env):
    env.availability.set(TOMORROW, ["18:30", "19:00"])
    ex = _ex(env)
    ex.run("hold_table", {"date": TOMORROW, "time": "18:30", "partySize": 2})
    ex.heard("Ett bord på tisdag klockan halv sju. Stämmer det?", "sv")
    assert ex.state["readBack"]["holdId"] == "p1"
    _next(ex)
    ex.heard("Eller vill du hellre ha klockan sju?", "sv")
    assert "readBack" not in ex.state
    _next(ex)
    assert ex.run("create_reservation", {"holdId": "p1", "name": "Anna", "confirmed": True})["code"] == \
        "readBackMismatch"


# --- BUG 2: the fee rule must be spoken before the booking -----------------------------------------

def _guarantee_ex(e, language_="en"):
    texts = Texts()
    ex = _ex(e, location={"stripeAccountId": "acct_bistro", "guarantee": dict(POLICY)}, language_=language_,
             sms=texts, invoke=PendingInvoke(e.invoke))
    return ex, texts


def test_scenario_b_the_fee_rule_written_before_hold_table_is_spoken(env):
    env.availability.set(TOMORROW, ["19:00"])
    ex, _ = _guarantee_ex(env)
    terms = ("That booking needs a card as a guarantee, but nothing is charged now. The restaurant charges a fee "
             "if the booking is cancelled later than twenty-four hours before the visit, or if you do not come. "
             "Would you like more information about that?")
    model = Scripted([Turn(text=f"[en] {terms}", tool_uses=(_hold(1, "19:00"),)),
                      Turn(text="[en] Sure.")])
    result = agent.run_turn(model, [], "Seven is fine, four of us", ex, system="s", lang="en")
    assert terms in result.reply                       # was dropped as a preamble: the caller never heard it
    assert ex.state["proposals"]["p1"]["guaranteeSaid"] == 1


def test_scenario_b_create_refuses_until_the_fee_rule_was_spoken(env):
    env.availability.set(TOMORROW, ["19:00"])
    ex, texts = _guarantee_ex(env)
    read_back = ("[en] A table for four on Tuesday at seven in the evening, under Anna. A text message with a "
                 "link to add a card will be sent to this number. Is that correct?")
    terms = ex.run("get_availability", {"date": TOMORROW, "partySize": 4})["guaranteeTerms"]
    model = Scripted([
        Turn(tool_uses=(_hold(1, "19:00"),)), Turn(text=read_back),                       # no terms said
        Turn(tool_uses=(_create(1, "p1"),)), Turn(text=f"[en] {terms['sentence']} {terms['question']}"),
        Turn(text=read_back),
        Turn(tool_uses=(_create(2, "p1"),)), Turn(text="[en] " + copy.GUARANTEE_LINK_SENT["en"]),
    ])
    messages = []
    agent.run_turn(model, messages, "Seven, four of us, Anna", ex, system="s", lang="en")
    _next(ex)
    agent.run_turn(model, messages, "Yes", ex, system="s", lang="en")
    refused = _result(messages, "c1")
    assert refused["code"] == "termsFirst" and refused["guaranteeTerms"]["sentence"] == terms["sentence"]
    assert _booked(env) == [] and texts.sent == []
    assert ex.state["proposals"]["p1"]["guaranteeSaid"] == 2
    _next(ex)
    agent.run_turn(model, messages, "No thanks", ex, system="s", lang="en")          # the read-back again
    _next(ex)
    agent.run_turn(model, messages, "Yes", ex, system="s", lang="en")
    assert _result(messages, "c2")["guaranteeLinkSent"] is True and len(texts.sent) == 1


def test_a_read_back_before_the_terms_needs_reading_again(env):
    env.availability.set(TOMORROW, ["19:00"])
    ex, texts = _guarantee_ex(env, language_="sv")
    ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 4})
    ex.heard("Ett bord för fyra på tisdag klockan sju på kvällen. Stämmer det?", "sv")
    _next(ex)
    ex.heard("Restaurangen tar ut en avgift om ni inte kommer. Vill du ha mer information om det?", "sv")
    _next(ex)
    assert ex.run("create_reservation", {"holdId": "p1", "name": "Anna", "confirmed": True})["code"] == \
        "readBackMismatch" and texts.sent == []


# --- BUG 3 and 4: Mandarin and Cantonese -----------------------------------------------------------

AWS = language.AWS_CALL_LANGUAGES


def test_a_clear_script_is_strong_evidence_in_few_words():
    assert language.script_strong("我想订一张桌子") and language.short("我想订一张桌子") is False
    assert language.pre_switch("sv", "zh", "我想订一张桌子", allowed=AWS) == "zh"
    assert language.decide("sv", "zh", "zh", "我想订一张桌子", allowed=AWS) == ("zh", None)
    assert language.decide("sv", "zh", "zh", "我想说中文", allowed=AWS) == ("zh", None)
    assert language.requested_languages("我想说中文") == {"zh"}
    assert language.requested_languages("可唔可以講廣東話") == {"yue"}
    assert language.decide("sv", "ru", "ru", "Здравствуйте", allowed=AWS) == ("sv", None)   # one greeting: no
    assert language.decide("sv", "ru", "ru", "Здравствуйте, столик", allowed=AWS) == ("ru", None)
    assert language.decide("sv", "ko", "ko", "예약하고 싶어요 내일", allowed=AWS) == ("ko", None)
    assert language.short("你好")         # two characters: not enough


def test_cantonese_markers_and_traditional_characters_pick_cantonese_on_the_line():
    assert language.hint("你好，我係想訂位，今晚七點，四位", AWS) == "yue"
    assert language.hint("我想訂位，今晚七點，四個人", AWS) == "yue"          # Traditional only: Hong Kong
    assert language.hint("我想订位，今晚七点，四个人", AWS) == "zh"           # Simplified: Mandarin
    flash = language.FLASH_LANGUAGES                                         # no Cantonese voice: Mandarin
    assert language.pre_switch("sv", language.hint("我想訂位，今晚七點，四個人"), "我想訂位，今晚七點，四個人",
                               allowed=flash) == "zh"
    assert language.decide("sv", "yue", "zh", "你好，我係想訂位", allowed=AWS) == ("yue", None)


def _lex(e, session, text, attrs=None, locale="sv_SE"):
    event = {"sessionId": session, "inputTranscript": text, "invocationSource": "DialogCodeHook",
             "bot": {"localeId": locale}, "sessionState": {
                 "intent": {"name": "FallbackIntent", "state": "InProgress"},
                 "sessionAttributes": attrs or {"callerNumber": CALLER, "dialedNumber": NUMBER}}}
    out = e.live.handler({"lexV2": event})
    return out, out["sessionState"]["sessionAttributes"]


def _call_row(e, attrs):
    return e.live.calls.get_call(attrs["tenantId"], attrs["callId"])


def test_the_first_chinese_utterance_switches_the_connect_line_to_mandarin(live):
    seen = []
    live.model.converse = lambda system, messages, tools, **kw: (seen.append(system), Turn(text="[zh] 好的，几位？"))[1]
    out, attrs = _lex(live, "contact-zh", "Hej")
    out, attrs = _lex(live, "contact-zh", "我想订一张桌子", attrs)
    assert "answer only in Chinese" in seen[-1]             # the prompt asked for Mandarin before the model ran
    assert attrs["next"] == "switch" and attrs["lang"] == "zh" and attrs["voice"] == "Zhiyu"


def test_asking_for_chinese_switches_at_once(live):
    out, attrs = _lex(live, "contact-zh2", "Hej")
    live.model.responses = [Turn(text="[zh] 好的，我们说中文。请问有什么可以帮您？")]
    out, attrs = _lex(live, "contact-zh2", "我想说中文", attrs)
    assert attrs["lang"] == "zh" and _call_row(live, attrs)["language"] == "zh"


def test_cantonese_in_traditional_characters_switches_to_cantonese(live):
    out, attrs = _lex(live, "contact-yue", "Hej")
    live.model.responses = [Turn(text="[zh] 好呀，幾多位？")]
    out, attrs = _lex(live, "contact-yue", "你好，我係想訂位，今晚七點", attrs)
    assert attrs["lang"] == "yue" and attrs["voice"] == "Hiujin"


# --- BUG 5: a language the line cannot speak, one question at a time ----------------------------------

PERSIAN = "سلام، می‌خواهم برای فردا یک میز رزرو کنم"


def test_persian_yes_continues_in_english_with_the_english_voice(live):
    out, attrs = _lex(live, "contact-fa1", "Hej")
    live.model.responses = [Turn(text="[fa] سلام، برای چند نفر؟")]
    out, attrs = _lex(live, "contact-fa1", PERSIAN, attrs)
    assert attrs["next"] == "switch" and attrs["lang"] == "en" and attrs["voice"] == "Amy"
    assert copy.UNSUPPORTED_LANGUAGE["en"] in attrs["say"] and "call you back" not in attrs["say"]
    attrs = {k: v for k, v in attrs.items() if k not in ("next", "say")}
    out, attrs = _lex(live, "contact-fa1", "بله", attrs, locale="en_GB")   # no model turn for the answer
    said = out["messages"][0]["content"]
    assert copy.UNSUPPORTED_CONTINUE["en"] in said and "next" not in attrs
    row = _call_row(live, attrs)
    assert row["language"] == "en" and "callbackId" not in json.loads(row["toolState"])
    assert "unsupported" not in json.loads(row["toolState"])


def test_persian_no_then_yes_gets_the_callback_confirmed_in_english(live):
    out, attrs = _lex(live, "contact-fa2", "Hej")
    live.model.responses = [Turn(text="[fa] سلام، برای چند نفر؟")]
    out, attrs = _lex(live, "contact-fa2", PERSIAN, attrs)
    attrs = {k: v for k, v in attrs.items() if k not in ("next", "say")}
    out, attrs = _lex(live, "contact-fa2", "نه", attrs, locale="en_GB")
    asked = out["messages"][0]["content"]
    assert copy.UNSUPPORTED_ASK_CALLBACK["en"] in asked and "next" not in attrs
    out, attrs = _lex(live, "contact-fa2", "بله", attrs, locale="en_GB")
    said = out["messages"][0]["content"]
    assert copy.UNSUPPORTED_CALLBACK["en"] in said and attrs["next"] == "end"
    state = json.loads(_call_row(live, attrs)["toolState"])
    assert state["outcome"] == "callback"


def test_unsupported_sentences_ask_one_question_each_without_dashes():
    for sentences in (copy.UNSUPPORTED_LANGUAGE, copy.UNSUPPORTED_ASK_CALLBACK):
        for text in sentences.values():
            assert len(re.findall(r"[?؟]", text)) == 1 and not DASHES.search(text)
    assert set(copy.UNSUPPORTED_LANGUAGE) == set(copy.UNSUPPORTED_ASK_CALLBACK)
    assert language.yes_no("بله") == "yes" and language.yes_no("نه") == "no" and language.yes_no("係") == "yes"
    assert language.yes_no("唔該") is None and language.yes_no("I would like a table for four") is None


# --- BUG 6: fewer, faster rounds ----------------------------------------------------------------------

def test_several_reads_in_one_round_run_their_lookups_in_parallel(env):
    env.availability.set(TOMORROW, ["19:00"])
    env.availability.set("2026-11-04", ["19:00"])
    real = env.invoke

    def slow(fn, event):
        if fn == "get-availability":
            time.sleep(0.3)
        return real(fn, event)
    ex = _ex(env, invoke=slow)
    days = (ToolUse("a1", "get_availability", {"date": TOMORROW, "partySize": 2}),
            ToolUse("a2", "get_availability", {"date": "2026-11-04", "partySize": 2}))
    model = Scripted([Turn(tool_uses=days), Turn(text="[sv] Båda dagarna har sju på kvällen.")])
    messages = []
    result = agent.run_turn(model, messages, "Tisdag eller onsdag?", ex, system="s", budget_s=8)
    assert not result.cut and result.tool_ms < 900          # four lookups of 0.3 s: 1.2 s one after another
    assert _result(messages, "a1")["times"] == ["19:00"] and _result(messages, "a2")["times"] == ["19:00"]


def test_after_the_still_checking_sentence_the_next_turn_reads_back_and_books(env):
    from test_phone_agent_v2 import SlowTurn
    env.availability.set(TOMORROW, ["19:00"])
    ex = _ex(env)
    model = Scripted([Turn(tool_uses=(_hold(1, "19:00", 2),)), SlowTurn(Turn(text="[sv] x"), 2),
                      Turn(text="[sv] Ett bord för två på tisdag klockan sju på kvällen. Stämmer det?"),
                      Turn(tool_uses=(_create(1, "p1"),)), Turn(text="[sv] Klart.")])
    messages = []
    cut = agent.run_turn(model, messages, "Två personer klockan sju", ex, system="s", budget_s=1.5)
    assert cut.cut and "readBack" not in ex.state
    _next(ex)
    assert agent.run_turn(model, messages, "Okej", ex, system="s").reply.endswith("Stämmer det?")
    _next(ex)
    assert agent.run_turn(model, messages, "Ja", ex, system="s").reply == "Klart."
    assert _booked(env) == ["19:00"]
