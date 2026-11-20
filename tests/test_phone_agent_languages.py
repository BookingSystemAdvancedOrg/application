"""The phone line in many languages with AWS only (Connect + Lex V2 + Polly):
the language table, the provider's language list, the switch handshake with
the Connect flow (Close, no message, next=switch, say, voice), the switch cap,
the garbled first sentence, the turn languages and the after-call translation
into Swedish and English (phone-agent-translate)."""

import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from shared.agent_model import Turn
from shared.features import phone_agent as pa
from shared.features.phone_agent import copy, language, session
from test_phone_agent_calls import NUMBER, Phone, live  # noqa: F401 (fixture)
from test_phone_agent_live import lex as lex_sv
from test_phone_agent_support import CALLER, TENANT_A, env, load

assert live and env
ROOT = Path(__file__).parents[1]


def lex(e, session_id, text, attrs=None, locale=None):
    """A turn in the locale the flow runs the bot in: the call's language (attrs `lang`), else sv_SE."""
    if attrs is None:
        return lex_sv(e, session_id, text)
    locale = locale or language.AWS_LANGUAGES[attrs.get("lang") or "sv"]["lexLocale"]
    event = {"sessionId": session_id, "inputTranscript": text, "bot": {"localeId": locale},
             "sessionState": {"intent": {"name": "FallbackIntent", "state": "InProgress"},
                              "sessionAttributes": attrs}}
    out = e.live.handler({"lexV2": event})
    return out, out["sessionState"]["sessionAttributes"]


def flow(attrs):
    """What the Connect flow passes to the next bot block after a switch."""
    return {k: attrs[k] for k in ("tenantId", "callId", "lang", "callerNumber", "dialedNumber") if k in attrs}


def turns_of(e, attrs):
    row = e.live.calls.get_call(attrs["tenantId"], attrs["callId"])
    return row, pa.query_all(pa.tenant_pk(TENANT_A), row["SK"] + "#T#")


# --- the language table ------------------------------------------------------------------------

def test_aws_language_table_is_complete_and_known_to_the_model():
    doc = json.loads((ROOT / "shared/features/phone_agent/aws_languages.json").read_text())
    assert "infrastructure/phone-test-line/languages.json" in doc["_comment"]
    assert language.AWS_LANGUAGES == doc["languages"]
    # the exact list Terraform's byte-identical copy (infrastructure/phone-test-line/languages.json) has:
    # a locale here that the bot lacks fails every switch to it
    assert list(language.AWS_LANGUAGES) == ["sv", "en", "da", "fi", "de", "fr", "es", "it", "nl", "pl", "pt", "ar",
                                            "tr", "cs", "ca", "ja", "ko", "hi", "ru", "is", "no", "zh", "yue", "ro", "cy"]
    for code, spec in language.AWS_LANGUAGES.items():
        assert set(spec) - {"assistedNlu"} == {"lexLocale", "connectLanguage", "voice", "engine"}, code
        assert spec.get("assistedNlu", "Primary") == "Primary", code
        assert spec["engine"] in ("neural", "standard"), code
        assert code in language.NAMES, code
    assert language.AWS_LANGUAGES["sv"] == {"lexLocale": "sv_SE", "connectLanguage": "sv-SE", "voice": "Elin",
                                            "engine": "neural"}
    assert language.locale_language("de_DE") == "de" and language.locale_language("xx_XX") is None


def test_connect_calls_get_the_aws_list_and_other_calls_the_elevenlabs_list():
    assert session.allowed_languages({}, {"callRef": "connect-abc"}) == language.AWS_CALL_LANGUAGES
    assert session.allowed_languages({}, {"callRef": "conv-1"}) == language.voice_languages()
    assert session.allowed_languages({}) == language.voice_languages()
    assert "so" not in language.AWS_CALL_LANGUAGES and "no" in language.AWS_CALL_LANGUAGES
    assert "ca" in language.AWS_CALL_LANGUAGES


def test_unsupported_language_is_said_in_the_callers_language_then_english():
    en = copy.UNSUPPORTED_LANGUAGE["en"]
    assert "English or Swedish" in en and "call you back" in en and "Arabic" not in en
    assert copy.unsupported_language() == en
    # Bosnian on the Connect line: Elin can read the Latin script, so Bosnian first, then English
    assert copy.unsupported_language("bs", connect=True) == f"{copy.UNSUPPORTED_LANGUAGE['bs']} {en}"
    # Persian on the Connect line: Elin cannot read the Arabic script, English only (never silent Swedish)
    assert copy.unsupported_language("fa", connect=True, current="sv") == en
    assert copy.unsupported_language("fa") == f"{copy.UNSUPPORTED_LANGUAGE['fa']} {en}"   # a multilingual voice
    assert copy.unsupported_language("bs", callback=True, connect=True) == \
        f"{copy.UNSUPPORTED_CALLBACK['bs']} {copy.UNSUPPORTED_CALLBACK['en']}"


