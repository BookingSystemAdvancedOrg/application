"""Every Expect check on recorded calls built by hand (builder 'harness'): one passing and one failing call per
field, so a scenario cannot pass for the wrong reason. No gateway, no numpy, no AWS."""

import dataclasses

import pytest

from vg_harness import analysis as an
from vg_harness.asserts import CHECKS, check, checked_fields
from vg_harness.builders import api_call, booking, result_of, seg
from vg_harness.scenarios import ALL, Expect, Piece, Scenario, Step, get
from voice_gateway import events as ev


def scenario(**expect) -> Scenario:
    """A scenario that checks only what `expect` names (the always-on defaults are switched off)."""
    base = {"backchannel_overlaps": None, "eos_to_first_audio_p50_max_ms": None}
    return Scenario("t", "t", (Step("sv", (Piece("x"),)),), Expect(**{**base, **expect}))


def turn_end(at, text="ja", eos=None, language="sv"):
    return ev.TurnEnd(at, text, language, at - 900, eos if eos is not None else at - 400, "silence")


def started(at, kind="assistant", language="sv", dur=1000, item="i"):
    return ev.PlaybackStarted(at, f"{item}{at}", kind, language, dur)


def done(at, language="sv", awaiting=False):
    return ev.AgentTurnDone(at, 1, language, 1, False, False, awaiting_confirmation=awaiting)


def sentence(at, text, language="sv"):
    return ev.AgentSentence(at, 1, 0, text, language)


def failures(expect, result):
    return check(scenario(**expect), result)


def test_every_expect_field_has_a_check():
    fields = {f.name for f in dataclasses.fields(Expect)}
    assert fields == {name for name, _ in CHECKS}


def test_checked_fields_follow_the_expectation():
    assert "booked" in checked_fields(Expect(booked=False))       # False is an expectation here
    assert "booked" not in checked_fields(Expect())
    assert "fee_rule" not in checked_fields(Expect(fee_rule=False))   # False is "off" for a flag
    assert "backchannel_overlaps" in checked_fields(Expect())       # 0 overlaps is always expected
    assert "voice_of_each_sentence_matches_language" in checked_fields(Expect())


def test_a_run_that_raised_fails_before_any_check():
    out = check(scenario(booked=True), result_of(error="RuntimeError: boom"))
    assert out == ["run: the call raised RuntimeError: boom"]


# --- hesitation -----------------------------------------------------------------------------------------------

HES = [seg(0, 0, 1000, 2500), seg(0, 1, 2900, 3400), seg(0, 2, 4900, 7000)]


def test_cutoff_inside_the_hesitation_fails():
    cut = result_of(turn_end(3100, "jag skulle vilja boka bord för"), segments=HES)
    assert "inside the hesitation" in failures({"no_cutoff_before_ms": 2500}, cut)[0]


def test_turn_after_the_whole_utterance_passes_and_a_too_early_one_fails():
    assert failures({"no_cutoff_before_ms": 2500}, result_of(turn_end(7800), segments=HES)) == []
    short = [seg(0, 0, 1000, 1500), seg(0, 1, 1600, 2000)]      # last words start at 1600, whole step ends 2000
    too_soon = result_of(turn_end(3300), segments=short)         # after the last words, but only 2300 ms in
    assert any("expected at or after +2500" in f for f in failures({"no_cutoff_before_ms": 2500}, too_soon))
    fine = result_of(turn_end(3600), segments=short)
    assert failures({"no_cutoff_before_ms": 2500}, fine) == []


def test_no_turn_at_all_is_a_failure_not_a_pass():
    assert any("no_cutoff_before_ms" in f for f in failures({"no_cutoff_before_ms": 2500}, result_of(segments=HES)))


def test_hesitation_backchannels_are_counted_before_the_last_words():
    one = result_of(turn_end(7800), started(3000, "backchannel"), segments=HES)
    two = result_of(turn_end(7800), started(3000, "backchannel"), started(4000, "backchannel"), segments=HES)
    late = result_of(turn_end(7800), started(5200, "backchannel"), started(6000, "backchannel"), segments=HES)
    assert not [f for f in failures({"max_hesitation_acks": 1}, one) if f.startswith("max_hes")]
    assert any(f.startswith("max_hesitation_acks") for f in failures({"max_hesitation_acks": 1}, two))
    assert not [f for f in failures({"max_hesitation_acks": 1}, late) if f.startswith("max_hes")]


