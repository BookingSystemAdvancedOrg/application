"""Openers: a short sound at the caller's turn end. Owner: builder 'converse'.

A person answering on the phone makes a sound within a quarter of a second
("Mm.", "Okej.", "Då ska vi se.") and then answers. The agent's first
sentence takes a Bedrock round (1.3 to 1.8 s measured live), so the gateway
plays one pre-rendered opener from the ClipCache right at the TurnEnd, in
the caller's language, and the reply follows. The opener is Booqy copy, never
the model's, and it never commits to an answer:

  kind       when                                              sv / en
  check      a lookup is certain (the brain will run the       "Jag kollar." / "I'll check."
             availability lookup before its first round:
             AgentBrain.lookup_planned), the only opener that
             says something is being done
  see        the caller asked something                       "Då ska vi se." / "Let's see."
  ack        any other turn with content (an answer, a          "Mm." "Okej." / "Mm." "Okay."
             correction, a name)
  (none)     a bare yes or no when the assistant asked nothing
             (the reply is quick), a language switch
             (the short disclosure comes first), the first
             turn of a call that has not heard the caller's
             language yet, an unsupported language

Varied, not every turn: an "ack" or "see" opener plays with
SpeakConfig.opener_chance, never the same line twice in a row; "check"
always plays (the lookup and the model round take the longest). A yes to a
read-back gets an "ack" (the write takes a model round and a tool). An
opener never starts with "Ja" or "Right" before a question is answered
(that would answer it before the tools did).

OPENERS[lang] = ((key, kind, polly_text), ...). The clips are rendered by
ClipCache.warm with the call's voice; one longer than MAX_OPENER_MS after
the trim is not kept (an opener must not sound like the reply).
"""

from __future__ import annotations

import random
import re
from typing import Literal

Kind = Literal["check", "see", "ack"]

# Read a little quicker than speech, as people say them.
_R = '<prosody rate="130%">{}</prosody>'

OPENERS: dict[str, tuple[tuple[str, Kind, str], ...]] = {
    "sv": (("mm", "ack", _R.format("Mm.")), ("okej", "ack", _R.format("Okej.")),
           ("se", "see", _R.format("Då ska vi se.")), ("se2", "see", _R.format("Ett ögonblick.")),
           ("kollar", "check", _R.format("Jag kollar.")), ("kollar2", "check", _R.format("Då kollar jag."))),
    "en": (("mm", "ack", _R.format("Mm.")), ("okay", "ack", _R.format("Okay.")),
           ("see", "see", _R.format("Let's see.")), ("see2", "see", _R.format("One moment.")),
           ("check", "check", _R.format("I'll check.")), ("check2", "check", _R.format("I will check."))),
    "de": (("mm", "ack", _R.format("Mhm.")), ("okay", "ack", _R.format("Okay.")),
           ("see", "see", _R.format("Mal sehen.")), ("check", "check", _R.format("Ich prüfe."))),
    "tr": (("hi-hi", "ack", _R.format("Hı hı.")), ("tamam", "ack", _R.format("Tamam.")),
           ("see", "see", _R.format("Bir bakayım.")), ("check", "check", _R.format("Bakıyorum."))),
    "pl": (("mhm", "ack", _R.format("Mhm.")), ("dobrze", "ack", _R.format("Dobrze.")),
           ("see", "see", _R.format("Zobaczmy.")), ("check", "check", _R.format("Sprawdzam."))),
    "ar": (("aha", "ack", _R.format("آها.")), ("hasanan", "ack", _R.format("حسناً.")),
           ("see", "see", _R.format("دعني أرى.")), ("check", "check", _R.format("سأتحقق."))),
    "es": (("vale", "ack", _R.format("Vale.")),
           ("see", "see", _R.format("A ver.")), ("check", "check", _R.format("Lo compruebo."))),
    "fr": (("mh", "ack", _R.format("Mh.")), ("daccord", "ack", _R.format("D'accord.")),
           ("see", "see", _R.format("Voyons.")), ("check", "check", _R.format("Je vérifie."))),
    "it": (("va-bene", "ack", _R.format("Va bene.")),
           ("see", "see", _R.format("Vediamo.")), ("check", "check", _R.format("Controllo."))),
    "fi": (("selva", "ack", _R.format("Selvä.")),
           ("see", "see", _R.format("Katsotaan.")), ("check", "check", _R.format("Tarkistan."))),
    "da": (("mm", "ack", _R.format("Mm.")), ("okay", "ack", _R.format("Okay.")),
           ("see", "see", _R.format("Lad mig se.")), ("check", "check", _R.format("Jeg tjekker."))),
    "nl": (("mm", "ack", _R.format("Mm.")), ("oke", "ack", _R.format("Oké.")),
           ("see", "see", _R.format("Even kijken.")), ("check", "check", _R.format("Ik kijk."))),
}
MAX_OPENER_MS = 650          # longer clips are dropped: the harness and a real caller take 700 ms of audio as a reply