# --- the Connect line: switching ------------------------------------------------------------------

def test_switch_closes_without_a_message_and_hands_the_reply_to_the_flow(live):
    out, attrs = lex(live, "contact-l1", "Hej")
    live.model.responses = [Turn(text="[de] Gerne. Für wie viele Personen?")]
    out, attrs = lex(live, "contact-l1", "Hallo, ich möchte einen Tisch für morgen reservieren", attrs)
    state = out["sessionState"]
    assert out["messages"] == []
    assert state["dialogAction"] == {"type": "Close"} and state["intent"]["state"] == "Fulfilled"
    assert attrs["next"] == "switch" and attrs["lang"] == "de"
    assert (attrs["connectLanguage"], attrs["voice"], attrs["engine"]) == ("de-DE", "Vicki", "Neural")
    assert attrs["callerNumber"] == CALLER and attrs["dialedNumber"] == NUMBER
    assert attrs["say"] == ('<speak><prosody rate="100%">Ich bin der digitale Assistent des Restaurants. '
                            'Gerne. Für wie viele Personen?</prosody></speak>')
    row, turns = turns_of(live, attrs)
    assert row["language"] == "de" and row["languageSwitches"] == 1 and row["status"] == "talking"
    caller = [t for t in turns if t["role"] == "caller"]
    assert caller[-1]["lang"] == "de" and caller[-1]["asrLocale"] == "sv_SE"   # said in German, heard by sv_SE
    assert caller[0]["lang"] == "sv" and turns[-1]["lang"] == "de" and turns[-1]["translation"] == "pending"
    # the flow starts the bot again in de_DE with the ids: an ordinary German turn, no flow attributes left
    live.model.responses = [Turn(text="[de] Wunderbar.")]
    attrs = flow(attrs)
    event = {"sessionId": "contact-l1", "inputTranscript": "Vier Personen", "bot": {"localeId": "de_DE"},
             "sessionState": {"intent": {"name": "Samtal", "state": "InProgress"}, "sessionAttributes": attrs}}
    out = live.live.handler({"lexV2": event})
    assert out["sessionState"]["dialogAction"] == {"type": "ElicitIntent"}
    assert out["messages"][0]["content"] == '<speak><prosody rate="100%">Wunderbar.</prosody></speak>'
    assert "next" not in out["sessionState"]["sessionAttributes"]
    assert turns_of(live, attrs)[1][-2]["asrLocale"] == "de_DE"


def test_switching_back_to_swedish_uses_the_same_handshake(live):
    out, attrs = lex(live, "contact-l2", "Hej")
    live.model.responses = [Turn(text="[en] Of course. For how many?")]
    out, attrs = lex(live, "contact-l2", "Hi, I would like to book a table for tomorrow", attrs)
    assert attrs["next"] == "switch" and attrs["voice"] == "Amy"
    live.model.responses = [Turn(text="[sv] Självklart. Hur många blir ni?")]
    out, attrs = lex(live, "contact-l2", "Kan vi prata svenska istället, jag vill boka ett bord", flow(attrs))
    assert out["messages"] == [] and attrs["next"] == "switch"
    assert (attrs["connectLanguage"], attrs["voice"], attrs["engine"], attrs["lang"]) == \
        ("sv-SE", "Elin", "Neural", "sv")
    assert "Självklart. Hur många blir ni?" in attrs["say"]


def test_a_connect_call_switches_at_most_three_times(live):
    out, attrs = lex(live, "contact-l3", "Hej")
    # explicit requests (a switch right after a switch needs one)
    for tagged, said in (("[en] Sure.", "Hi, can we speak English please, I would like a table"),
                         ("[de] Gerne.", "Hallo, können wir bitte Deutsch sprechen"),
                         ("[en] Sure.", "Sorry, can we speak English, I want a table")):
        live.model.responses = [Turn(text=tagged)]
        out, attrs = lex(live, "contact-l3", said, flow(attrs))
        assert attrs["next"] == "switch"
    live.model.responses = [Turn(text="[de] Gerne.")]
    out, attrs = lex(live, "contact-l3", "Hallo, können wir bitte Deutsch sprechen", flow(attrs))
    assert out["sessionState"]["dialogAction"] == {"type": "ElicitIntent"} and "next" not in attrs
    row, _ = turns_of(live, attrs)
    assert row["language"] == "en" and row["languageSwitches"] == 3


