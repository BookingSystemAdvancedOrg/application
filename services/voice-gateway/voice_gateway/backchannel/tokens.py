"""Backchannel tokens per language. Owner: builder 'converse'.

TOKENS[lang] = ((token, weight, polly_text), ...): `token` is the key (in
events and the clip cache), `polly_text` what Polly reads (SSML body; the
builder tunes it so Elin says a soft "mm" and not "em em"; listen to each
clip, keep the ones that sound natural, owner decision 7 may replace
them with recorded human clips later). Languages without a row get none
(silence is better than a wrong-language acknowledgement).

pick_token(): weighted random with the RNG the controller owns, never the
same token twice in a row, "mm" style tokens most of the time.
"""

from __future__ import annotations

import random

TOKENS: dict[str, tuple[tuple[str, int, str], ...]] = {
    "sv": (("mm", 5, "Mm."), ("ja", 2, "Ja."), ("okej", 1, "Okej."), ("precis", 1, "Precis.")),
    "en": (("mm-hmm", 4, "Mm-hmm."), ("yeah", 2, "Yeah."), ("right", 1, "Right."), ("okay", 1, "Okay.")),
    "de": (("mhm", 4, "Mhm."), ("ja", 2, "Ja."), ("genau", 1, "Genau.")),
    "fr": (("mh", 4, "Mh."), ("oui", 2, "Oui."), ("d-accord", 1, "D'accord.")),
    "es": (("mhm", 4, "Mhm."), ("si", 2, "Sí."), ("vale", 1, "Vale.")),
    "it": (("mhm", 4, "Mhm."), ("si", 2, "Sì."), ("certo", 1, "Certo.")),
    "pl": (("mhm", 4, "Mhm."), ("tak", 2, "Tak."), ("jasne", 1, "Jasne.")),
    "tr": (("hi-hi", 3, "Hı hı."), ("evet", 2, "Evet."), ("tamam", 1, "Tamam.")),
    "ar": (("aha", 3, "آها."), ("naam", 2, "نعم."), ("tayyib", 1, "طيب.")),
}


def pick_token(language: str, rng: random.Random, last: str | None = None) -> str | None:
    raise NotImplementedError