def test_monologue_backchannel_range_and_overlap():
    monologue = [seg(0, 0, 0, 20000)]
    bc = lambda t: started(t, "backchannel", dur=300)
    assert not [f for f in failures({"backchannels_between": (1, 2)}, result_of(bc(6000), bc(12000), segments=monologue)) if "between" in f]
    assert any("between" in f for f in failures({"backchannels_between": (1, 2)}, result_of(segments=monologue)))
    assert any("between" in f for f in failures({"backchannels_between": (1, 2)}, result_of(bc(5000), bc(9000), bc(13000), segments=monologue)))
    over = result_of(bc(6000), ev.PlaybackFinished(6300, "i6000", "backchannel", 300, False), ev.VadResume(6200, 200), segments=monologue)
    assert an.backchannel_overlaps(over.events) == 1
    assert any(f.startswith("backchannel_overlaps") for f in failures({"backchannel_overlaps": 0}, over))
    clean = result_of(bc(6000), ev.PlaybackFinished(6300, "i6000", "backchannel", 300, False), ev.VadResume(6800, 200))
    assert an.backchannel_overlaps(clean.events) == 0 and not [f for f in failures({"backchannel_overlaps": 0}, clean) if f.startswith("backchannel_ov")]


# --- barge-in --------------------------------------------------------------------------------------------------

def test_barge_in_stop_time():
    fast = result_of(ev.BargeIn(5000, 4700, "duration"), ev.PlaybackFlushed(5180, 400, 180))
    slow = result_of(ev.BargeIn(5000, 4700, "duration"), ev.PlaybackFlushed(5420, 400, 420))
    none = result_of()
    unflushed = result_of(ev.BargeIn(5000, 4700, "duration"))
    assert not [f for f in failures({"barge_in_stop_max_ms": 300}, fast) if f.startswith("barge")]
    assert "420 ms" in failures({"barge_in_stop_max_ms": 300}, slow)[0]
    assert "no BargeIn" in failures({"barge_in_stop_max_ms": 300}, none)[0]
    assert "never followed" in failures({"barge_in_stop_max_ms": 300}, unflushed)[0]


def test_readback_repeated_after_barge_in():
    base = [ev.BargeIn(5000, 4700, "words"), ev.PlaybackFlushed(5150, 300, 150)]
    again = result_of(*base, ev.ToolFinished(6000, 2, "hold_table", True, 20), done(6100, awaiting=True), started(6300))
    never = result_of(*base, done(6100, awaiting=False), started(6300))
    unplayed = result_of(*base, ev.ToolFinished(6000, 2, "hold_table", True, 20), done(6100, awaiting=True))
    key = {"readback_repeated_after_barge_in": True}
    assert not [f for f in failures(key, again) if f.startswith("readback_rep")]
    assert any("no new read-back" in f for f in failures(key, never))
    assert any("never played" in f for f in failures(key, unplayed))
    assert any("no BargeIn" in f for f in failures(key, result_of()))


# --- booking -----------------------------------------------------------------------------------------------------

def read_back_then_yes(book_at=9000):
    return [ev.ToolFinished(2000, 1, "hold_table", True, 10), done(2200, awaiting=True), turn_end(5000, "Ja, det stämmer."),
            done(5400, awaiting=True), turn_end(8000, "Anna Svensson."), done(book_at + 300)]


def test_party_size_comes_from_the_booking_else_the_last_hold():
    r = result_of(*read_back_then_yes(), tool_calls=[api_call("get-availability", 1000, status=200, party=4),
                                                      booking(9000, party=5)])
    assert not [f for f in failures({"party_size": 5}, r) if f.startswith("party")]
    assert "expected 4" in failures({"party_size": 4}, r)[0]
    asked_only = result_of(tool_calls=[api_call("get-availability", 1000, status=200, party=4)])
    assert not [f for f in failures({"party_size": 4}, asked_only) if f.startswith("party")]


def test_booked_counts_successful_reservation_writes():
    one = result_of(tool_calls=[booking(9000)])
    none = result_of()
    two = result_of(tool_calls=[booking(9000), booking(9500)])
    refused = result_of(tool_calls=[api_call("create-pending-reservation", 9000, status=409)])
    assert not [f for f in failures({"booked": True}, one) if f.startswith("booked:")]
    assert "0 reservations" in failures({"booked": True}, none)[0]
    assert "2 reservations" in failures({"booked": True}, two)[0]
    assert "0 reservations" in failures({"booked": True}, refused)[0]
    assert not [f for f in failures({"booked": False}, none) if f.startswith("booked:")]
    assert "expected none" in failures({"booked": False}, one)[0]