def test_the_end_of_a_connect_call_closes_with_next_end(live):
    out, attrs = lex(live, "contact-l4", "Hej")
    live.model.responses = [
        {"tool_uses": [{"id": "u1", "name": "end_call", "input": {"outcome": "info", "summary": "Hälsade"}}]},
        Turn(text="Tack för samtalet, hej då."),
    ]
    out, attrs = lex(live, "contact-l4", "Tack, det var allt", attrs)
    assert out["sessionState"]["dialogAction"] == {"type": "Close"} and attrs["next"] == "end"
    assert "hej då" in out["messages"][0]["content"]


def test_a_switch_in_the_ending_turn_has_the_new_voice_say_the_goodbye(live):
    out, attrs = lex(live, "contact-l4b", "Hej")
    live.model.responses = [
        {"tool_uses": [{"id": "u1", "name": "end_call", "input": {"outcome": "info", "summary": "Öppettider"}}]},
        Turn(text="[de] Ja, heute bis zweiundzwanzig Uhr. Tschüss."),
    ]
    out, attrs = lex(live, "contact-l4b", "Ich wollte nur fragen ob ihr heute offen habt, danke tschüss", attrs)
    assert out["messages"] == [] and out["sessionState"]["dialogAction"] == {"type": "Close"}
    assert attrs["next"] == "switchEnd" and attrs["lang"] == "de"
    assert (attrs["connectLanguage"], attrs["voice"], attrs["engine"]) == ("de-DE", "Vicki", "Neural")
    assert "Tschüss" in attrs["say"]
    row, _ = turns_of(live, attrs)
    assert row["status"] in ("ended", "processed")


def test_a_garbled_first_sentence_is_asked_about_once_in_swedish_and_english(live):
    out, attrs = lex(live, "contact-l5", "Hej")

    def garbled(text, attrs):
        event = {"sessionId": "contact-l5", "inputTranscript": text, "bot": {"localeId": "sv_SE"},
                 "transcriptions": [{"transcription": text, "transcriptionConfidence": 0.21}],
                 "sessionState": {"intent": {"name": "FallbackIntent", "state": "InProgress"},
                                  "sessionAttributes": attrs}}
        out = live.live.handler({"lexV2": event})
        return out, out["sessionState"]["sessionAttributes"]

    out, attrs = garbled("vala mak tobi sena ruki", attrs)
    assert out["sessionState"]["dialogAction"] == {"type": "ElicitIntent"}
    assert out["messages"][0]["content"] == f'<speak><prosody rate="100%">{copy.ask_language()}</prosody></speak>'
    row, turns = turns_of(live, attrs)
    assert row["languageAsked"] and turns[-1]["text"] == copy.ask_language()
    out, attrs = garbled("vala mak tobi sena ruki", attrs)       # asked once: the agent answers now
    assert copy.ASK_LANGUAGE["en"] not in out["messages"][0]["content"]


def test_a_fallback_to_the_swedish_locale_puts_the_call_back_in_swedish(live):
    out, attrs = lex(live, "contact-l6", "Hej")
    live.model.responses = [Turn(text="[de] Gerne.")]
    out, attrs = lex(live, "contact-l6", "Hallo, ich möchte bitte einen Tisch reservieren", attrs)
    lex(live, "contact-l6", "Hej, jag vill boka ett bord", flow(attrs), locale="sv_SE")   # the flow fell back
    assert turns_of(live, attrs)[0]["language"] == "sv"


def test_elevenlabs_calls_keep_their_language_list_and_store_turn_languages(live):
    phone = Phone(live)
    phone.say("Hej, har ni öppet ikväll?")
    turns = [t for t in phone.turns() if t["role"] != "tool"]
    assert [t.get("lang") for t in turns] == ["sv", "sv", "sv"] and "asrLocale" not in turns[1]
    assert {t["translation"] for t in turns} == {"pending"}


# --- the call detail returns the turn languages and translations ---------------------------------------

def test_call_detail_returns_lang_and_translations(live):
    from test_phone_agent_support import call as api_call
    phone = Phone(live)
    phone.say("Hej, har ni öppet ikväll?")
    status, body = api_call(live, "GET", "/locations/{locationId}/phone-agent/calls/{callId}", callId=phone.call_id)
    assert status == 200
    first = body["turns"][0]
    assert first["lang"] == "sv" and first["translation"] == "pending"
    assert first["textSv"] is None and first["textEn"] is None


# --- phone-agent-translate --------------------------------------------------------------------------

