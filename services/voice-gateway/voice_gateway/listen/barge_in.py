"""Barge-in detection. Owner: builder 'listen'.

Pure: BargeInDetector looks at caller speech while assistant audio plays
(not backchannels of our own: a backchannel clip playing never counts as
"assistant playing" here) and fires one BargeIn per assistant utterance:

  * duration: VAD speech (during-playback threshold) sustained for
    BargeInConfig.min_speech_ms;
  * words: a partial with a word that is not a backchannel token
    (is_backchannel) after word_min_speech_ms;
  * stop_word: a partial containing a stop word ("nej", "vänta", "wait")
    at any time.
Not a barge-in:
  * echo: the caller frame level is at least echo_return_loss_db under the
    assistant playback level of the last echo_delay_max_ms (the mixer's
    level history, `playback_dbfs_at`), and no partial words arrived;
  * backchannels: partials made only of backchannel tokens ("mm", "ja",
    "okej", "aha", "mm-hmm", "yeah", "right") under min_speech_ms;
  * the first ignore_first_playback_ms of each assistant item.
Details: a voiced frame that is echo-like (level at least echo_return_loss_db
under the recent playback level) does not count towards the duration; a
words barge-in needs voiced speech for word_min_speech_ms of which at least
a third is not echo-like, so Transcribe transcribing our own echo (a
partial with real words) cannot stop the assistant by itself; a stop word
stops it at any time. One BargeIn per assistant utterance (reset() when
the next one starts).
Barge-in stop latency target: under 300 ms from the BargeIn to the last
assistant sample on the line (mixer flush), measured as PlaybackFlushed.stop_ms.
"""

from __future__ import annotations

from typing import Callable

from voice_gateway.config import BargeInConfig
from voice_gateway.events import BargeIn, SttPartial

BACKCHANNEL_WORDS: dict[str, frozenset[str]] = {
    "sv": frozenset("mm mhm mm-m aha ja jaa jo okej ok precis visst japp".split()),
    "en": frozenset("mm mhm mm-hmm uh-huh yeah yes yep right okay ok sure".split()),
    "de": frozenset("mhm ja genau okay ok".split()),
    "fr": frozenset("mh oui ouais d'accord ok".split()),
    "es": frozenset("mhm sí vale ya ok".split()),
}


import re

_TOKEN = re.compile(r"[^\W_]+(?:['\u2019-][^\W_]+)*")
_ALL_BACKCHANNELS = frozenset().union(*BACKCHANNEL_WORDS.values())


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(str(text or "").lower().replace("\u2019", "'"))


def is_backchannel(text: str, language: str) -> bool:
    """True when every token of `text` is a backchannel token."""
    toks = _tokens(text)
    if not toks:
        return False
    words = BACKCHANNEL_WORDS.get(language, _ALL_BACKCHANNELS)
    return all(t in words or re.fullmatch(r"m+h?m*", t) for t in toks)


def _has_stop_word(text: str, language: str, stops: dict) -> bool:
    toks = _tokens(text)
    joined = " " + " ".join(toks) + " "
    for stop in stops.get(language, ()):
        if (" " + stop + " ") in joined:
            return True
    return False


class BargeInDetector:
    def __init__(self, cfg: BargeInConfig, *, playback_dbfs_at: Callable[[int], float]) -> None:
        self.cfg, self.playback_dbfs_at = cfg, playback_dbfs_at
        self.reset()

    def reset(self) -> None:
        """A new assistant utterance began (or playback ended)."""
        self._fired = False
        self._voiced_ms = 0                  # non-echo speech ms over playback
        self._echo_ms = 0                    # echo-like voiced ms over playback
        self._run_start: int | None = None
        self._last_now: int | None = None
        self._speaking = False

    def _fire(self, now_ms: int, reason: str) -> BargeIn:
        self._fired = True
        start = self._run_start if self._run_start is not None else now_ms
        return BargeIn(now_ms, start, reason)    # type: ignore[arg-type]

    def on_frame(self, now_ms: int, *, speech_prob: float, caller_dbfs: float, speaking: bool,
                 assistant_playing: bool, item_started_ms: int | None) -> BargeIn | None:
        step = 20 if self._last_now is None else max(1, min(100, now_ms - self._last_now))
        self._last_now = now_ms
        self._speaking = speaking and assistant_playing
        cfg = self.cfg
        if not cfg.enabled or not assistant_playing or self._fired:
            if not assistant_playing:
                self._voiced_ms = self._echo_ms = 0
                self._run_start = None
            return None
        if item_started_ms is not None and now_ms - item_started_ms < cfg.ignore_first_playback_ms:
            return None
        if not speaking:
            self._voiced_ms = max(0, self._voiced_ms - step)       # short dips do not forget a real interruption
            if self._voiced_ms == 0:
                self._echo_ms, self._run_start = 0, None
            return None
        if self._run_start is None:
            self._run_start = now_ms - step
        reference = max(self.playback_dbfs_at(max(0, now_ms - d)) for d in range(0, cfg.echo_delay_max_ms + 1, 60))
        echo = caller_dbfs <= reference - cfg.echo_return_loss_db
        if echo:
            self._echo_ms += step
            return None
        self._voiced_ms += step
        if self._voiced_ms >= cfg.min_speech_ms:
            return self._fire(now_ms, "duration")
        return None

    def on_partial(self, ev: SttPartial, *, assistant_playing: bool) -> BargeIn | None:
        cfg = self.cfg
        if not cfg.enabled or not assistant_playing or self._fired or not ev.text.strip():
            return None
        if _has_stop_word(ev.text, ev.language, cfg.stop_words):
            return self._fire(ev.at_ms, "stop_word")
        if is_backchannel(ev.text, ev.language):
            return None
        total = self._voiced_ms + self._echo_ms
        if (self._speaking or self._voiced_ms > 0) and total >= cfg.word_min_speech_ms \
                and self._voiced_ms >= total / 3:
            return self._fire(ev.at_ms, "words")
        return None
