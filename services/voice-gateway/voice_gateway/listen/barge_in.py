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


def is_backchannel(text: str, language: str) -> bool:
    """True when every token of `text` is a backchannel token."""
    raise NotImplementedError


class BargeInDetector:
    def __init__(self, cfg: BargeInConfig, *, playback_dbfs_at: Callable[[int], float]) -> None:
        self.cfg, self.playback_dbfs_at = cfg, playback_dbfs_at

    def on_frame(self, now_ms: int, *, speech_prob: float, caller_dbfs: float, speaking: bool,
                 assistant_playing: bool, item_started_ms: int | None) -> BargeIn | None:
        raise NotImplementedError

    def on_partial(self, ev: SttPartial, *, assistant_playing: bool) -> BargeIn | None:
        raise NotImplementedError

    def reset(self) -> None:
        """A new assistant utterance began (or playback ended)."""
        raise NotImplementedError
