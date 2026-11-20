"""Semantic turn detection on transcripts with hesitations (listen builder)."""

import pytest

from voice_gateway.config import SttConfig, TurnConfig
from voice_gateway.events import AssistantContext, SttFinal, SttPartial, TurnEnd, TurnHold, VadPause, VadResume, VadStart
from voice_gateway.listen.turn import TurnDetector, classify_tail


class Rig:
    """Drives a TurnDetector on a virtual clock the way the Listener does."""

    def __init__(self, language="sv", ctx=None, cfg=None):
        self.td = TurnDetector(cfg or TurnConfig(), SttConfig(), language=language)
        if ctx:
            self.td.set_context(AssistantContext(0, ctx.get("asked"), ctx.get("confirm", False), language))
        self.now = 0
        self.out = []

    def _take(self, evs):
        self.out.extend(evs)
        return evs

    def run_to(self, t):
        while self.now < t:
            self.now += 20
            self._take(self.td.on_tick(self.now))

    def speak(self, start, ms):
        self.run_to(start)
        self._take(self.td.on_vad(VadStart(start + 100, start, 0.9)))
        self.speech_start, self.speech_end = start, start + ms
        self.speech_ms = ms

    def pause(self, silence_start, speech_ms=None):
        self.run_to(silence_start + 80)
        self._take(self.td.on_vad(VadPause(silence_start + 80, silence_start, speech_ms or self.speech_ms)))

    def resume(self, at, pause_ms):
        self.run_to(at)
        self._take(self.td.on_vad(VadResume(at, pause_ms)))

    def partial(self, at, text):
        self.run_to(at)
        self._take(self.td.on_partial(SttPartial(at, "s", text, "", "sv", 1)))

    def final(self, at, text, conf=0.9, end=None):
        self.run_to(at)
        self._take(self.td.on_final(SttFinal(at, "s", text, "sv", 1, 0, end if end is not None else at, conf)))

    def ends(self):
        return [e for e in self.out if isinstance(e, TurnEnd)]

    def holds(self):
        return [e for e in self.out if isinstance(e, TurnHold)]


# --- classify_tail -----------------------------------------------------------------------------

@pytest.mark.parametrize("text,lang,asked,kind", [
    ("", "sv", None, "empty"),
    ("jag vill boka bord", "sv", None, "complete"),
    ("jag vill boka bord för", "sv", None, "dangling"),
    ("jag vill boka bord för ehm", "sv", None, "filler"),
    ("jag vill boka bord för ehm...", "sv", None, "filler"),
    ("öööh", "sv", None, "filler"),
    ("ja", "sv", "yes_no", "short_answer"),
    ("ja precis", "sv", "yes_no", "short_answer"),
    ("ja", "sv", None, "complete"),
    ("ja men", "sv", "yes_no", "dangling"),
    ("fyra", "sv", "number", "short_answer"),
    ("fyra", "sv", None, "number"),
    ("noll sju tre", "sv", None, "number"),
    ("noll sju tre", "sv", "number", "number"),
    ("det blir 18", "sv", None, "number"),
    ("klockan", "sv", None, "dangling"),
    ("jag vill ha ett", "sv", None, "dangling"),
    ("it is for", "en", None, "dangling"),
    ("it is for um", "en", None, "filler"),
    ("yes", "en", "yes_no", "short_answer"),
    ("for four", "en", None, "number"),
    ("ich möchte einen tisch für", "de", None, "dangling"),
    ("tisch für vier", "de", None, "number"),
    ("bonjour je voudrais réserver pour", "fr", None, "dangling"),
    ("something in an unknown language", "xx", None, "complete"),
])
def test_classify_tail(text, lang, asked, kind):
    assert classify_tail(text, lang, asked) == kind


# --- thresholds and turn ends ------------------------------------------------------------------

