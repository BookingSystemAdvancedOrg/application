"""Backchannel tokens and the controller (builder 'converse'): timing rules,
sparseness, variety, context (hesitation, numbers, questions, read-backs),
per-language tokens. Pure, no asyncio."""

import random
from collections import Counter

import pytest

from voice_gateway.backchannel import tokens as tk
from voice_gateway.backchannel.controller import BackchannelController, tail_kind
from voice_gateway.config import BackchannelConfig
from voice_gateway.events import (AssistantContext, CallState, LanguageAction, LanguageDecided, PlaybackFinished,
                                  PlaybackStarted, SttPartial, StateChanged, TurnEnd, TurnHold, VadPause, VadResume,
                                  VadStart)

CFG = BackchannelConfig()


# --- tokens ----------------------------------------------------------------------------------

def test_every_token_row_is_well_formed():
    for lang, rows in tk.TOKENS.items():
        keys = [t for t, _, _ in rows]
        assert len(keys) == len(set(keys)), lang
        assert any(t in tk.CONTINUERS for t in keys), f"{lang} needs a continuer"
        for token, weight, text in rows:
            assert weight > 0 and text.strip() and chr(0x2013) not in text and chr(0x2014) not in text


def test_pick_token_never_repeats_and_mostly_says_mm():
    rng = random.Random(1)
    last, picks = None, []
    for _ in range(2000):
        token = tk.pick_token("sv", rng, last)
        assert token != last
        picks.append(token)
        last = token
    counts = Counter(picks)
    assert set(counts) == {t for t, _, _ in tk.TOKENS["sv"]}
    continuers = sum(n for t, n in counts.items() if t in tk.CONTINUERS)
    assert continuers / len(picks) > 0.5


def test_pick_token_styles_languages_and_availability():
    rng = random.Random(3)
    for _ in range(50):
        assert tk.pick_token("sv", rng, style="continuer") in tk.CONTINUERS
        assert tk.pick_token("sv", rng, style="acknowledger") not in tk.CONTINUERS
        assert tk.pick_token("en", rng) in {t for t, _, _ in tk.TOKENS["en"]}
    assert tk.pick_token("sv-SE", rng, style="continuer") in {"mm", "mhm"}
    assert tk.pick_token("fa", rng) is None and tk.pick_token("", rng) is None
    assert tk.pick_token("sv", rng, available=lambda lang, t: t == "ja") == "ja"
    assert tk.pick_token("sv", rng, "ja", available=lambda lang, t: t == "ja") is None   # never a repeat
    assert tk.pick_token("tr", rng, "hi-hi", style="continuer") is None


def test_recent_tokens_weigh_less():
    rng = random.Random(5)
    plain = Counter(tk.pick_token("sv", rng) for _ in range(3000))
    rng = random.Random(5)
    damped = Counter(tk.pick_token("sv", rng, recent=("mm",)) for _ in range(3000))
    assert damped["mm"] < plain["mm"] * 0.8


# --- controller helpers ----------------------------------------------------------------------