def test_booking_needs_a_heard_read_back_and_a_clear_yes():
    good = result_of(*read_back_then_yes(), tool_calls=[booking(8200)])
    assert not [f for f in failures({"booked_only_after_yes": True}, good) if f.startswith("booked_only")]
    no_yes = result_of(ev.ToolFinished(2000, 1, "hold_table", True, 10), done(2200, awaiting=True),
                       turn_end(5000, "Anna Svensson."), tool_calls=[booking(5500)])
    assert "without a heard read-back" in failures({"booked_only_after_yes": True}, no_yes)[0]
    no_hold = result_of(turn_end(5000, "Ja."), tool_calls=[booking(5500)])
    assert failures({"booked_only_after_yes": True}, no_hold)
    talked_over = result_of(ev.ToolFinished(2000, 1, "hold_table", True, 10), ev.BargeIn(2500, 2300, "words"),
                            turn_end(5000, "Ja."), tool_calls=[booking(5500)])
    assert failures({"booked_only_after_yes": True}, talked_over)
    new_hold_after_yes = result_of(ev.ToolFinished(2000, 1, "hold_table", True, 10), turn_end(4000, "Ja."),
                                   ev.ToolFinished(4500, 2, "hold_table", True, 10), tool_calls=[booking(5500)])
    assert failures({"booked_only_after_yes": True}, new_hold_after_yes)
    heard_again = result_of(ev.ToolFinished(2000, 1, "hold_table", True, 10), ev.BargeIn(2500, 2300, "words"),
                            turn_end(3000, "Vänta, nio."), ev.ToolFinished(3500, 2, "hold_table", True, 10),
                            turn_end(6000, "Ja."), tool_calls=[booking(6500)])
    assert not [f for f in failures({"booked_only_after_yes": True}, heard_again) if f.startswith("booked_only")]


def test_no_invented_times():
    ok = result_of(turn_end(1000, "Har ni bord på fredag klockan sju?"), sentence(2000, "Klockan 19:00 är fullt. Jag har 18:30 eller 19:30."),
                   offered=("18:30", "19:30"))
    bad = result_of(sentence(2000, "Jag har 20:15."), offered=("18:30", "19:30"))
    words = result_of(sentence(2000, "Vi har bord klockan åtta."), offered=("18:30", "19:30"))
    hours = result_of(sentence(2000, "Vi har bord klockan sju."), offered=("19:30",))
    key = {"no_invented_times": True}
    assert not [f for f in failures(key, ok) if f.startswith("no_invented")]
    assert "20:15" in failures(key, bad)[0]
    assert failures(key, words)
    assert not [f for f in failures(key, hours) if f.startswith("no_invented")]


def test_fee_rule_says_the_rule_and_never_an_amount_unasked():
    rule = "För den bokningen behövs ett kort som garanti, men inget dras nu. Restaurangen tar ut en avgift om ni inte kommer."
    asked = "Vill du ha mer information om det?"
    ok = result_of(sentence(1000, rule), sentence(1100, asked))
    amount = result_of(sentence(1000, rule), sentence(1100, asked), sentence(1200, "Avgiften är 150 kr per person."))
    missing = result_of(sentence(1000, "Jag håller ett bord."))
    detail = result_of(turn_end(900, "Hur mycket kostar det?"), sentence(1000, rule), sentence(1100, asked),
                       sentence(1200, "Det är 150 kr per person."))
    key = {"fee_rule": True}
    assert not [f for f in failures(key, ok) if f.startswith("fee_rule")]
    assert "an amount was spoken" in failures(key, amount)[0]
    assert "never said" in failures(key, missing)[0]
    assert not [f for f in failures(key, detail) if f.startswith("fee_rule")]


# --- language --------------------------------------------------------------------------------------------------

def test_language_path_final_language_and_switches():
    switch_to_en = ev.LanguageDecided(3000, ev.LanguageAction.SWITCH, "en", "en", "strong", 1)
    r = result_of(switch_to_en, done(4000, language="en"))
    assert an.language_path(r.events) == ["sv", "en"]
    assert not [f for f in failures({"final_language": "en", "switches": 1}, r) if f.startswith(("final", "switches"))]
    assert "expected sv" in failures({"final_language": "sv"}, r)[0]
    back = result_of(switch_to_en, done(4000, "en"), ev.LanguageDecided(8000, ev.LanguageAction.SWITCH, "sv", "sv", "explicit", 2),
                     done(9000, "sv"))
    assert an.switches(back.events) == 2
    keep = result_of(ev.LanguageDecided(3000, ev.LanguageAction.KEEP, "sv", None, "uncertain", 0), done(4000, "sv"))
    assert an.switches(keep.events) == 0
    assert "expected 0" in failures({"switches": 0}, r)[0]


