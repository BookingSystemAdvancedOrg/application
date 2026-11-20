"""Barge-in detector: duration, words, stop words, echo, backchannels (listen builder)."""

import pytest

from voice_gateway.config import BargeInConfig
from voice_gateway.events import SttPartial
from voice_gateway.listen.barge_in import BargeInDetector, is_backchannel


def _det(playback_db=-20.0, cfg=None):
    return BargeInDetector(cfg or BargeInConfig(), playback_dbfs_at=lambda t: playback_db)


def _frames(det, start, ms, *, caller_db=-25.0, speaking=True, playing=True, item_started=0, prob=0.9):
    """20 ms steps; returns the first BargeIn."""
    for t in range(start, start + ms, 20):
        ev = det.on_frame(t + 20, speech_prob=prob, caller_dbfs=caller_db, speaking=speaking,
                          assistant_playing=playing, item_started_ms=item_started)
        if ev:
            return ev
    return None


def _partial(at, text, lang="sv"):
    return SttPartial(at, "s", text, "", lang, 1)


@pytest.mark.parametrize("text,lang,expected", [
    ("mm", "sv", True), ("mhm", "sv", True), ("ja", "sv", True), ("okej", "sv", True), ("ja precis", "sv", True),
    ("mm-hmm", "en", True), ("Yeah, right", "en", True), ("uh-huh", "en", True), ("mmm", "sv", True),
    ("nej vänta", "sv", False), ("ja men jag vill ändra", "sv", False), ("", "sv", False), ("   ", "en", False),
    ("wait", "en", False), ("ok", "xx", True),
])
def test_is_backchannel(text, lang, expected):
    assert is_backchannel(text, lang) is expected


def test_sustained_caller_speech_over_playback_barges_in_after_350_ms():
    det = _det()
    ev = _frames(det, 1000, 2000, item_started=500)
    assert ev is not None and ev.reason == "duration"
    assert ev.speech_start_ms == 1000 - 0 and 1340 <= ev.at_ms <= 1380       # 350 ms of speech
    assert _frames(det, 3000, 1000, item_started=500) is None                # one BargeIn per utterance
    det.reset()
    assert _frames(det, 5000, 1000, item_started=4000) is not None


def test_short_bursts_a_cough_or_a_backchannel_do_not_barge_in():
    det = _det()
    assert _frames(det, 1000, 280) is None                                   # 280 ms of voice
    assert _frames(det, 1280, 400, speaking=False) is None
    assert _frames(det, 1700, 200) is None                                   # a new burst: the counter faded
    assert det.on_partial(_partial(2000, "mm"), assistant_playing=True) is None


def test_nothing_fires_when_the_assistant_is_silent_or_the_feature_is_off():
    det = _det()
    assert _frames(det, 0, 2000, playing=False) is None
    assert det.on_partial(_partial(10, "nej vänta"), assistant_playing=False) is None
    off = _det(cfg=BargeInConfig(enabled=False))
    assert _frames(off, 0, 2000) is None
    assert off.on_partial(_partial(10, "nej"), assistant_playing=True) is None


def test_first_150_ms_of_each_item_are_ignored():
    det = _det()
    assert _frames(det, 1000, 140, item_started=1000) is None
    det2 = _det()
    ev = _frames(det2, 1000, 600, item_started=1000)
    assert ev is not None and ev.at_ms >= 1000 + 150 + 350 - 40


def test_echo_does_not_count_but_a_louder_caller_does():
    det = _det(playback_db=-20)
    assert _frames(det, 1000, 3000, caller_db=-35) is None                  # 15 dB under the playback: our own echo
    det = _det(playback_db=-20)
    ev = _frames(det, 1000, 1000, caller_db=-25)                             # only 5 dB under: the caller talks
    assert ev is not None and ev.reason == "duration"


def test_a_partial_with_real_words_barges_in_after_150_ms_of_voice():
    det = _det()
    assert _frames(det, 1000, 160) is None
    ev = det.on_partial(_partial(1200, "nej jag vill ha tisdag istället"), assistant_playing=True)
    assert ev is not None and ev.reason in ("words", "stop_word")
    det = _det()
    assert det.on_partial(_partial(1200, "tisdag istället"), assistant_playing=True) is None   # no voice yet
    assert _frames(det, 1000, 100) is None
    assert det.on_partial(_partial(1100, "tisdag istället"), assistant_playing=True) is None   # under 150 ms
    assert _frames(det, 1100, 100) is None
    ev = det.on_partial(_partial(1200, "tisdag istället"), assistant_playing=True)
    assert ev is not None and ev.reason == "words" and ev.speech_start_ms <= 1020


def test_words_from_pure_echo_cannot_stop_the_assistant():
    det = _det(playback_db=-20)
    assert _frames(det, 1000, 600, caller_db=-38) is None                    # all echo-like voiced frames
    assert det.on_partial(_partial(1600, "din bokning gäller för fyra personer"), assistant_playing=True) is None


@pytest.mark.parametrize("text,lang", [("nej", "sv"), ("Vänta lite", "sv"), ("stopp", "sv"), ("wait a second", "en"),
                                       ("hold on", "en"), ("sorry", "en")])
def test_stop_words_stop_the_assistant_at_any_time(text, lang):
    det = _det()
    ev = det.on_partial(_partial(500, text, lang), assistant_playing=True)
    assert ev is not None and ev.reason == "stop_word" and ev.at_ms == 500


def test_stop_words_are_whole_words_only():
    det = _det()
    assert det.on_partial(_partial(500, "jag har ingen nejlika", "sv"), assistant_playing=True) is None
    assert det.on_partial(_partial(500, "no", "sv"), assistant_playing=True) is None     # "no" is English only


def test_backchannel_words_alone_never_fire_but_a_long_mm_hmm_is_still_speech():
    det = _det()
    assert _frames(det, 1000, 200) is None
    assert det.on_partial(_partial(1200, "mm-hmm", "en"), assistant_playing=True) is None
    assert det.on_partial(_partial(1250, "yeah right", "en"), assistant_playing=True) is None
    ev = _frames(det, 1200, 300)
    assert ev is not None and ev.reason == "duration"                        # 350 ms of voice is an interruption
