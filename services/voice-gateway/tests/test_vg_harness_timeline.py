"""The timeline and the CLI (builder 'harness'), on a call built by hand."""

import pytest

from vg_harness import cli, runner, timeline
from vg_harness.asserts import check
from vg_harness.builders import booking, result_of, seg
from vg_harness.scenarios import get
from voice_gateway import events as ev


def barge_call():
    """voice-D in miniature: the caller asks, the agent reads back, the caller talks over it, a new read-back, a yes."""
    return result_of(
        ev.StateChanged(0, ev.CallState.CONNECTING, ev.CallState.GREETING),
        ev.PlaybackStarted(200, "g", "fixed", "sv", 3000),
        ev.PlaybackFinished(3200, "g", "fixed", 3000, False),
        ev.VadStart(4300, 4000, 0.9),
        ev.SttFinal(6000, "s1", "Jag vill boka bord för två i morgon klockan åtta.", "sv", 1, 4000, 6000),
        ev.TurnHold(6100, "dangling", 1400),
        ev.TurnEnd(6700, "Jag vill boka bord för två i morgon klockan åtta.", "sv", 4000, 6000, "silence"),
        ev.ToolStarted(6800, 1, "hold_table"), ev.ToolFinished(7000, 1, "hold_table", True, 200),
        ev.AgentSentence(7200, 1, 0, "Jag håller ett bord klockan 20:00. Ska jag boka det?", "sv"),
        ev.PlaybackStarted(7500, "a1", "assistant", "sv", 3000),
        ev.AgentTurnDone(7300, 1, "sv", 2, False, False, awaiting_confirmation=True, asked_question="yes_no", latency_ms=600),
        ev.BargeIn(8800, 8500, "words"), ev.PlaybackFlushed(8990, 1800, 190),
        ev.PlaybackFinished(8990, "a1", "assistant", 1490, True),
        ev.LanguageDecided(6600, ev.LanguageAction.KEEP, "sv", None, "first_utterance", 0),
        ev.CallEnded(12000, "hangup"),
        segments=[seg(0, 0, 4000, 6000), seg(1, 0, 8500, 10000)],
        tool_calls=[booking(11000)])


def test_lanes_show_caller_reply_and_barge_in_on_one_axis():
    lines = timeline.lanes(barge_call())
    caller = next(x for x in lines if x.startswith("  caller")).split("caller")[1].strip(" ")
    assistant = next(x for x in lines if x.startswith("  assistant"))
    assert "#" in caller and "x" in assistant and "A" in assistant and "F" in assistant
    assert lines[0].startswith("  t") and "0s" in lines[0]


def test_every_event_kind_gets_a_readable_line():
    text = "\n".join(timeline.event_lines(barge_call()))
    for needle in ("CALLER", "starts speaking", "STT", "final", "TURN", "holds the turn open: dangling", "ends:", "TOOL",
                   "hold_table started", "AGENT", "BARGE", "line quiet 190 ms", "ASSIST", "plays assistant", "LANG",
                   "CALL", "ended: hangup"):
        assert needle in text, needle


def test_text_can_be_hidden_for_a_shared_screen():
    shown = "\n".join(timeline.event_lines(barge_call(), show_text=True))
    hidden = "\n".join(timeline.event_lines(barge_call(), show_text=False))
    assert "Jag håller ett bord" in shown and "Jag håller ett bord" not in hidden and "words>" in hidden


def test_verbose_adds_the_noise():
    quiet = timeline.event_lines(barge_call())
    loud = timeline.event_lines(barge_call(), verbose=True)
    assert len(loud) > len(quiet) and any("state" in x or "->" in x for x in loud)


def test_numbers_summarise_latency_barge_in_language_and_booking():
    text = "\n".join(timeline.numbers(barge_call()))
    assert "end of speech to first audio 1500 ms" in text and "turn detection 700 ms" in text
    assert "barge-in at" in text and "190 ms" in text
    assert "language path: sv (0 switches)" in text and "tools: hold_table" in text and "reservations made: 1" in text


def test_render_lists_every_checked_field_with_its_verdict():
    scenario = get("voice-D")
    result = barge_call()
    failures = check(scenario, result)
    out = timeline.render(result, scenario, failures)
    assert out.startswith("== voice-D: Interruption of the read-back  [fake]")
    assert "EXPECTATIONS" in out and "pass  barge_in_stop_max_ms" in out
    assert "FAIL" in out and "booked_only_after_yes" in out          # booked at 11000 with no yes in this miniature call
    assert out.rstrip().splitlines()[-1].startswith("  FAIL:") or "FAIL" in out


def test_an_error_is_the_first_thing_a_reader_sees():
    out = timeline.render(result_of(error="RuntimeError: boom"), get("voice-A"), ["run: the call raised RuntimeError: boom"])
    assert "ERROR: RuntimeError: boom" in out.splitlines()[2]


# --- the CLI ---------------------------------------------------------------------------------------------------------

def test_list_prints_every_scenario(capsys):
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 14 and "voice-A" in out and "lang-H" in out and "during_reply" in out


def test_unknown_scenario_exits_2(capsys):
    assert cli.main(["nope"]) == 2
    assert "unknown scenario" in capsys.readouterr().err


def test_no_arguments_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_text_mode_runs_without_the_gateway(capsys):
    pytest.importorskip("moto")
    assert cli.main(["voice-C", "--text"]) == 0
    out = capsys.readouterr().out
    assert "caller:    Ja, det stämmer." in out and "reservations 1" in out and "[text]" in out


def test_fake_mode_names_what_is_still_missing(monkeypatch, capsys):
    monkeypatch.setattr(runner, "missing_components", lambda: ["voice_gateway.listen.pipeline.Listener.run"])
    assert cli.main(["voice-A"]) == 2
    err = capsys.readouterr().err
    assert "Listener.run" in err and "--text" in err


def test_module_entry_point_puts_the_test_paths_on_sys_path():
    import importlib
    import sys
    replay = importlib.import_module("voice_gateway.replay")
    assert replay.HERE.name == "voice-gateway" and str(replay.HERE / "tests") in sys.path
