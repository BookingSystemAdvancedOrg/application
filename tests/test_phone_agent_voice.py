"""Voice naturalness and multilingual switching on the phone line (owner
2026-11-20, voice ASSESSMENT stage 1 and MULTILINGUAL stage 1): the
hesitation guard, varied fixed lines, the switch rules (strong evidence, the
same signal twice, explicit requests, never on short utterances or names,
dwell after a switch, at most three), the read-back again after a switch,
unsupported languages answered in their own language, the per-turn latency
log line, Bedrock prompt caching, and that the booking tools and their guards
are unchanged."""

import json
import re

import pytest

from shared import agent_model_real
from shared.agent_model import Turn
from shared.features.phone_agent import agent, copy, hesitation, language, session, tools
from test_phone_agent_calls import Phone, live  # noqa: F401 (fixture)
from test_phone_agent_languages import flow, lex
from test_phone_agent_live import TOMORROW
from test_phone_agent_support import env  # noqa: F401 (fixture)

DASHES = re.compile("[\u2013\u2014]|&[nm]dash;")


def record(e, replies):
    """The model answers `replies` in order; returns what it saw per round (system prompt, last message)."""
    seen, queue = [], list(replies)

    def converse(system, messages, tools_, *, max_tokens, timeout_s=None):
        seen.append({"system": system, "last": messages[-1]})
        return e.model._canned(queue.pop(0))
    e.model.converse = converse
    return seen


def state_of(phone):
    return json.loads(phone.row().get("toolState") or "{}")


def turn_lines(out):
    return [json.loads(x) for x in out.splitlines() if x.startswith("{") and '"phoneTurn"' in x]


# --- the hesitation guard ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, langs", [
    ("Jag skulle vilja boka bord för, ehm", ("sv",)),
    ("Jag skulle vilja boka ett bord för", ("sv",)),
    ("Vi är fyra och", ("sv",)),
    ("Kan vi komma klockan", ("sv",)),
    ("Hej, öh", ("sv",)),
    ("I would like a table for", ("en",)),
    ("I'd like to book a table, um", ("en",)),
    ("Ich möchte einen Tisch für", ("de",)),
    ("Je voudrais réserver pour", ("fr",)),
    ("Quisiera una mesa para", ("es",)),
    ("Vorrei prenotare un tavolo per", ("it",)),
    ("Chciałbym zarezerwować stolik dla", ("pl",)),
    ("Želim rezervisati sto za", ("bs",)),
    ("Saya mau pesan meja untuk", ("id",)),
    ("أريد حجز طاولة في", ("ar",)),
    ("می‌خواهم یک میز برای", ("fa",)),
    ("Bir masa istiyorum, şey", ("tr",)),
    ("Jag vill boka...", ("sv",)),
])
def test_an_unfinished_utterance_is_held(text, langs):
    assert hesitation.unfinished(text, langs)


@pytest.mark.parametrize("text, langs", [
    ("Jag vill boka ett bord för fyra imorgon klockan 19", ("sv",)),
    ("Vi är fyra personer", ("sv",)),
    ("Jag vill lägga till", ("sv",)),          # a verb particle ends the sentence
    ("Kan jag boka om", ("sv",)),
    ("Who am I speaking with", ("en",)),      # a question may end on a preposition
    ("Vad har ni för", ("sv",)),
    ("Mm", ("sv",)),                          # an answer, not a pause
    ("Ja", ("sv",)),
    ("Dört kişi için", ("tr",)),              # Turkish postpositions end a phrase
    ("Ich rufe morgen an", ("de",)),           # German separable verbs
    ("Har ni öppet?", ("sv",)),
    ("Det var det jag tänkte på", ("sv",)),    # Swedish strands prepositions in finished sentences
    ("Det är bokningen jag ringde om", ("sv",)),
    ("Och vem pratar jag med", ("sv",)),
    ("Yes, that's exactly what I'm looking for", ("en",)),
    ("Yes, I'd like to", ("en",)),
    ("", ("sv",)),
])
def test_a_finished_utterance_is_not_held(text, langs):
    assert not hesitation.unfinished(text, langs)


def test_backchannels_are_short_and_varied_in_every_line_language():
    for lang in copy.LINE_LANGUAGES:
        options = hesitation.BACKCHANNEL[lang]
        assert len(set(options)) == 2, lang
        for said in options:
            assert len(said) <= 8 and "!" not in said and not DASHES.search(said), (lang, said)
    assert hesitation.backchannel("sv", 0) == "Mm?" and hesitation.backchannel("sv", 1) == "Ja?"
    assert hesitation.backchannel("xx", 0) == hesitation.BACKCHANNEL["en"][0]


