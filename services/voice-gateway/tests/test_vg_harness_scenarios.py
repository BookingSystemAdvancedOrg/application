"""The scenario data and the caller audio cache (builder 'harness'): structural checks that need no gateway,
no AWS and no numpy, so they run in root CI."""

import dataclasses
import re

import pytest

from vg_harness import caller_audio as ca
from vg_harness import scenarios as sc

DASH = re.compile("[" + chr(0x2013) + chr(0x2014) + "]|&[nm]dash;")


def test_every_voice_and_language_scenario_is_there():
    keys = [s.key for s in sc.ALL]
    assert keys == [f"voice-{c}" for c in "ABCDEF"] + [f"lang-{c}" for c in "ABCDEFGHIJK"]
    assert len(set(keys)) == len(keys)


@pytest.mark.parametrize("scenario", sc.ALL, ids=lambda s: s.key)
def test_scenario_is_well_formed(scenario):
    assert sc.validate(scenario) == []


def test_first_step_can_never_wait_for_a_reply_to_talk_over():
    assert all(s.steps[0].trigger != "during_reply" for s in sc.ALL)


def test_lookup_by_short_forms():
    assert sc.get("voice-A") is sc.get("voice-a")
    assert sc.get("langc").key == "lang-C"
    assert sc.get("lang_d").key == "lang-D"
    with pytest.raises(KeyError):
        sc.get("nope")


def test_validate_reports_problems():
    bad = dataclasses.replace(sc.get("voice-A"), steps=(sc.Step("sv", (sc.Piece("  "),), trigger="during_reply"),))
    problems = sc.validate(bad)
    assert any("empty text" in p for p in problems) and any("after_ms" in p for p in problems)
    assert any("nothing to talk over" in p for p in problems)
    foreign = dataclasses.replace(sc.get("voice-A"), steps=(sc.Step("fa", (sc.Piece("x"),)),))
    assert any("gloss" in p for p in sc.validate(foreign))


def test_no_dash_in_any_piece_or_gloss():
    for s in sc.ALL:
        for step in s.steps:
            for piece in step.pieces:
                assert not DASH.search(piece.text) and not DASH.search(piece.gloss)


def test_the_scenarios_cover_the_design_expectations():
    used = {f.name for s in sc.ALL for f in dataclasses.fields(s.expect)
            if getattr(s.expect, f.name) is not None and getattr(s.expect, f.name) is not False}
    for required in ("no_cutoff_before_ms", "max_hesitation_acks", "backchannels_between", "backchannel_overlaps",
                     "barge_in_stop_max_ms", "readback_repeated_after_barge_in", "party_size", "booked",
                     "booked_only_after_yes", "no_invented_times", "fee_rule", "final_language", "switches",
                     "unsupported_reply", "callback_topic", "readback_in_new_language_first"):
        assert required in used, required


def test_voice_a_has_the_one_and_a_half_second_pause():
    step = sc.get("voice-A").steps[0]
    assert [p.pause_after_ms for p in step.pieces] == [400, 1500, 0]


# --- the caller audio cache ----------------------------------------------------------------------------------

def test_every_polly_voiced_piece_is_cached_so_ci_costs_nothing():
    """Run `python -m vg_harness.caller_audio --warm` (booqy-dev, cents) after changing a scenario's text."""
    missing = []
    for s in sc.ALL:
        for step in s.steps:
            for piece in step.pieces:
                if step.language in ca.CALLER_VOICES and not (ca.cache_path(piece.text, step.language).is_file()):
                    missing.append(f"{s.key}: {piece.text[:40]}")
    assert missing == []


def test_the_only_pieces_without_a_fixture_are_the_languages_without_a_polly_voice():
    report = ca.warm(dry_run=True)
    assert all(line.startswith("no voice: fa") for line in report), report


def test_cache_stays_small():
    total = sum(p.stat().st_size for p in ca.CACHE.glob("*"))
    assert total < 1_500_000, f"{total} bytes: the cache is committed, keep it small"


def test_cache_key_depends_on_voice_and_text():
    assert ca.cache_key("Hej", "sv") != ca.cache_key("Hej", "en")
    assert ca.cache_key("Hej", "sv") != ca.cache_key("Hej!", "sv")
    assert ca.cache_key("Hej", "sv") == ca.cache_key("Hej", "sv")