def test_complete_clause_ends_after_the_base_silence_with_the_final_already_in():
    r = Rig()
    r.speak(1000, 1500)
    r.partial(1500, "jag vill boka ett bord")
    r.pause(2500, 1500)
    r.final(2700, "Jag vill boka ett bord.")
    r.run_to(4000)
    (end,) = r.ends()
    assert end.text == "Jag vill boka ett bord." and end.reason == "silence"
    assert end.eos_ms == 2500 and end.speech_start_ms == 1000 and end.language == "sv"
    assert 2500 + 550 <= end.at_ms <= 2500 + 550 + 40
    assert end.confidence == pytest.approx(0.9)


def test_waits_for_the_final_up_to_final_wait_then_takes_the_last_partial():
    r = Rig()
    r.speak(0, 1500)
    r.partial(1000, "tisdag kväll")
    r.pause(1500)
    r.run_to(3000)
    (end,) = r.ends()
    assert end.text == "tisdag kväll" and end.reason == "silence"
    assert 1500 + 550 + 350 <= end.at_ms <= 1500 + 550 + 350 + 40


def test_final_arriving_after_the_threshold_ends_at_once():
    r = Rig()
    r.speak(0, 1500)
    r.partial(1000, "tisdag")
    r.pause(1500)
    r.final(1500 + 550 + 100, "Tisdag.")
    (end,) = r.ends()
    assert end.reason == "final_after_silence" and end.at_ms == 2150 and end.text == "Tisdag."


def test_hesitation_scenario_for_ehm_fyra_personer():
    """Voice A: 'för, ehm... fyra personer' must not be cut at the 1.5 s pause."""
    r = Rig()
    r.speak(0, 2500)
    r.partial(2000, "jag skulle vilja boka bord för ehm")
    r.final(2400, "Jag skulle vilja boka bord för, ehm.")
    r.pause(2500)
    r.run_to(2500 + 1500)                                     # the 1.5 s hesitation pause
    assert r.ends() == []
    assert [h.reason for h in r.holds()][:1] == ["filler"] and r.holds()[0].threshold_ms == 1600
    r.resume(4050, 1550)
    r.run_to(4600)
    r.partial(5000, "fyra personer")
    r.pause(5200, 1500)
    r.final(5400, "fyra personer.")
    r.run_to(7000)
    (end,) = r.ends()
    assert end.text == "Jag skulle vilja boka bord för, ehm. fyra personer." and end.speech_start_ms == 0
    assert end.eos_ms == 5200


def test_dangling_word_holds_longer_than_a_complete_clause():
    r = Rig()
    r.speak(0, 1500)
    r.final(1400, "Jag vill boka bord för.")
    r.pause(1500)
    r.run_to(1500 + 1300)
    assert r.ends() == [] and r.holds()[0].reason == "dangling" and r.holds()[0].threshold_ms == 1400
    r.run_to(1500 + 1500)
    assert len(r.ends()) == 1


def test_number_being_spoken_holds_for_the_next_digit():
    r = Rig()
    r.speak(0, 1500)
    r.final(1400, "Noll sju tre")
    r.pause(1500)
    r.run_to(1500 + 1000)
    assert r.ends() == [] and r.holds()[0].reason == "number"
    r.resume(2540, 1040)                                      # more digits come before the 1.1 s run out
    assert r.ends() == []


def test_short_answers_are_quick_after_a_yes_no_question_and_slow_otherwise():
    r = Rig(ctx={"asked": "yes_no"})
    r.speak(0, 400)
    r.final(500, "Ja.")
    r.pause(400)
    r.run_to(1000)
    (end,) = r.ends()
    assert end.text == "Ja." and end.at_ms - 400 <= 350 + 40
    r2 = Rig(ctx={"confirm": True})                           # a read-back awaits the yes
    r2.speak(0, 400)
    r2.final(500, "Ja")
    r2.pause(400)
    r2.run_to(1000)
    assert len(r2.ends()) == 1 and r2.ends()[0].at_ms - 400 <= 350 + 40
    r3 = Rig()
    r3.speak(0, 400)
    r3.final(500, "Ja.")
    r3.pause(400)
    r3.run_to(1000)
    assert r3.ends()[0].at_ms - 400 >= 550