def test_asked_unsupported_and_callback():
    asked = result_of(ev.LanguageDecided(3000, ev.LanguageAction.ASK, "sv", "en", "uncertain", 0))
    assert not [f for f in failures({"asked_language": True}, asked) if f.startswith("asked")]
    assert failures({"asked_language": True}, result_of())
    assert not [f for f in failures({"asked_language": False}, result_of()) if f.startswith("asked")]
    from shared.features.phone_agent import copy
    said = result_of(sentence(2000, copy.unsupported_language(connect=True)))
    flagged = result_of(ev.LanguageDecided(2000, ev.LanguageAction.UNSUPPORTED, "sv", "fa", "unsupported", 0))
    assert not [f for f in failures({"unsupported_reply": True}, said) if f.startswith("unsupp")]
    assert not [f for f in failures({"unsupported_reply": True}, flagged) if f.startswith("unsupp")]
    assert failures({"unsupported_reply": True}, result_of(sentence(2000, "Hej!")))
    cb = result_of(callbacks=[{"topic": "language:fa"}])
    assert not [f for f in failures({"callback_topic": "language:fa"}, cb) if f.startswith("callback")]
    assert "language:fa" in failures({"callback_topic": "language:fa"}, result_of(callbacks=[{"topic": "other"}]))[0]


def test_readback_in_new_language_first():
    sw = ev.LanguageDecided(3000, ev.LanguageAction.SWITCH, "en", "en", "explicit", 1)
    key = {"readback_in_new_language_first": True, "final_language": "en"}
    good = result_of(sw, done(4000, "en", awaiting=True), tool_calls=[booking(7000)])
    stale = result_of(done(1000, "sv", awaiting=True), sw, done(4000, "en", awaiting=False), tool_calls=[booking(7000)])
    no_switch = result_of(done(1000, "sv", awaiting=True), tool_calls=[booking(7000)])
    assert not [f for f in failures(key, good) if f.startswith("readback_in")]
    assert any("no read-back in the new language" in f for f in failures(key, stale))
    assert any("never switched" in f for f in failures(key, no_switch))


def test_native_voice_guard():
    ok = result_of(sentence(1000, "Hello there", "en"), tts_calls=[("Hello there", "en", "Amy")])
    wrong = result_of(sentence(1000, "Hello there", "en"), tts_calls=[("Hello there", "sv", "Elin")])
    novoice = result_of(tts_calls=[("x", "fa", None)])
    unspoken = result_of(sentence(1000, "salam", "fa"))
    assert failures({}, ok) == []
    assert "not its own language" in failures({}, wrong)[0]
    assert "no voice" in failures({}, novoice)[0]
    assert "no voice" in failures({}, unspoken)[0]
    off = dataclasses.replace(scenario(), expect=Expect(voice_of_each_sentence_matches_language=False, backchannel_overlaps=None, eos_to_first_audio_p50_max_ms=None))
    assert check(off, wrong) == []


def test_latency_p50():
    fast = result_of(turn_end(2000, eos=1500), started(2600), turn_end(8000, eos=7500), started(8900))
    slow = result_of(turn_end(2000, eos=1500), started(4500), turn_end(8000, eos=7500), started(10500))
    unanswered = result_of(turn_end(2000, eos=1500))
    assert not [f for f in failures({"eos_to_first_audio_p50_max_ms": 1500}, fast) if f.startswith("eos_to")]
    assert "p50" in failures({"eos_to_first_audio_p50_max_ms": 1500}, slow)[0]
    assert "no reply audio" in failures({"eos_to_first_audio_p50_max_ms": 1500}, unanswered)[0]
    assert [r.eos_to_first_audio for r in an.replies(fast.events)] == [1100, 1400]


def test_a_reply_belongs_to_the_turn_before_the_next_one():
    r = result_of(turn_end(1000, eos=800), turn_end(4000, eos=3800), started(4600))
    first, second = an.replies(r.events)
    assert first.first_audio_ms is None and second.first_audio_ms == 4600


def test_times_in_text():
    assert an.times_in("klockan sju och 18:30 samt at eight") == {(7, None), (18, 30), (8, None)}
    assert an.times_in("fyra personer") == set()
    assert an.time_allowed((7, None), {(19, 0)}) and not an.time_allowed((7, 30), {(19, 0)})


@pytest.mark.parametrize("text,yes", [("Ja, det stämmer.", True), ("Yes, please book it.", True), ("نعم، هذا صحيح.", True),
                                      ("Nej tack", False), ("Anna Svensson.", False), ("", False)])
def test_clear_yes(text, yes):
    assert an.is_clear_yes(text) is yes


def test_every_scenario_has_at_least_one_check():
    assert all(checked_fields(s.expect) for s in ALL)


def test_scenario_expectations_name_real_checks():
    names = checked_fields(get("voice-D").expect)
    assert {"barge_in_stop_max_ms", "readback_repeated_after_barge_in", "booked_only_after_yes"} <= set(names)
