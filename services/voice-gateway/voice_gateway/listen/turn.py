"""Semantic turn detection (end of the caller's turn). Owner: builder 'listen'.

Pure: TurnDetector consumes VAD events, partials, finals and clock ticks
and decides when the caller's turn is over. The silence threshold is
chosen per moment from the TAIL of the current transcript and the
assistant context (TurnConfig):

  tail kind (classify_tail)          threshold
  complete clause                    base_silence_ms
  short answer after a yes/no or     short_answer_silence_ms
    number question ("ja", "fyra")
  after an open question             open_question_silence_ms (minimum)
  filler ("ehm", "öh", "um", "uh")   filler_silence_ms
  dangling word ("för", "och",       dangling_silence_ms
    "till", "klockan", "and", "for")
  number being spoken ("noll sju     number_silence_ms
    tre", "18", "halv")
  never more than max_silence_ms; a TurnHold is published whenever the
  threshold rises above base_silence_ms (metrics, backchannel).

Turn end: silence since VadPause >= threshold -> wait up to
SttConfig.final_wait_ms for the SttFinal covering the speech (a final
already in: end at once) -> TurnEnd with the final text (else the last
partial, reason "silence"). max_utterance_ms of speech -> TurnEnd
"max_length". Speech under min_speech_ms with no words is noise: no turn.
Several finals in one turn are joined with a space. Word lists per
language live in TAILS (sv and en complete; de, fr, es, ar, pl, tr, it
best effort, reviewed later), matched on lowercase tokens.
"""

from __future__ import annotations

from typing import Literal

from voice_gateway.config import SttConfig, TurnConfig
from voice_gateway.events import AssistantContext, SttFinal, SttPartial, TurnEnd, TurnHold, VadPause, VadResume, VadStart

TailKind = Literal["empty", "complete", "short_answer", "filler", "dangling", "number"]

TAILS: dict[str, dict[str, frozenset[str]]] = {
    "sv": {
        "filler": frozenset("eh ehm öh öhm äh ähm hm hmm mm alltså typ liksom asså".split()),
        "dangling": frozenset("för och till på i med att som men eller klockan kl vid om från den det en ett "
                              "är var vill ska skulle kan har hade jag vi".split()),
        "number": frozenset("noll ett en två tre fyra fem sex sju åtta nio tio elva tolv tretton fjorton femton "
                            "sexton sjutton arton nitton tjugo trettio fyrtio femtio halv kvart".split()),
        "short_answer": frozenset("ja nej japp nä absolut precis gärna okej visst stämmer".split()),
    },
    "en": {
        "filler": frozenset("uh um er erm hm hmm mm like".split()),
        "dangling": frozenset("for and to at on in with that but or the a an is was would will can i we "
                              "about from of".split()),
        "number": frozenset("zero oh one two three four five six seven eight nine ten eleven twelve thirteen "
                            "fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty "
                            "half quarter".split()),
        "short_answer": frozenset("yes no yeah yep nope sure right correct okay ok".split()),
    },
}


def classify_tail(text: str, language: str,
                  asked: Literal["yes_no", "open", "number", None] = None) -> TailKind:
    """The kind of the transcript's end (digits count as a number)."""
    raise NotImplementedError


class TurnDetector:
    def __init__(self, cfg: TurnConfig, stt: SttConfig, *, language: str = "sv") -> None:
        self.cfg, self.stt, self.language = cfg, stt, language

    def on_vad(self, ev: VadStart | VadPause | VadResume) -> list[TurnHold | TurnEnd]:
        raise NotImplementedError

    def on_partial(self, ev: SttPartial) -> list[TurnHold | TurnEnd]:
        raise NotImplementedError

    def on_final(self, ev: SttFinal) -> list[TurnHold | TurnEnd]:
        raise NotImplementedError

    def on_tick(self, now_ms: int) -> list[TurnHold | TurnEnd]:
        """Called every frame (20 ms); fires TurnEnd when due."""
        raise NotImplementedError

    def set_context(self, ctx: AssistantContext) -> None:
        raise NotImplementedError

    def set_language(self, language: str) -> None:
        raise NotImplementedError

    def threshold_ms(self) -> int:
        """The silence threshold that applies right now."""
        raise NotImplementedError

    def reset(self) -> None:
        """After a TurnEnd (or a barge-in turn handled elsewhere)."""
        raise NotImplementedError
