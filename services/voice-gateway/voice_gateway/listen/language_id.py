"""Language identification and the switch rules. Owner: builder 'listen'.

Evidence per caller utterance (MULTILINGUAL.md section 3, "Confidence and
hysteresis rules"; Transcribe identify-language replaces the local LID
model there: owner 2026-11-20, AWS only):
  * Transcribe LID scores: the first utterance of the call runs on an
    identify-language stream (candidates_for()), later utterances only on
    a probe (Transcriber.probe) when the text hint or the model tag points
    elsewhere, or the ASR confidence is under LanguageConfig.low_asr_confidence;
  * the text hint: shared language.hint() on the transcript, with tenant
    proper nouns (restaurant, location, menu names) stripped first
    (`strip_names`) so "Trattoria Roma" or "kebab" never count;
  * the explicit request: shared language.asked_for_language();
  * the model tag of the previous reply (follow()).

LanguageTracker is pure (no I/O). It implements rules 1 to 9 with the
LanguageConfig numbers and returns one LanguageDecided per utterance.
The brain stays the authority on the call row: when session.call_turn
switches on its own (the model tag, language.decide) the orchestrator
calls `follow()` so both agree, and a switch decided here is applied with
AgentBrain.set_language before the turn runs. Switch cap: one count shared
with the call row (`languageSwitches`, language.MAX_CONNECT_SWITCHES).
Unsupported (rule 8): a confident language outside languages.SPOKEN
(fa, bs, id, so ...) gives LanguageAction.UNSUPPORTED with the code; the
brain answers with the shared fixed sentences and the callback.
"""

from __future__ import annotations

from dataclasses import dataclass

from voice_gateway.config import LanguageConfig
from voice_gateway.events import LanguageDecided


@dataclass(frozen=True)
class UtteranceInfo:
    at_ms: int
    text: str
    speech_ms: int
    asr_language: str                                  # language of the stream that produced the text
    asr_confidence: float | None = None
    lid_scores: tuple[tuple[str, float], ...] = ()     # our codes, from Transcribe LID, may be empty
    first: bool = False                                # first caller utterance of the call


def candidates_for(caller_e164: str | None, cfg: LanguageConfig, *, current: str = "sv") -> tuple[str, ...]:
    """Up to 5 our-codes for an identify-language stream: cfg.always_candidates,
    then the caller's country candidates (longest prefix first), then
    cfg.default_extra_candidates; only languages Transcribe can hear
    (languages.to_transcribe), no duplicates, `current` first."""
    raise NotImplementedError


def strip_names(text: str, names: tuple[str, ...]) -> str:
    """The text without the tenant's proper nouns (case-insensitive, whole words)."""
    raise NotImplementedError


class LanguageTracker:
    def __init__(self, cfg: LanguageConfig, *, spoken: tuple[str, ...], names: tuple[str, ...] = (),
                 current: str = "sv", switches: int = 0) -> None:
        self.cfg, self.spoken, self.names = cfg, spoken, names
        self.current, self.switches = current, switches

    def observe(self, utt: UtteranceInfo) -> LanguageDecided:
        """One caller utterance -> the decision (rules 1 to 9)."""
        raise NotImplementedError

    def wants_probe(self, utt: UtteranceInfo) -> tuple[str, ...] | None:
        """Candidates for a LID probe on this utterance, or None (no probe):
        the hint or the explicit request names another language, or the ASR
        confidence is low, and no LID scores are present yet."""
        raise NotImplementedError

    def follow(self, code: str, at_ms: int, reason: str = "model_tag") -> LanguageDecided | None:
        """The brain switched (call row): adopt it, count it, start the
        stay-turns hold. None when it already was the current language."""
        raise NotImplementedError

    @property
    def locked(self) -> bool:
        """max_auto_switches reached: only explicit requests switch now."""
        raise NotImplementedError
