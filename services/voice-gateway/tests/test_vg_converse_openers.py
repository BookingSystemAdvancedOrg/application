"""Openers at the caller's turn end (speak/openers.py): the right kind, varied, never a claim of a lookup that is not
planned, never a yes before the answer (converse builder)."""

import random

from voice_gateway.speak.openers import OPENERS, OpenerPicker, is_bare_yes_no, kind_for, lines, polly_text


def test_the_kind_follows_the_turn():
    assert kind_for("Jag vill boka bord för fyra i morgon klockan sju", lookup=True, awaiting_confirmation=False) \
        == "check"
    assert kind_for("Har ni öppet på söndag?", lookup=False, awaiting_confirmation=False) == "see"
    assert kind_for("Anna Svensson", lookup=False, awaiting_confirmation=False) == "ack"
    assert kind_for("Ja.", lookup=False, awaiting_confirmation=False) is None          # a quick exchange
    assert kind_for("Ja, tack", lookup=False, awaiting_confirmation=False, asked=True) == "ack"   # a hold follows
    assert kind_for("Ja, tack.", lookup=False, awaiting_confirmation=True) == "ack"    # a write follows the yes
    assert is_bare_yes_no("nej tack") and not is_bare_yes_no("nej, jag menar fem")


def test_check_only_when_a_lookup_is_planned_and_no_opener_starts_with_a_yes():
    rng = random.Random(1)
    picker = OpenerPicker(rng, chance=1.0)
    for _ in range(50):
        key = picker.pick("Kan jag boka bord?", "sv", lookup=False, awaiting_confirmation=False)
        assert key not in lines("sv", "check")
    for lang, rows in OPENERS.items():
        for key, kind, text in rows:
            spoken = text.split(">", 1)[1].split("<", 1)[0].lower()
            assert spoken.split()[0].strip(",.") not in ("ja", "yes", "right", "sure", "absolut"), (lang, key)
            if kind != "check":
                assert not any(w in spoken for w in ("kollar", "check", "prüfe", "tarkistan")), (lang, key)
            assert chr(0x2013) not in text and chr(0x2014) not in text


def test_openers_vary_and_skip_some_turns():
    picker = OpenerPicker(random.Random(7), chance=0.65)
    picks = [picker.pick("Anna Svensson", "sv", lookup=False, awaiting_confirmation=False) for _ in range(200)]
    played = [p for p in picks if p]
    assert 0.5 < len(played) / len(picks) < 0.8
    assert all(a != b for a, b in zip(picks, picks[1:]) if a and b)                  # never the same line twice
    always = OpenerPicker(random.Random(7), chance=0.0)
    assert all(always.pick("boka", "sv", lookup=True, awaiting_confirmation=False) for _ in range(10))


def test_a_language_without_openers_gets_none_and_missing_clips_are_skipped():
    picker = OpenerPicker(random.Random(3), chance=1.0)
    assert picker.pick("salam", "fa", lookup=False, awaiting_confirmation=False) is None
    assert picker.pick("Anna", "sv", lookup=False, awaiting_confirmation=False, available=lambda l, k: False) is None
    assert polly_text("sv", "kollar") and polly_text("sv", "nope") is None


def test_no_filler_once_the_lookup_finished_the_answer_is_a_round_away():
    """Live 2026-11-20: a filler started 70 ms before the reply and held it back 1.5 s."""
    from voice_gateway.config import SpeakConfig
    from voice_gateway.events import ToolStarted
    from voice_gateway.speak.fillers import FillerPolicy
    slow = FillerPolicy(SpeakConfig())
    slow.on_tool_started(ToolStarted(1000, 1, "get_availability"))
    assert slow.due(1000 + 700, "sv", assistant_busy=False) == 0           # still running: the filler covers it
    fast = FillerPolicy(SpeakConfig())
    fast.on_tool_started(ToolStarted(1000, 1, "get_availability"))
    fast.on_tool_finished(1060)
    assert fast.due(1000 + 700, "sv", assistant_busy=False) is None and fast.pending_since is None
