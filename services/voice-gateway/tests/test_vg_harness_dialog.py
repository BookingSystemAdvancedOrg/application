"""The scenarios as plain dialog (builder 'harness'): every step's text through the real phone-agent-call code
(app.start_call, session.call_turn, the real tools and guards) in the fake world, no audio and no gateway. This proves the
world, the glosses and the scripted model deliver what the audio scenarios expect, so a failing audio run points at the
gateway and not at the test double. Needs moto (the application's own dev dependency)."""

import contextlib
import io

import pytest
from shared.features.phone_agent import copy

pytest.importorskip("moto")

from vg_harness import analysis as an
from vg_harness import scenarios as sc
from vg_harness.textrun import glosses_of, run_text
from vg_harness.world import EVENING, parse_slot, pivot


@pytest.fixture(scope="module")
def runs():
    cache = {}

    def run(key):
        if key not in cache:
            with contextlib.redirect_stdout(io.StringIO()):          # the application prints CloudWatch EMF lines
                cache[key] = run_text(sc.get(key))
        return cache[key]

    return run


def made(out):
    return [c for c in out.tool_calls if c["fn"] == "create-pending-reservation" and c.get("status") == 201]


def test_hesitation_text_is_one_booking_request_for_four_on_friday(runs):
    out = runs("voice-A")
    assert len(out.turns) == 1 and "ehm" in out.turns[0].caller
    assert out.turns[0].reply.startswith("Jag håller ett bord 2026-11-06 klockan 19:00 för 4")
    assert not made(out)                                           # nothing is written before a yes


def test_correction_books_five_only_after_the_yes_and_the_name(runs):
    out = runs("voice-C")
    assert [t.reply.split("?")[0][-25:] for t in out.turns][0].endswith("boka det") and "för 5" in out.turns[0].reply
    assert "namn" in out.turns[1].reply and "bokad" in out.turns[2].reply
    (booking,) = made(out)
    assert booking["party"] == 5 and booking["time"] == "19:00"


def test_a_new_time_after_the_interruption_gets_a_new_read_back(runs):
    out = runs("voice-D")
    assert "klockan 20:00" in out.turns[0].reply and "klockan 21:00" in out.turns[1].reply
    assert all("Ska jag boka det?" in t.reply for t in out.turns[:2]) and not made(out)


def test_the_fee_rule_is_said_without_an_amount_and_the_party_of_eight_books_pending(runs):
    out = runs("voice-E")
    rule = out.turns[0].reply
    assert "avgift" in rule and rule.endswith("Vill du ha mer information om det?")
    assert not an.AMOUNT.search(rule) and not any(an.AMOUNT.search(t.reply) for t in out.turns)
    (booking,) = made(out)
    assert booking["party"] == 8
    assert out.sms and out.sms[0]["notice"]["kind"] == "phone-agent.guarantee-link"      # the card link goes by SMS


def test_a_party_below_the_guarantee_threshold_hears_no_fee_rule(runs):
    assert all("avgift" not in t.reply for t in runs("voice-C").turns)


def test_a_full_time_offers_only_times_the_tools_returned(runs):
    out = runs("voice-F")
    # the lookup comes first (brain/prefetch.py, the prompt's step 2): the nearest free times around the asked one
    assert ("19:00 är tyvärr fullt" in out.turns[0].reply or "Närmast klockan 19:00" in out.turns[0].reply) \
        and not made(out)
    offered = {t for c in out.tool_calls for t in ([c["time"]] if c["time"] else [])}
    said = {f"{h:02d}:{m:02d}" for t in out.turns for h, m in an.times_in(t.reply) if m is not None}
    assert said <= set(EVENING) and "19:00" in said and "19:15" not in said


def test_swedish_booking_stays_swedish(runs):
    out = runs("lang-A")
    assert out.language == "sv" and len(made(out)) == 1


def test_english_caller_gets_the_short_disclosure_and_books(runs):
    out = runs("lang-B")
    assert out.language == "en" and "digital assistant" in out.turns[0].reply and len(made(out)) == 1
    assert out.turns[0].actions[0]["name"] == "language_detection"


def test_persian_is_unsupported_then_a_callback_with_the_language_topic(runs):
    out = runs("lang-C")
    assert copy.UNSUPPORTED_LANGUAGE["en"] in out.turns[0].reply and [c["topic"] for c in out.callbacks] == ["language:fa"]
    assert any(a["name"] == "end_call" for a in out.turns[1].actions) and not made(out)


def test_arabic_books_with_the_call_in_arabic(runs):
    out = runs("lang-D")
    assert out.language == "ar" and len(made(out)) == 1


def test_english_to_swedish_switches_twice(runs):
    out = runs("lang-E")
    assert out.language == "sv" and sum(1 for t in out.turns for a in t.actions if a["name"] == "language_detection") == 2


def test_a_short_okay_and_a_restaurant_name_do_not_switch(runs):
    assert runs("lang-F").language == "sv"


def test_the_read_back_is_repeated_in_english_after_the_switch_before_the_booking(runs):
    out = runs("lang-G")
    assert out.turns[0].reply.startswith("Jag håller ett bord")           # read back in Swedish first
    assert "Shall I book it?" in out.turns[1].reply                       # again in English after the switch
    (booking,) = made(out)
    assert booking["party"] == 4 and out.language == "en"


def test_interruption_keeps_english(runs):
    out = runs("lang-H")
    assert out.language == "en" and "19:30" in out.turns[1].reply


def test_every_non_swedish_non_english_piece_has_a_gloss_the_model_reads():
    for s in sc.ALL:
        glosses, languages = glosses_of(s)
        for step in s.steps:
            for piece in step.pieces:
                assert piece.text in languages and languages[piece.text] == step.language
                if step.language not in ("sv", "en"):
                    assert glosses[piece.text] == piece.gloss


def test_pivot_turns_clock_words_into_digits():
    assert pivot("Jag vill ha klockan sju") == "Jag vill ha klockan 7"
    assert pivot("Helst runt halv sju") == "Helst runt klockan 18:30"
    assert pivot("make that half past seven") == "make that at 19:30"
    assert pivot("bord för sju personer") == "bord för sju personer"


def test_parse_slot_maps_weekday_names_to_the_fake_calendar():
    assert parse_slot("fri 19:00") == ("2026-11-06", "19:00") and parse_slot("Saturday 18:30") == ("2026-11-07", "18:30")