def test_open_question_gives_the_caller_time_to_think():
    r = Rig(ctx={"asked": "open"})
    r.speak(0, 1000)
    r.final(1100, "Anna Svensson.")
    r.pause(1000)
    r.run_to(1000 + 800)
    assert r.ends() == [] and r.holds()[0].reason == "open_question" and r.holds()[0].threshold_ms == 900
    r.run_to(1000 + 960)
    assert len(r.ends()) == 1


def test_threshold_is_capped_and_follows_the_context_and_language():
    r = Rig(cfg=TurnConfig(filler_silence_ms=5000))
    r.speak(0, 1000)
    r.partial(900, "ehm")
    r.pause(1000)
    assert r.td.threshold_ms() == 2200                        # max_silence_ms
    assert Rig().td.threshold_ms() == 550                     # idle: the base
    r = Rig(language="en")
    r.speak(0, 1000)
    r.partial(900, "I would like to book for")
    r.pause(1000)
    assert r.td.threshold_ms() == 1400
    r.td.set_language("sv")
    r.td.set_context(AssistantContext(0, None, False, "sv"))
    assert r.td.threshold_ms() == 550                         # "for" is not Swedish


def test_max_utterance_ends_a_monologue():
    r = Rig()
    r.speak(0, 30000)
    r.partial(500, "och sedan och sedan")
    r.run_to(21000)
    (end,) = r.ends()
    assert end.reason == "max_length" and 20000 <= end.at_ms <= 20040 and end.text == "och sedan och sedan"


def test_noise_is_not_a_turn():
    r = Rig()
    r.speak(0, 60)
    r.pause(60, 60)                                           # under min_speech_ms, no words
    r.run_to(5000)
    assert r.ends() == []
    r2 = Rig()
    r2.speak(0, 600)                                          # voiced, but Transcribe heard no words at all
    r2.pause(600, 600)
    r2.run_to(600 + 550 + 1400)
    assert r2.ends() == []
    r2.run_to(600 + 550 + 1700)
    assert r2.ends() == [] and r2.td.threshold_ms() == 550


def test_slow_transcribe_still_gets_the_turn_when_the_text_arrives_inside_the_grace():
    r = Rig()
    r.speak(0, 600)
    r.pause(600, 600)
    r.final(600 + 550 + 900, "Hej.")
    assert r.ends()[0].text == "Hej." and r.ends()[0].reason == "final_after_silence"


def test_several_finals_are_joined_and_ended_turns_wait_for_reset_or_new_speech():
    r = Rig()
    r.speak(0, 3000)
    r.final(1000, "Hej.", end=900)
    r.final(2400, "Jag vill boka.", end=2300)
    r.pause(3000)
    r.run_to(4000)
    (end,) = r.ends()
    assert end.text == "Hej. Jag vill boka."
    r.final(4100, "late final", end=3000)
    r.run_to(6000)
    assert len(r.ends()) == 1                                 # ended: nothing more until reset or new speech
    r.td.reset()
    r.speak(6000, 800)
    r.final(6900, "Tack.", end=6800)
    r.pause(6800)
    r.run_to(8000)
    assert [e.text for e in r.ends()] == ["Hej. Jag vill boka.", "Tack."]


def test_new_speech_after_an_ended_turn_ignores_its_late_finals():
    r = Rig()
    r.speak(0, 1500)
    r.final(1400, "Bord för alla.", end=1450)
    r.pause(1500)
    r.run_to(2200)
    assert len(r.ends()) == 1
    r.speak(2300, 500)                                        # the caller goes on before the orchestrator reset
    r.final(2350, "Bord för alla.", end=1450)                 # the old turn's final arrives late
    r.final(2900, "Och en barnstol.", end=2800)
    r.pause(2800)
    r.run_to(4000)
    assert [e.text for e in r.ends()] == ["Bord för alla.", "Och en barnstol."]
