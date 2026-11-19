"""What the phone assistant says about itself and the card guarantee fees
(owner 2026-11-19): the greeting "Hej och välkommen till <restaurangens
namn>. Du pratar med vår digitala assistent." with the guest-facing name from
the restaurant's own data (never the legal company name), no AI wording in
any spoken sentence in any language, Booqy said "Bookie", and the fees
said as a rule without amounts unless the caller asks."""

import re
from datetime import date

import pytest

from shared import tenant as shared_tenant
from shared.features.phone_agent import agent, copy
from shared.features.phone_agent import locations as locations_mod
from shared.features.phone_agent import tools
from test_phone_agent_calls import Phone, live  # noqa: F401 (fixture)
from test_phone_agent_support import LOC_A, TENANT_A, env  # noqa: F401 (fixture)

# Words that call the assistant AI, a bot or artificial intelligence, in the languages copy.py has.
_AI_SHORT = re.compile(r"(?<![A-Za-z])(AI|KI|IA|A\.I\.)(?![A-Za-z])")   # case-sensitive: "ai" is Italian
_AI_LONG = re.compile(
    r"\b(bot|robot|chatbot)\b|tekoäly|inteligencia artificial|intelligence artificielle|künstliche|artificial|"
    r"kunstig intelligens|sztuczn|искусствен|штучн|yapay zek|الذكاء الاصطناعي|هوش مصنوعی|زیرەکی دەستکرد|"
    r"ሰው ሰራሽ|mesterséges intelligencia|τεχνητ|人工智能|人工知能|인공지능|umjetn|vještač|veštač|"
    r"inteligência artificial|intelligenza artificiale|kunstmatige|umělá|umelá|artificială|изкуствен|एआई|"
    r"kecerdasan buatan|kepintaran buatan|trí tuệ nhân tạo|செயற்கை", re.I)


class AI_WORDS:      # noqa: N801 (reads like a pattern at the call sites)
    @staticmethod
    def search(text):
        return _AI_SHORT.search(text) or _AI_LONG.search(text)


# The instruction prompt pieces (English for the model, never spoken as such).
_PROMPT_PARTS = {"SYSTEM_PROMPT", "REGISTER", "DISCLOSE_NOW", "LOW_ASR", "UNSUPPORTED", "RESUME", "PHONE_KNOWN",
                 "PHONE_HIDDEN"}


def _spoken_copy():
    """Every fixed sentence in copy.py: (name, language, text)."""
    out = []
    for name, value in vars(copy).items():
        if name.startswith("_") or name in _PROMPT_PARTS or not name.isupper():
            continue
        if isinstance(value, str):
            out.append((name, None, value))
        elif isinstance(value, dict):
            for lang, text in value.items():
                if isinstance(text, dict):
                    out += [(f"{name}.{k}", lang, t) for k, t in text.items() if isinstance(t, str)]
                elif isinstance(text, str):
                    out.append((name, lang, text))
    return out


def test_no_spoken_sentence_in_any_language_calls_the_assistant_ai():
    spoken = _spoken_copy()
    assert len({lang for _, lang, _ in spoken if lang}) >= len(copy.REVIEWED)
    found = [(name, lang, text) for name, lang, text in spoken if AI_WORDS.search(text)]
    assert found == []


def test_every_language_says_it_is_the_restaurants_digital_assistant():
    assert set(copy.REVIEWED) <= set(copy.DISCLOSURE_SHORT)
    assert copy.DISCLOSURE_SHORT["sv"] == "Jag är restaurangens digitala assistent."
    assert copy.DISCLOSURE_SHORT["en"] == "I am the restaurant's digital assistant."
    for lang, text in copy.DISCLOSURE_SHORT.items():
        assert text and not AI_WORDS.search(text), lang
        assert "–" not in text and "—" not in text


@pytest.mark.parametrize("lang", ["sv", "en", "de", "so", "sw"])
def test_the_prompt_never_lets_the_model_call_itself_ai(lang):
    prompt = agent.system_prompt(lang, None, "Bistro A", date(2026, 11, 19), disclose_now=True)
    guard = ("Never call yourself AI, an AI assistant, artificial intelligence, a bot, a robot or a computer, in "
             "any language.")
    assert guard in prompt
    assert "say honestly that you are the restaurant's digital assistant, not a person" in prompt
    assert "that you are the restaurant's digital assistant (never the words AI, artificial intelligence or bot)" \
        in prompt
    rest = prompt.replace(guard, "").replace("(never the words AI, artificial intelligence or bot)", "")
    assert not re.search(r"\bAI\b|artificial intelligence", rest)
    assert "You are the digital phone assistant of the restaurant Bistro A." in prompt


def test_the_assistant_names_itself_booqy_assistenten_unless_the_restaurant_named_it():
    assert copy.agent_name(None) == copy.agent_name("Booqy") == copy.agent_name(" booqy ") == "Booqy-assistenten"
    assert copy.agent_name("Sara") == "Sara"
    prompt = agent.system_prompt("sv", "Booqy", "Bistro A", date(2026, 11, 19))
    assert "When you name yourself, you are Booqy-assistenten (in other languages: the Booqy assistant" in prompt
    assert "When you name yourself, you are Sara." in agent.system_prompt("sv", "Sara", "Bistro A", date(2026, 11, 19))
    assert "[NEEDS YOUR OK: default name]" not in open(copy.__file__, encoding="utf-8").read()


