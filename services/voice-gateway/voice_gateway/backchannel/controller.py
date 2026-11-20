"""BackchannelController: when a short acknowledgement fits. Owner: builder 'converse'.

Pure (no I/O, injected RNG). Consumes events, returns BackchannelRequests;
the async shell (speak.speaker.Speaker) forwards them to the mixer.

Fires only when ALL hold (BackchannelConfig):
  * the call is older than min_call_ms and the orchestrator state is LISTENING
    (the caller holds the turn; never in GREETING, THINKING or SPEAKING);
  * the caller has spoken at least min_turn_speech_ms in this turn;
  * the caller is in a pause (VadPause) that has lasted pause_min_ms and
    not yet pause_max_ms (on_tick decides inside the window; a VadResume
    cancels), and the TurnDetector has not ended the turn; a TurnHold in
    this pause makes it a better moment (hesitation), not a requirement;
  * a meaningful clause since the last backchannel: at least
    min_words_since_last new words in the partials, and the tail is not a
    number (turn.classify_tail != "number") when no_number_tail;
  * not within cooldown_ms of the last one, at most max_per_turn per turn;
  * no assistant audio playing or queued, and the assistant stopped more
    than after_assistant_ms ago;
  * no read-back awaiting the caller's yes (AssistantContext
    .awaiting_confirmation) when no_during_confirmation;
  * a token exists for the caller's language (tokens.TOKENS).
At most one request per pause. Each request is counted for metrics
(time to first backchannel from VadStart).
"""

from __future__ import annotations

import random

from voice_gateway.config import BackchannelConfig
from voice_gateway.events import BackchannelRequest


class BackchannelController:
    def __init__(self, cfg: BackchannelConfig, *, rng: random.Random | None = None, language: str = "sv") -> None:
        self.cfg, self.rng, self.language = cfg, rng or random.Random(), language

    def on_event(self, event) -> list[BackchannelRequest]:
        """Any bus event (VadStart, VadPause, VadResume, SttPartial, TurnHold,
        TurnEnd, PlaybackStarted, PlaybackFinished, StateChanged,
        AssistantContext, LanguageDecided); unknown types are ignored."""
        raise NotImplementedError

    def on_tick(self, now_ms: int) -> list[BackchannelRequest]:
        raise NotImplementedError