class FakeTranslate:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail or {}

    def translate_text(self, *, Text, SourceLanguageCode, TargetLanguageCode):
        self.calls.append((SourceLanguageCode, TargetLanguageCode))
        code = self.fail.get(TargetLanguageCode)
        if code:
            raise ClientError({"Error": {"Code": code, "Message": "x"}}, "TranslateText")
        return {"TranslatedText": f"<{TargetLanguageCode}>{Text}"}


@pytest.fixture
def translate(live, monkeypatch):
    fn = load("phone-agent-translate")
    monkeypatch.setattr(fn, "BACKOFF_S", 0)
    fn._clients["translate"] = FakeTranslate()
    return fn


def ended(phone):
    return {"source": "booqy.phone-agent", "detail-type": "PhoneCall.Ended",
            "detail": {"tenantId": TENANT_A, "callId": phone.call_id}}


def german_call(e):
    phone = Phone(e)
    e.model.responses = [Turn(text="[de] Gerne. Für wie viele Personen?")]
    phone.say("Hallo, ich möchte einen Tisch für morgen reservieren")
    return phone


def test_translate_skips_the_language_a_turn_already_is_in(live, translate):
    phone = german_call(live)
    out = translate.handler(ended(phone))
    assert out["ok"] and out["failed"] == 0
    by_lang = {t["lang"]: t for t in phone.turns() if t["role"] in ("caller", "agent")}
    sv, de = phone.turns()[0], by_lang["de"]
    assert "textSv" not in sv and sv["textEn"].startswith("<en>") and sv["translation"] == "done"
    assert de["textSv"].startswith("<sv>") and de["textEn"].startswith("<en>") and de["translation"] == "done"
    assert ("sv", "sv") not in translate._clients["translate"].calls
    assert all(t["text"] == t["text"].lstrip("<") for t in phone.turns())     # the original is unchanged


def test_translate_is_idempotent(live, translate):
    phone = german_call(live)
    translate.handler(ended(phone))
    fake = translate._clients["translate"]
    before = len(fake.calls)
    out = translate.handler(ended(phone))
    assert len(fake.calls) == before and out["done"] == 0


def test_translate_marks_failed_after_bounded_retries_and_retries_on_redelivery(live, translate, caplog):
    phone = german_call(live)
    fake = translate._clients["translate"] = FakeTranslate(fail={"en": "ThrottlingException"})
    with pytest.raises(translate.TranslationIncomplete):        # Lambda's async retries, then the DLQ
        translate.handler(ended(phone))
    failed = [t for t in phone.turns() if t["role"] != "tool"]
    assert {t["translation"] for t in failed} == {"failed"}
    assert len([c for c in fake.calls if c[1] == "en"]) == len(failed) * translate.TRANSLATE_ATTEMPTS
    assert "Gerne" not in caplog.text and "Hallo" not in caplog.text            # no transcript in the logs
    translate._clients["translate"] = FakeTranslate()
    out = translate.handler(ended(phone))
    assert out["failed"] == 0 and {t["translation"] for t in phone.turns() if t["role"] != "tool"} == {"done"}


def test_translate_never_overwrites_an_erased_turn(live, translate):
    phone = german_call(live)
    first = phone.turns()[0]
    pa.table().update_item(Key={"PK": first["PK"], "SK": first["SK"]}, UpdateExpression="SET #x = :r",
                           ExpressionAttributeNames={"#x": "text"}, ExpressionAttributeValues={":r": "[raderad]"})
    turn = dict(first)                                   # read before the erase, saved after it
    assert translate._save(turn, {"textEn": "x"}, "done") is False


def test_a_permanent_translate_error_stays_failed_without_a_retry(live, translate):
    phone = german_call(live)
    translate._clients["translate"] = FakeTranslate(fail={"en": "TextSizeLimitExceededException"})
    out = translate.handler(ended(phone))
    assert out["ok"] and out["failed"] >= 1
    assert {t["translation"] for t in phone.turns() if t["role"] != "tool"} == {"failed"}


def test_translate_ignores_other_events_and_maps_codes(translate):
    assert translate.handler({"detail-type": "PhoneCall.Answered", "detail": {}})["code"] == "ignored"
    assert translate.handler({"detail-type": "PhoneCall.Ended", "detail": {"tenantId": TENANT_A,
                                                                           "callId": "nope"}})["code"] == "invalid"
    assert translate.source_code("pt") == "pt-PT" and translate.source_code("yue") == "zh-TW"
    assert translate.source_code("ti") == "auto" and translate.source_code("no") == "no"
    for code in language.AWS_LANGUAGES:
        assert translate.source_code(code) != "auto", code