class Call:
    """Drives a controller through a caller turn on a virtual timeline."""

    def __init__(self, cfg=CFG, seed=0, language="sv", available=None, start_ms=5000):
        self.c = BackchannelController(cfg, rng=random.Random(seed), language=language, available=available)
        self.now = start_ms
        self.fired = []
        self.words = 0
        self.seg = 0
        self.ev(StateChanged(self.now, CallState.SPEAKING, CallState.LISTENING))

    def ev(self, event):
        self.fired += self.c.on_event(event)

    def tick_to(self, until):
        while self.now < until:
            self.now += 20
            self.fired += self.c.on_tick(self.now)

    def speak(self, ms, text="", *, first=False):
        """Caller speech for `ms`, with partial text appended (word counts grow)."""
        if first:
            self.ev(VadStart(self.now + 100, self.now, 0.9))
            self.speech_ms = 0
        else:
            self.ev(VadResume(self.now + 60, 0))
        if text:
            self.seg += 1
            self.ev(SttPartial(self.now + ms // 2, f"s{self.seg}", text, text, "sv", 1))
        self.tick_to(self.now + ms)
        self.speech_ms += ms

    def pause(self, ms, hold=None):
        start = self.now
        self.ev(VadPause(start + 80, start, self.speech_ms))
        if hold:
            self.ev(TurnHold(start + 200, hold, 1500))
        self.tick_to(start + ms)

    def end_turn(self, text="x"):
        self.ev(TurnEnd(self.now, text, "sv", 0, self.now - 500, "silence"))


def monologue(call, clauses=4):
    """An 8 s story: clauses of about 2 s with 450 ms pauses between them."""
    texts = ["vi är fyra personer som vill komma", "på fredag kväll efter jobbet om det går",
             "och en av oss sitter i rullstol så vi behöver", "ett bord som är lätt att komma fram till"]
    call.speak(2000, texts[0], first=True)
    for text in texts[1:clauses]:
        call.pause(450)
        call.speak(2000, text)
    call.pause(450)


# --- controller: when ------------------------------------------------------------------------

def test_monologue_gets_one_or_two_backchannels_inside_pauses():
    for seed in range(40):
        call = Call(seed=seed)
        monologue(call)
        assert 1 <= len(call.fired) <= 2, seed
        for req in call.fired:
            assert req.language == "sv" and req.token in {t for t, _, _ in tk.TOKENS["sv"]}
        times = [r.at_ms for r in call.fired]
        assert all(b - a >= CFG.cooldown_ms for a, b in zip(times, times[1:]))


def test_fires_only_inside_the_pause_window_with_jitter():
    moments = set()
    for seed in range(30):
        call = Call(seed=seed)
        call.speak(3000, "vi är fyra personer som vill boka", first=True)
        pause_start = call.now
        call.pause(700)
        assert len(call.fired) == 1
        offset = call.fired[0].at_ms - pause_start
        assert CFG.pause_min_ms <= offset <= CFG.pause_max_ms
        moments.add(offset)
    assert len(moments) > 3            # not on the same beat every time


def test_never_while_the_caller_speaks_and_resume_cancels():
    call = Call()
    call.speak(3000, "vi är fyra personer som vill boka", first=True)
    call.ev(VadPause(call.now + 80, call.now, 3000))
    call.tick_to(call.now + 200)
    call.ev(VadResume(call.now, 200))          # speaks again before the window opened
    call.tick_to(call.now + 2000)
    assert call.fired == []


def test_short_turn_few_words_and_the_first_second_get_nothing():
    call = Call()
    call.speak(1500, "vi är fyra personer som vill boka", first=True)   # under min_turn_speech_ms
    call.pause(700)
    assert call.fired == []

    call = Call()
    call.speak(3000, "ja precis", first=True)                       # under min_words_since_last
    call.pause(700)
    assert call.fired == []

    call = Call(cfg=BackchannelConfig(min_turn_speech_ms=0), start_ms=0)
    call.speak(200, "vi är fyra personer", first=True)              # call younger than min_call_ms
    call.ev(VadPause(call.now + 80, call.now, 200))
    call.tick_to(min(call.now + 500, CFG.min_call_ms - 20))
    assert call.fired == []


def test_needs_listening_state():
    call = Call()
    call.ev(StateChanged(call.now, CallState.LISTENING, CallState.THINKING))
    call.speak(3000, "vi är fyra personer som vill boka", first=True)
    call.pause(700)
    assert call.fired == []
    fresh = BackchannelController(CFG, rng=random.Random(0))
    assert fresh.blocked(9000) == "state"


def test_cooldown_and_max_per_turn():
    cfg = BackchannelConfig(cooldown_ms=0)
    for seed in range(20):
        call = Call(cfg=cfg, seed=seed)
        call.speak(3000, "vi är fyra personer som vill boka", first=True)
        for i in range(6):
            call.pause(500)
            call.speak(1500, f"och sen vill vi också ha något mer {i}")
        assert len(call.fired) <= cfg.max_per_turn

    call = Call(seed=2)
    call.speak(3000, "vi är fyra personer som vill boka", first=True)
    call.pause(500)
    assert len(call.fired) == 1
    call.speak(500, "och vi kommer klockan sju i kväll")
    call.pause(500)                               # 1 s later: inside the 4 s cooldown
    assert len(call.fired) == 1


def test_counters_reset_on_turn_end():
    call = Call(cfg=BackchannelConfig(cooldown_ms=0, max_per_turn=1))
    call.speak(3000, "vi är fyra personer som vill boka", first=True)
    call.pause(500)
    call.end_turn()
    call.speak(3000, "och sen undrar jag om ni har parkering", first=True)
    call.pause(500)
    assert len(call.fired) == 2
    assert call.fired[0].token != call.fired[1].token


# --- controller: context ---------------------------------------------------------------------

def test_never_after_a_number():
    for text in ("vi blir fyra", "mitt nummer är noll sju noll", "klockan 18", "we are four"):
        call = Call(language="en" if text.startswith("we") else "sv")
        call.speak(3000, "hej jag skulle vilja boka ett bord " + text, first=True)
        call.pause(700)
        assert call.fired == [], text
    call = Call()
    call.speak(3000, "hej jag skulle vilja boka ett bord nu", first=True)
    call.pause(700, hold="number")
    assert call.fired == []


def test_never_after_the_callers_question():
    call = Call()
    call.speak(3000, "hej jag undrar om ni har ett bord ledigt ikväll?", first=True)
    call.pause(700)
    assert call.fired == []


def test_never_while_a_read_back_awaits_the_yes():
    call = Call()
    call.ev(AssistantContext(call.now, "yes_no", True, "sv"))
    call.speak(3000, "ja det stämmer men vänta lite nu", first=True)
    call.pause(700)
    assert call.fired == []
    call.ev(AssistantContext(call.now, "open", False, "sv"))
    call.end_turn()
    call.speak(3000, "och vi vill sitta vid fönstret om det går", first=True)
    call.pause(700)
    assert len(call.fired) == 1


def test_never_over_or_right_after_assistant_audio():
    call = Call()
    call.ev(PlaybackStarted(call.now, "t1-s0", "assistant", "sv", 2000))
    call.speak(3000, "vi är fyra personer som vill boka", first=True)
    call.pause(700)
    assert call.fired == []

    call = Call()
    call.speak(3000, "vi är fyra personer som vill boka", first=True)
    call.ev(PlaybackStarted(call.now - 900, "t1-s0", "assistant", "sv", 800))
    call.ev(PlaybackFinished(call.now - 100, "t1-s0", "assistant", 800, False))
    call.pause(700)                               # 100 to 800 ms after the assistant stopped
    assert call.fired == []


def test_hesitation_gets_a_continuer():
    for seed in range(40):
        call = Call(seed=seed)
        call.speak(3000, "vi skulle vilja komma på fredag för", first=True)
        call.pause(700, hold="dangling")
        assert len(call.fired) == 1 and call.fired[0].token in tk.CONTINUERS, seed
        assert call.fired[0].reason == "hesitation"
    call = Call()
    call.speak(3000, "vi skulle vilja komma på fredag ehm", first=True)
    call.pause(700)
    assert call.fired and call.fired[0].token in tk.CONTINUERS


def test_scenario_voice_a_hesitation_at_most_one_mm():
    """"för, ehm... fyra personer": the 1.5 s pause gets at most one mm."""
    call = Call()
    call.speak(2600, "hej jag vill boka ett bord för", first=True)
    call.pause(400, hold="dangling")
    call.speak(400, "hej jag vill boka ett bord för ehm")
    call.pause(1500, hold="filler")
    call.speak(900, "fyra personer")
    assert len(call.fired) <= 1
    assert all(r.token in tk.CONTINUERS for r in call.fired)


def test_language_follows_decisions_and_unknown_languages_stay_silent():
    call = Call()
    call.ev(LanguageDecided(call.now, LanguageAction.SWITCH, "en", "en", "strong", 1))
    call.speak(3000, "we would like to come on friday night", first=True)
    call.pause(700)
    assert len(call.fired) == 1 and call.fired[0].language == "en"
    assert call.fired[0].token in {t for t, _, _ in tk.TOKENS["en"]}

    call = Call(language="fa")
    call.speak(3000, "kalame kalame kalame kalame kalame", first=True)
    call.pause(700)
    assert call.fired == []


def test_only_tokens_with_a_clip():
    call = Call(available=lambda lang, token: token == "ja")
    call.speak(3000, "vi är fyra personer som vill boka", first=True)
    call.pause(700)
    assert [r.token for r in call.fired] == ["ja"]
    call = Call(available=lambda lang, token: False)
    call.speak(3000, "vi är fyra personer som vill boka", first=True)
    call.pause(700)
    assert call.fired == []


def test_note_dropped_undoes_the_count():
    call = Call(cfg=BackchannelConfig(max_per_turn=1))
    call.speak(3000, "vi är fyra personer som vill boka", first=True)
    call.pause(500)
    assert len(call.fired) == 1 and call.c.sent == 1
    call.c.note_dropped(call.fired[0])
    assert call.c.count_this_turn == 0 and call.c.last_ms is None and call.c.dropped == 1
    call.speak(500, "och vi kommer klockan sju i kväll med barn")
    call.pause(500)
    assert len(call.fired) == 2


def test_disabled():
    call = Call(cfg=BackchannelConfig(enabled=False))
    monologue(call)
    assert call.fired == []


def test_first_backchannel_latency_is_measured():
    call = Call(seed=4)
    monologue(call)
    assert call.c.first_ms is not None and call.c.first_ms >= CFG.min_turn_speech_ms


@pytest.mark.parametrize("text,kind", [("vi är fyra", "number"), ("klockan 18.30", "number"), ("", "empty"),
                                       ("jag vill ha ett bord för", "dangling"), ("ehm", "filler"),
                                       ("det låter bra", "complete")])
def test_tail_kind(text, kind):
    got = tail_kind(text, "sv")
    if kind in ("number", "empty"):
        assert got == kind
    else:
        assert got in {kind, "complete", "short_answer"}       # listen's classifier may be finer