def test_a_held_fragment_is_joined_with_the_next_utterance_and_never_reaches_the_model_alone(live, capsys):
    seen = record(live, [Turn(text="[sv] Absolut, vilken dag gäller det?")])
    phone = Phone(live)
    assert phone.say("Jag skulle vilja boka bord för, ehm") in ("Mm?", "Ja?")
    assert seen == [] and phone.row()["heldFragment"]["count"] == 1
    assert [t for t in phone.turns() if t["role"] == "caller"] == []        # no transcript turn for a pause
    assert phone.say("fyra personer") == "Absolut, vilken dag gäller det?"
    assert seen[0]["last"]["content"] == "Jag skulle vilja boka bord för, ehm fyra personer"
    assert phone.row()["heldFragment"] is None
    caller = [t["text"] for t in phone.turns() if t["role"] == "caller"]
    assert caller == ["Jag skulle vilja boka bord för, ehm fyra personer"]
    assert state_of(phone)["turn"] == 1                                       # the held turn was no agent turn
    lines = turn_lines(capsys.readouterr().out)
    assert lines[-1]["held"] == 1


def test_at_most_two_fragments_are_held_in_a_row(live):
    record(live, [Turn(text="[sv] Förlåt, vad sa du?")])
    phone = Phone(live)
    assert phone.say("Jag vill boka för") == "Mm?"
    assert phone.say("och") == "Ja?"
    assert phone.say("till") == "Förlåt, vad sa du?"                        # the third goes to the agent


def test_the_connect_line_answers_a_hesitation_with_a_backchannel(live):
    out, attrs = lex(live, "contact-h1", "Hej")
    out, attrs = lex(live, "contact-h1", "Jag skulle vilja boka bord för, öhm", attrs)
    assert out["sessionState"]["dialogAction"] == {"type": "ElicitIntent"}
    assert out["messages"][0]["content"] in ('<speak><prosody rate="100%">Mm?</prosody></speak>',
                                             '<speak><prosody rate="100%">Ja?</prosody></speak>')


# --- the switch rules ----------------------------------------------------------------------------------

AWS = language.AWS_CALL_LANGUAGES


def test_english_after_the_swedish_greeting_switches_and_the_prompt_already_asks_english(live, capsys):
    seen = record(live, [Turn(text="[en] Of course. For how many people?")])
    phone = Phone(live)
    assert phone.say("Hi, I would like to book a table for tomorrow evening").endswith(
        "Of course. For how many people?")
    assert "answer only in English" in seen[0]["system"]                     # the hint was strong evidence
    assert "switched to English" in seen[0]["system"]
    assert phone.row()["language"] == "en"
    line = turn_lines(capsys.readouterr().out)[-1]
    assert (line["lang"], line["from"], line["switched"], line["reason"]) == ("en", "sv", True, "strong")


def test_arabic_persian_and_urdu_are_told_apart_by_letters_and_words():
    assert language.hint("مرحبا، أريد حجز طاولة لأربعة أشخاص غداً") == "ar"
    assert language.hint("سلام، می‌خواهم برای فردا شب یک میز رزرو کنم") == "fa"
    assert language.hint("سلام، فردا میز برای چهار نفر لطفاً") == "fa"          # no Persian-only letter: the words
    assert language.hint("هل يوجد طاولة الليلة من فضلك") == "ar"
    assert language.hint("السلام علیکم، مجھے کل کے لیے ایک میز چاہیے") == "ur"
    assert language.hint("Želim rezervisati sto za sutra, molim") == "bs"
    assert language.hint("Saya mau pesan meja untuk besok malam") == "id"


def test_a_persian_caller_on_the_connect_line_hears_that_the_line_cannot_speak_persian(live):
    out, attrs = lex(live, "contact-fa", "Hej")
    live.model.responses = [Turn(text="[fa] سلام، برای چند نفر؟")]
    out, attrs = lex(live, "contact-fa", "سلام، می‌خواهم برای فردا یک میز رزرو کنم", attrs)
    # live test 2026-11-20: the call switches to English first, so the English voice (not Elin) says it
    assert out["messages"] == [] and attrs["next"] == "switch" and attrs["lang"] == "en" and attrs["voice"] == "Amy"
    said = attrs["say"]
    assert copy.UNSUPPORTED_LANGUAGE["en"] in said                             # Amy cannot read Persian script
    assert "سلام" not in said
    row = live.live.calls.get_call(attrs["tenantId"], attrs["callId"])
    assert json.loads(row["toolState"])["unsupported"] == "fa"