# --- the greeting and the restaurant's name ---------------------------------------------------------

def test_the_greeting_is_the_owners_sentence_with_the_restaurants_name():
    assert copy.greeting("sv", "Booqy", "Restaurang Anar", "Vad kan jag hjälpa dig med?") == (
        "Hej och välkommen till Restaurang Anar. Du pratar med vår digitala assistent. Vad kan jag hjälpa dig med?")
    assert copy.greeting("en", "Sara", "Anar", None) == (
        "Hello and welcome to Anar. You are speaking with our digital assistant. How may I help you?")
    assert copy.greeting("sv", None, "", None) == (
        "Hej och välkommen. Du pratar med vår digitala assistent. Vad kan jag hjälpa dig med?")
    assert copy.greeting("sv", None, "Anar", None, recording=True).startswith("Samtalet spelas in. Hej och välkommen")


@pytest.mark.parametrize("tenant_row, location, expected", [
    ({"name": "Anar"}, {"name": "Anar Södermalm"}, "Anar Södermalm"),        # the location's own name
    ({"name": "RADIN DJ AB"}, {"name": "Restaurant 1"}, "Restaurant 1"),     # never the company
    ({"name": "Anar"}, {"name": ""}, "Anar"),                                # no location name: the restaurant's
    ({"name": "RADIN DJ AB"}, {"name": None}, ""),                           # only a company name: no name
    ({"name": "Pizzeria Roma HB"}, {"name": "Roma Aktiebolag"}, ""),
    ({"name": "Bistro Ltd."}, {"name": "  Bistro   Nord "}, "Bistro Nord"),
    ({"name": "Anar", "legalName": "Anar Restaurang AB"}, None, "Anar"),     # a shared number: the restaurant's
    ({"name": "Absolut Bar"}, None, "Absolut Bar"),                          # "Ab..." is not the company form
    (None, None, ""),
])
def test_public_name_is_the_guest_facing_name_from_the_restaurants_own_data(tenant_row, location, expected):
    assert locations_mod.public_name(tenant_row, location) == expected


def test_a_call_is_greeted_with_the_locations_name_never_the_company(live, monkeypatch):
    location = shared_tenant.get_location(TENANT_A, LOC_A)
    real = shared_tenant.get_tenant
    monkeypatch.setattr(shared_tenant, "get_tenant",
                        lambda tenant_id, fresh=False: {**(real(tenant_id, fresh=True) or {}), "name": "RADIN DJ AB"})
    phone = Phone(live)
    first = phone.turns()[0]["text"]
    assert first.startswith(f"Hej och välkommen till {location['name']}. Du pratar med vår digitala assistent.")
    assert "RADIN" not in first and not AI_WORDS.search(first)


# --- Booqy is said "Bookie" ------------------------------------------------------------------------------

def test_booqy_is_read_as_bookie_by_polly_and_written_out_for_plain_text_voices():
    assert copy.ssml_names("Jag heter Booqy-assistenten.") == (
        'Jag heter <sub alias="Bookie">Booqy</sub>-assistenten.')
    assert copy.ssml_names("booqys bord") == '<sub alias="Bookie">booqy</sub>s bord'
    assert copy.ssml_names("Booqyx") == "Booqyx"
    assert copy.spoken_plain("Du pratar med Booqy-assistenten.") == "Du pratar med Bookie-assistenten."


def test_the_lex_reply_carries_the_alias_inside_the_ssml(live):
    ssml = live.live._ssml("Jag är Booqy-assistenten & hjälper dig.")
    assert ssml == ('<speak><prosody rate="92%">Jag är <sub alias="Bookie">Booqy</sub>-assistenten &amp; hjälper '
                    'dig.</prosody></speak>')


# --- the fees: the rule by default, the amounts only when asked --------------------------------------------

def test_the_prompt_says_the_fee_rule_first_and_amounts_only_when_asked():
    prompt = agent.system_prompt("sv", None, "Bistro A", date(2026, 11, 19))
    assert "say guaranteeTerms.sentence" in prompt and "end that reply with guaranteeTerms.question" in prompt
    assert "Never say an amount there." in prompt
    assert "Only if the caller wants more information, say guaranteeTerms.ifAsked.details" in prompt
    assert "the fees for the whole party" not in prompt
    say = tools.SAY["guaranteeLink"]
    assert "with no amount" in say and "Otherwise never say an amount" in say and "guaranteeTerms.question" in say
    assert "kronor" not in tools.SAY["guaranteeLinkNoTerms"]


def test_the_connect_line_greeting_comes_from_the_restaurants_data(live):
    from test_phone_agent_calls import NUMBER
    location = shared_tenant.get_location(TENANT_A, LOC_A)
    out = live.live.handler({"connectGreeting": {"dialedNumber": NUMBER}})
    assert out["ok"] == "true" and set(out) == {"ok", "greeting"} and all(isinstance(v, str) for v in out.values())
    assert out["greeting"].startswith(f'<speak><prosody rate="92%">Hej och välkommen till {location["name"]}. '
                                      "Du pratar med vår digitala assistent.")
    assert live.live.handler({"connectGreeting": {"dialedNumber": "+46100000999"}}) == {"ok": "false"}
    assert live.live.handler({"connectGreeting": {}}) == {"ok": "false"}
