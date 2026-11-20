"""Backchannel tokens per language. Owner: builder 'converse'.

TOKENS[lang] = ((token, weight, polly_text), ...): `token` is the key (in
events and the clip cache), `polly_text` what Polly reads (SSML body; the
builder tunes it so Elin says a soft "mm" and not "em em"; listen to each
clip, keep the ones that sound natural, owner decision 7 may replace
them with recorded human clips later). Languages without a row get none
(silence is better than a wrong-language acknowledgement).

Two styles, as people use them on the phone:
  continuer     "mm", "mm-hmm", "mhm": go on, I am listening. The only
                style that fits a hesitation ("för, ehm...") or a pause
                in the middle of a clause.
  acknowledger  "ja", "okej", "precis", "right": got it. Fits after a
                complete clause, never mid-thought (it can sound like the
                assistant wants the turn).

pick_token(): weighted random with the RNG the controller owns, never the
same token twice in a row, "mm" style tokens most of the time.
"""

from __future__ import annotations

import random
from typing import Callable, Literal

# Quick, as people say them: Polly reads a lone word like a dictionary entry,
# so every token is sped up (checked live with Elin and Amy 2026-11-20:
# "Mm." 420 -> 320 ms after the trim; two-beat tokens 400 to 520 ms).
_Q = '<prosody rate="125%">{}</prosody>'
_A = '<prosody rate="118%">{}</prosody>'

TOKENS: dict[str, tuple[tuple[str, int, str], ...]] = {
    "sv": (("mm", 5, _Q.format("Mm.")), ("mhm", 2, _Q.format("Mhm.")), ("ja", 2, _A.format("Ja.")),
           ("okej", 1, _A.format("Okej.")), ("precis", 1, _Q.format("Precis."))),
    "en": (("mm-hmm", 4, _Q.format("Mm-hmm.")), ("mm", 2, _Q.format("Mm.")), ("yeah", 2, _A.format("Yeah.")),
           ("right", 1, _A.format("Right.")), ("okay", 1, _A.format("Okay."))),
    "de": (("mhm", 4, _Q.format("Mhm.")), ("mm", 2, _Q.format("Mm.")), ("ja", 2, _A.format("Ja.")),
           ("genau", 1, _A.format("Genau."))),
    "fr": (("mh", 4, _Q.format("Mh.")), ("mm", 2, _Q.format("Mm.")), ("oui", 2, _A.format("Oui.")),
           ("d-accord", 1, _Q.format("D'accord."))),
    "es": (("mhm", 4, _Q.format("Mhm.")), ("mm", 2, _Q.format("Mm.")), ("si", 2, _A.format("Sí.")),
           ("vale", 1, _A.format("Vale."))),
    "it": (("mhm", 4, _Q.format("Mhm.")), ("mm", 2, _Q.format("Mm.")), ("si", 2, _A.format("Sì.")),
           ("certo", 1, _A.format("Certo."))),
    "pl": (("mhm", 4, _Q.format("Mhm.")), ("mm", 2, _Q.format("Mm.")), ("tak", 2, _A.format("Tak.")),
           ("jasne", 1, _A.format("Jasne."))),
    "tr": (("hi-hi", 3, _Q.format("Hı hı.")), ("evet", 2, _A.format("Evet.")), ("tamam", 1, _A.format("Tamam."))),
    "ar": (("aha", 3, _Q.format("آها.")), ("naam", 2, _A.format("نعم.")), ("tayyib", 1, _A.format("طيب."))),
}

# Tokens that only say "go on" (safe in a hesitation or mid-clause).
CONTINUERS = frozenset({"mm", "mm-hmm", "mhm", "mh", "hi-hi", "aha"})

Style = Literal["continuer", "acknowledger"]


def base_language(language: str | None) -> str:
    """"sv-SE" -> "sv"; our codes pass unchanged."""
    return str(language or "").split("-")[0].split("_")[0].lower()


def tokens_for(language: str | None) -> tuple[tuple[str, int, str], ...]:
    return TOKENS.get(base_language(language), ())


def polly_text(language: str, token: str) -> str | None:
    for key, _, text in tokens_for(language):
        if key == token:
            return text
    return None


def pick_token(language: str, rng: random.Random, last: str | None = None, *, style: Style | None = None,
               available: Callable[[str, str], bool] | None = None,
               recent: tuple[str, ...] = ()) -> str | None:
    """A weighted random token for `language`, never `last` (None when
    nothing else is left). `style` limits the choice (continuer / acknowledger),
    `available(language, token)` drops tokens without a clip, `recent`
    (newest last) halves the weight of tokens said lately so a long call
    keeps varying."""
    lang = base_language(language)
    rows = [(t, w) for t, w, _ in TOKENS.get(lang, ())]
    if style == "continuer":
        rows = [(t, w) for t, w in rows if t in CONTINUERS]
    elif style == "acknowledger":
        rows = [(t, w) for t, w in rows if t not in CONTINUERS]
    if available is not None:
        rows = [(t, w) for t, w in rows if available(lang, t)]
    if not rows:
        return None
    rows = [(t, w) for t, w in rows if t != last]     # never twice in a row: none beats a repeat
    if not rows:
        return None
    weights = [w / (2 if t in recent else 1) for t, w in rows]
    return rng.choices([t for t, _ in rows], weights=weights, k=1)[0]