def test_a_bosnian_caller_hears_bosnian_first_then_english_then_a_callback(live):
    out, attrs = lex(live, "contact-bs", "Hej")
    live.model.responses = [Turn(text="[bs] Dobar dan, za koliko osoba?"), Turn(text="[bs] Razumijem.")]
    out, attrs = lex(live, "contact-bs", "Dobar dan, želim rezervisati sto za sutra", attrs)
    assert attrs["next"] == "switch" and attrs["lang"] == "en"           # the English voice reads both
    said = attrs["say"]
    attrs = {k: v for k, v in attrs.items() if k not in ("next", "say")}
    assert copy.UNSUPPORTED_LANGUAGE["bs"] in said and copy.UNSUPPORTED_LANGUAGE["en"] in said
    assert said.index(copy.UNSUPPORTED_LANGUAGE["bs"]) < said.index(copy.UNSUPPORTED_LANGUAGE["en"])
    out, attrs = lex(live, "contact-bs", "Ne znam švedski, molim vas na bosanskom", attrs)
    assert copy.UNSUPPORTED_CALLBACK["bs"] in out["messages"][0]["content"] and attrs["next"] == "end"


def test_an_uncertain_short_utterance_is_asked_about_and_a_yes_switches(live):
    seen = record(live, [Turn(text="[en] Sure, for how many?"), Turn(text="[en] Great, for how many people?")])
    phone = Phone(live)
    reply = phone.say("Okay sure")
    assert reply == "Shall we continue in English? Ska vi fortsätta på svenska?"
    assert phone.row()["language"] == "sv" and state_of(phone)["askedSwitch"] == "en"
    assert agent.loads(phone.row()["agentMessages"])[-1]["content"] == reply   # the history holds what was said
    phone.say("Yes")
    assert "you asked whether to continue in English" in seen[1]["system"]
    assert phone.row()["language"] == "en" and "askedSwitch" not in state_of(phone)


def test_the_restaurants_name_never_switches_the_call(live):
    name = "Trattoria Roma Centrale"
    assert language.content_words(name, [name]) == 0
    d = language.decision("sv", None, "it", "Trattoria Roma Centrale", allowed=AWS, names=["Trattoria Roma Centrale"])
    assert tuple(d) == ("sv", None) and d.reason == "short"
    # with a Swedish request around it the hint still reads Swedish
    text = "Hej, jag vill boka på Trattoria Roma Centrale"
    assert language.hint(language.without_names(text, ["Trattoria Roma Centrale"])) == "sv"
    phone = Phone(live)
    record(live, [Turn(text="[sv] Gärna, för hur många?")])
    phone.say("Bistro A")
    assert phone.row()["language"] == "sv"


def test_one_word_never_switches_and_a_switch_holds_for_two_turns():
    assert language.decide("sv", "en", "en", "Okay", allowed=AWS) == ("sv", None)
    strong = "Hallo, ich möchte einen Tisch für morgen reservieren"
    d = language.decision("en", "de", "de", strong, allowed=AWS, turns_since_switch=1)
    assert tuple(d) == ("en", None) and (d.reason, d.ask) == ("dwell", "de")
    assert language.decide("en", "de", "de", strong, allowed=AWS, turns_since_switch=3) == ("de", None)
    # an explicit request is honoured at once, a fourth switch never
    assert language.decide("en", None, "de", "Bitte auf Deutsch", allowed=AWS, turns_since_switch=1) == ("de", None)
    assert language.decision("en", "de", "de", strong, allowed=AWS, switches=3, max_switches=3).reason == "capped"
    # same signal twice
    assert language.decide("sv", None, "es", "Hola, quiero una mesa", allowed=AWS, previous_tag="es") == ("es", None)
    # a language named inside a question is no request unless the reply follows it
    assert language.decide("sv", "sv", "sv", "Talar personalen engelska?", allowed=AWS) == ("sv", None)
    assert language.requested_languages("Ich spreche kein Schwedisch") == {"sv"}
    assert language.decide("sv", "de", "de", "Ich spreche kein Schwedisch, bitte", allowed=AWS) == ("de", None)