_YES_NO = frozenset("ja nej nä japp jo jaa nää okej ok visst gärna absolut tack yes no yeah yep nope sure okay "
                    "please thanks".split())
_QUESTION_START = re.compile(r"^\s*(har|kan|finns|går|är|vad|vilken|vilka|när|hur|var|får|do|does|can|could|is|are|"
                             r"what|when|where|how|which|have|haben|gibt|kann|wann|wie|was)\b", re.I)
_TOKEN = re.compile(r"[^\W\d_]+|\d+")


def base_language(language: str | None) -> str:
    return str(language or "").split("-")[0].lower()


def lines(language: str, kind: Kind) -> list[str]:
    return [key for key, k, _ in OPENERS.get(base_language(language), ()) if k == kind]


def is_bare_yes_no(text: str) -> bool:
    toks = _TOKEN.findall(str(text or "").lower())
    return 0 < len(toks) <= 3 and all(t in _YES_NO for t in toks)


def is_question(text: str) -> bool:
    text = str(text or "").strip()
    return text.endswith("?") or bool(_QUESTION_START.match(text))


def kind_for(text: str, *, lookup: bool, awaiting_confirmation: bool, asked: bool = False) -> Kind | None:
    """Which kind of opener fits this caller turn (None: none). `asked`: the assistant's last reply asked a
    question, so a yes or no to it starts work (a hold, a write: live lang-A waited 2.2 s in silence after
    "Ja, tack")."""
    if lookup:
        return "check"
    if is_bare_yes_no(text):
        return "ack" if awaiting_confirmation or asked else None
    if is_question(text):
        return "see"
    return "ack" if len(_TOKEN.findall(str(text or ""))) >= 1 else None


class OpenerPicker:
    """Pure: the opener for a turn, varied (injected RNG)."""

    def __init__(self, rng: random.Random, chance: float) -> None:
        self.rng, self.chance = rng, chance
        self.last: str | None = None

    def pick(self, text: str, language: str, *, lookup: bool, awaiting_confirmation: bool,
             available=None, asked: bool = False) -> str | None:
        kind = kind_for(text, lookup=lookup, awaiting_confirmation=awaiting_confirmation, asked=asked)
        if kind is None:
            return None
        if kind != "check" and self.rng.random() >= self.chance:
            self.last = None                      # a turn without one: the next may use any line again
            return None
        keys = [k for k in lines(language, kind) if k != self.last]
        if available is not None:
            keys = [k for k in keys if available(language, k)]
        if not keys:
            return None
        key = self.rng.choice(keys)
        self.last = key
        return key


def polly_text(language: str, key: str) -> str | None:
    for k, _, text in OPENERS.get(base_language(language), ()):
        if k == key:
            return text
    return None


def kind_of(language: str, key: str) -> Kind | None:
    for k, kind, _ in OPENERS.get(base_language(language), ()):
        if k == key:
            return kind
    return None