def test_the_pre_switch_needs_the_same_strong_evidence():
    text = "Hi, I would like to book a table"
    assert language.pre_switch("sv", "en", text, allowed=AWS) == "en"
    assert language.pre_switch("sv", "en", "Hi there", allowed=AWS) == "sv"                 # under three words
    assert language.pre_switch("sv", "en", text, allowed=AWS, turns_since_switch=1) == "sv"
    assert language.pre_switch("sv", "en", text, allowed=AWS, switches=3) == "sv"
    assert language.pre_switch("sv", "fa", "سلام، می‌خواهم برای فردا یک میز", allowed=AWS) == "sv"   # not on the line


def _book_in_english(live, phone):
    """An English call that held a table for four tomorrow at 19:00 and heard the read-back in English."""
    live.availability.set(TOMORROW, ["18:00", "19:00", "20:00"])
    record(live, [
        Turn(text="[en] Of course. For how many people, and which day?"),
        {"tool_uses": [{"id": "h1", "name": "hold_table",
                        "input": {"date": TOMORROW, "time": "19:00", "partySize": 4}}]},
        Turn(text="[en] Seven in the evening is free. What name should the booking be under?"),
        Turn(text="[en] A table for four tomorrow at seven in the evening under Anna. Is that correct?"),
    ])
    phone.say("Hi, I would like to book a table please")
    phone.say("Four people tomorrow at seven in the evening")
    phone.say("The name is Anna Berg")
    assert phone.row()["language"] == "en"


def test_mid_call_english_to_swedish_keeps_date_and_party_and_reads_back_again(live, capsys):
    phone = Phone(live)
    _book_in_english(live, phone)
    hold = next(iter(state_of(phone)["proposals"]))
    seen = record(live, [
        {"tool_uses": [{"id": "c1", "name": "create_reservation",
                        "input": {"holdId": hold, "name": "Anna Berg", "confirmed": True}}]},
        Turn(text="[sv] Då läser jag upp det igen: ett bord för fyra imorgon klockan sju på kvällen, i namnet "
                  "Anna Berg. Stämmer det?"),
        {"tool_uses": [{"id": "c2", "name": "create_reservation",
                        "input": {"holdId": hold, "name": "Anna Berg", "confirmed": True}}]},
        Turn(text="[sv] Klart, bordet är bokat."),
    ])
    reply = phone.say("Kan vi ta det på svenska istället, jag förstår bättre")
    assert "Stämmer det?" in reply and phone.row()["language"] == "sv"
    assert "switched to Swedish" in seen[0]["system"] and "read it back again in Swedish" in seen[0]["system"]
    refused = json.loads(seen[1]["last"]["content"][0]["content"])
    assert refused["ok"] is False and refused["code"] == "readBackFirst"     # the English yes does not count
    assert live.invoke.reservations == {}
    proposal = state_of(phone)["proposals"][hold]
    assert (proposal["date"], proposal["time"], proposal["partySize"]) == (TOMORROW, "19:00", 4)
    assert phone.say("Ja, det stämmer") == "Klart, bordet är bokat."
    (booking,) = live.invoke.reservations.values()
    assert booking["partySize"] == 4 and booking["startTime"] == "19:00"
    reasons = [x["reason"] for x in turn_lines(capsys.readouterr().out)]
    assert "explicit" in reasons or "strong" in reasons


def test_a_switch_found_only_after_the_model_ran_needs_a_read_back_in_a_later_turn():
    state = {"turn": 5, "proposals": {"h1": {"turn": 3, "date": TOMORROW, "partySize": 4}},
             "foundTurn": {"r1": 2}}
    session._read_back_again(state, 6)
    assert state["proposals"]["h1"]["turn"] == 6 and state["foundTurn"]["r1"] == 6
    assert state["proposals"]["h1"]["date"] == TOMORROW and state["proposals"]["h1"]["partySize"] == 4


def test_three_switches_at_most_on_every_line():
    assert session.language_locked({"callRef": "conv-1", "languageSwitches": 3})
    assert session.language_locked({"callRef": "connect-1", "languageSwitches": 3})
    assert not session.language_locked({"callRef": "conv-1", "languageSwitches": 2})


def test_a_switch_on_the_connect_line_still_uses_the_flow_handshake(live):
    out, attrs = lex(live, "contact-sw1", "Hej")
    live.model.responses = [Turn(text="[de] Gerne. Für wie viele Personen?")]
    out, attrs = lex(live, "contact-sw1", "Hallo, ich möchte einen Tisch für morgen reservieren", attrs)
    assert attrs["next"] == "switch" and attrs["voice"] == "Vicki"
    live.model.responses = [Turn(text="[en] Sure.")]
    out, attrs = lex(live, "contact-sw1", "Hello, I would like a table for four", flow(attrs))
    assert "next" not in attrs and attrs["lang"] == "de"                       # two turns after a switch: asks
    assert copy.ASK_CONTINUE["en"] in out["messages"][0]["content"]


# --- the per-turn log line --------------------------------------------------------------------------------

def test_every_turn_logs_one_latency_line_without_the_transcript(live, capsys):
    record(live, [{"tool_uses": [{"id": "i1", "name": "get_restaurant_info", "input": {}}]},
                  Turn(text="[sv] Vi har öppet till tio ikväll.")])
    phone = Phone(live)
    phone.say("Hej, hur länge har ni öppet ikväll?")
    out = capsys.readouterr().out
    (line,) = turn_lines(out)
    for key in ("modelMs", "toolMs", "agentMs", "totalMs", "rounds", "cacheReadTokens", "cacheWriteTokens", "lang",
                "switched", "reason"):
        assert key in line, key
    assert line["rounds"] == 2 and line["tools"] == 1 and line["lang"] == "sv" and line["switched"] is False
    assert line["totalMs"] >= line["agentMs"] >= 0 and line["modelMs"] >= 0 and line["toolMs"] >= 0
    assert {m["Name"] for m in line["_aws"]["CloudWatchMetrics"][0]["Metrics"]} == {"TurnLatencyMs", "TurnRounds"}
    assert line["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[]]          # no new metrics, no cost
    assert "öppet" not in json.dumps(line) and "tio ikväll" not in out


def test_the_turn_engine_measures_model_and_tool_time():
    class Model:
        def __init__(self):
            self.n = 0

        def converse(self, system, messages, tools_, *, max_tokens):
            self.n += 1
            if self.n == 1:
                return Turn(None, (agent_model_real.ToolUse("t1", "get_menu", {}),), "tool_use")
            return agent_model_real.UsageTurn("[sv] Vi har lasagne.", (), "end_turn",
                                              {"cacheReadInputTokens": 1200, "cacheWriteInputTokens": 30})

    class Executor:
        turn = 1

        def run(self, name, args):
            return {"dishes": []}

    result = agent.run_turn(Model(), [], "Vad har ni?", Executor(), system="s")
    assert result.rounds == 2 and result.cache_read == 1200 and result.cache_write == 30
    assert result.model_ms >= 0 and result.tool_ms >= 0 and result.latency_ms >= result.model_ms


# --- Bedrock prompt caching -------------------------------------------------------------------------------

class FakeBedrock:
    def __init__(self, fail_first=None):
        self.calls, self.fail_first = [], fail_first

    def converse_stream(self, **args):
        self.calls.append(args)
        if self.fail_first and len(self.calls) == 1:
            raise self.fail_first
        return {"stream": [{"contentBlockDelta": {"delta": {"text": "Hej"}}},
                           {"messageStop": {"stopReason": "end_turn"}},
                           {"metadata": {"usage": {"inputTokens": 40, "cacheReadInputTokens": 3100,
                                                   "cacheWriteInputTokens": 0}}}]}


def test_the_static_prompt_tools_and_history_get_cache_points(monkeypatch):
    monkeypatch.delenv("PHONE_AGENT_PROMPT_CACHE", raising=False)
    assert agent_model_real.CACHE_SPLIT == copy.TURN_SECTION
    from datetime import date
    plain = agent.system_prompt("sv", None, "Bistro A", date(2026, 11, 20))
    noted = agent.system_prompt("sv", None, "Bistro A", date(2026, 11, 20), disclose_now=True, resume=True)
    assert noted.startswith(plain) and copy.TURN_SECTION not in plain and "# Continue" in noted
    bedrock = FakeBedrock()
    turn = agent_model_real.RealAgentModel(client=bedrock, model_id="m").converse(
        noted, [{"role": "user", "content": "Hej"}], tools.TOOLS, max_tokens=400)
    args = bedrock.calls[0]
    assert args["system"][0] == {"text": plain} and args["system"][1] == agent_model_real.CACHE_POINT
    assert args["system"][2]["text"].startswith("# This turn") and "# Continue" in args["system"][2]["text"]
    assert args["toolConfig"]["tools"][-1] == agent_model_real.CACHE_POINT
    assert args["messages"][-1]["content"][-1] == agent_model_real.CACHE_POINT
    assert sum(json.dumps(args).count("cachePoint") for _ in [0]) == 3        # at most 4 per request
    assert turn.usage["cacheReadInputTokens"] == 3100


def test_caching_can_be_switched_off_and_falls_back_when_refused(monkeypatch):
    from botocore.exceptions import ClientError
    monkeypatch.setenv("PHONE_AGENT_PROMPT_CACHE", "off")
    bedrock = FakeBedrock()
    agent_model_real.RealAgentModel(client=bedrock, model_id="m").converse(
        "s", [{"role": "user", "content": "Hej"}], tools.TOOLS, max_tokens=400)
    assert "cachePoint" not in json.dumps(bedrock.calls[0])
    monkeypatch.delenv("PHONE_AGENT_PROMPT_CACHE")
    refused = ClientError({"Error": {"Code": "ValidationException", "Message": "cachePoint"}}, "ConverseStream")
    bedrock = FakeBedrock(fail_first=refused)
    model = agent_model_real.RealAgentModel(client=bedrock, model_id="m")
    assert model.converse("s" + copy.TURN_SECTION + "note", [{"role": "user", "content": "Hej"}], tools.TOOLS,
                          max_tokens=400).text == "Hej"
    assert len(bedrock.calls) == 2 and "cachePoint" not in json.dumps(bedrock.calls[1])
    assert bedrock.calls[1]["system"] == [{"text": "s\n\n# This turn\nnote"}] and model.cache_refused
    model.converse("s", [{"role": "user", "content": "Hej"}], tools.TOOLS, max_tokens=400)
    assert "cachePoint" not in json.dumps(bedrock.calls[2])                    # stays off in this container


# --- natural phrasing and the fixed sentences ----------------------------------------------------------------

def test_the_prompt_asks_for_natural_varied_phrasing_and_grounded_facts():
    from datetime import date
    prompt = agent.system_prompt("sv", None, "Bistro A", date(2026, 11, 20))
    assert '"Okej", "Precis"' in prompt and "never the same one twice in a row" in prompt
    assert "Sure, how many people will be joining you?" in prompt
    assert "stored in Swedish" in prompt and "exactly as stored, never translated and never invented" in prompt
    assert "must come from a tool result or from the caller" in prompt
    assert "never announce that you are checking" in prompt                   # no pretended lookups
    assert "answer only in German" in agent.system_prompt("de", None, "Bistro A", date(2026, 11, 20))
    other = agent.system_prompt("sw", None, "Bistro A", date(2026, 11, 20))
    assert copy.OPENERS_OTHER in other and "{" not in other


def test_every_line_language_has_every_fixed_sentence():
    assert set(language.AWS_LANGUAGES) <= set(copy.LINE_LANGUAGES)
    assert set(copy.TARGET_LANGUAGES) == {"sv", "en", "ar", "fa", "bs", "es", "id", "de", "fr", "it", "tr", "pl"}
    for name in ("DISCLOSURE_SHORT", "ERROR", "STILL_CHECKING", "MAX_LENGTH", "TRANSFERRING", "STILL_THERE",
                 "ASK_CONTINUE", "OPENERS"):
        missing = set(copy.LINE_LANGUAGES) - set(getattr(copy, name))
        assert not missing, (name, missing)
    for code in ("fa", "bs", "id"):            # no voice on the Connect line: said in their own language
        assert code in copy.UNSUPPORTED_LANGUAGE and code in copy.UNSUPPORTED_CALLBACK
    for lang in copy.TARGET_LANGUAGES:
        assert len(set((copy.STILL_CHECKING[lang],) + copy.STILL_CHECKING_MORE[lang])) == 3, lang
    for lines in copy.STILL_CHECKING_MORE.values():
        for line in lines:
            assert "!" not in line and not DASHES.search(line)
    assert copy.still_checking_line("sv", 0) == copy.STILL_CHECKING["sv"]
    assert copy.still_checking_line("sv", 1) != copy.still_checking_line("sv", 2)
    assert copy.still_checking_line("sw", 0) is None


def test_ask_switch_reads_in_the_voice_of_the_call():
    assert copy.ask_switch("sv", "de") == "Sollen wir auf Deutsch weitermachen? Ska vi fortsätta på svenska?"
    assert copy.ask_switch("sv", "ar", connect=True) == "Ska vi fortsätta på svenska? " + copy.WHICH_LANGUAGE["en"]
    assert copy.ask_switch("sv", "ar").startswith(copy.ASK_CONTINUE["ar"])
    assert copy.ask_switch("sv", "sw", candidate_name="Swahili") == \
        "Shall we continue in Swahili? Ska vi fortsätta på svenska?"


# --- menu in another language --------------------------------------------------------------------------------

def test_a_dish_asked_for_in_another_language_matches_the_swedish_menu():
    assert tools.menu_words("Do you have chicken?")[:2] == ["have", "kyckl"]
    assert "fisk" in tools.menu_words("¿Tienen pescado?") and "veget" in tools.menu_words("Vegetarisch bitte")

    class Ex(tools.Executor):
        def __init__(self):
            self.state, self._cache = {}, {}

        def _call(self, *a, **k):
            return 200, {"items": [{"name": "Kycklingspett", "price": 189}, {"name": "Entrecôte", "price": 295}]}

    out = Ex().get_menu({"query": "chicken"})
    assert out == {"dishes": [{"name": "Kycklingspett", "price": 189, "currency": "SEK"}], "sourceLanguage": "sv"}


# --- booking tools and guards unchanged ----------------------------------------------------------------------

def test_booking_tools_keep_their_canonical_inputs_and_confirmation():
    by = {t["name"]: t["input_schema"] for t in tools.TOOLS}
    assert by["get_availability"]["required"] == ["date", "partySize"]
    assert by["hold_table"]["required"] == ["date", "time", "partySize"]
    assert by["create_reservation"]["required"] == ["holdId", "name", "confirmed"]
    assert by["move_reservation"]["required"] == ["reservationId", "date", "time", "confirmed"]
    assert by["cancel_reservation"]["required"] == ["reservationId", "confirmed"]
    assert by["hold_table"]["properties"]["date"]["description"] == "YYYY-MM-DD, from the calendar in your instructions"
    assert by["hold_table"]["properties"]["time"]["description"] == "HH:MM, 24 hour clock, on the hour or half hour"
    assert by["hold_table"]["properties"]["partySize"]["type"] == "integer"
    assert by["create_reservation"]["properties"]["confirmed"]["description"] == \
        "true only after the caller said a clear yes to your read-back"
    assert tools.TOOL_NAMES == ("get_restaurant_info", "get_availability", "hold_table", "create_reservation",
                                "send_guarantee_link", "find_reservations", "move_reservation", "cancel_reservation",
                                "get_menu", "transfer_to_staff", "request_callback", "end_call")
    assert tools.SAY["readBackFirst"].startswith("The caller has not heard a read-back of this yet.")
    assert agent.WRITES == ("create_reservation", "move_reservation", "cancel_reservation", "request_callback",
                            "transfer_to_staff", "send_guarantee_link")


def test_owner_wording_is_unchanged():
    assert copy.DISCLOSURE["sv"] == "Hej och välkommen till {restaurant}. Du pratar med vår digitala assistent."
    assert copy.DEFAULT_AGENT_NAME == "Booqy-assistenten" and copy.BOOQY_SAID == "Bookie"
    terms = copy.guarantee_terms("sv", no_show="tvåhundra kronor", late="hundra kronor", hours="tjugofyra timmar")
    assert terms["sentence"] == ("För den bokningen behövs ett kort som garanti, men inget dras nu. Restaurangen tar "
                                 "ut en avgift om bokningen avbokas senare än tjugofyra timmar före besöket, eller "
                                 "om ni inte kommer.")
    assert terms["question"] == "Vill du ha mer information om det?" and "kronor" not in terms["sentence"]
    for name, value in vars(copy).items():
        texts = value.values() if isinstance(value, dict) else [value] if isinstance(value, str) else []
        for text in texts:
            if isinstance(text, str):
                assert "AI-assistent" not in text, name


def test_a_proper_name_is_removed_as_whole_words_only():
    assert language.without_names("Har ni barnstol på Bar Nord?", ["Bar", "Bar Nord"]).split() == \
        ["Har", "ni", "barnstol", "på", "?"]
