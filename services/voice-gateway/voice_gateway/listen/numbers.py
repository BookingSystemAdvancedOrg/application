"""Swedish numbers as Transcribe writes them. Owner: builder 'listen'.

Live on booqy-dev (2026-11-20) Transcribe sv-SE wrote small numbers as clock
times: "bord för tre" came back as "bord för 15:00 15:00", "fyra personer" as
"16:00 personer". The agent then asked which time was meant, or held a table
for the wrong party. `normalise` rewrites the transcript with the context
before the agent (and the prefetch) reads it:

  * the same clock time twice in a row is said once ("15:00 15:00");
  * a clock time on the hour (13:00 to 24:00, or a bare 13 to 24) where a
    party size stands is the small number it was: after "för", "bord för",
    "vi är", "vi blir", "är vi", or before "personer", "pers", "gäster",
    "st" ("för 15:00" -> "för 3"), never after "klockan", "kl", "till",
    "vid", "runt", "halv", "kvart" or "från" (those are times);
  * the assistant's last question asked how many guests (AssistantContext
    topic "party") and the answer holds one bare clock time and no time
    word: it is the number of guests;
  * "halv 19:00" or "halv 19" is "halv 7" (a half hour is said with the
    hour on the clock face; the agent's own half hour rule reads it);
  * "för en." right before the number of guests is the caller's "för,
    ehm..." (live voice-A: "boka bord för en. Fyra personer"): the "en"
    goes, so the agent hears one party size, not two.

Times stay times: "klockan 19:00", "till 18", "vid 17:00" are not touched.
Only Swedish (the other languages' recognisers write numbers as digits).
"""

from __future__ import annotations

import re

_TIME = r"(?:(?P<h>1[3-9]|2[0-4])(?::00|\.00)?|(?P<s>[1-9]|1[0-2])(?::00|\.00))"   # 15:00, 15, 3:00 (live lang-A)
_PARTY_BEFORE = r"(?:bord\s+för|för|vi\s+är|vi\s+blir|är\s+vi|blir\s+vi|sällskap\s+på|sällskap\s+om)"
_PARTY_AFTER = r"(?:personer|person|pers|gäster|gäst|st|stycken)"
_TIME_WORDS = re.compile(r"(?<!\w)(klockan|kl|till|vid|runt|halv|kvart|från|tid|tiden)(?!\w)", re.I)

_HESITATION_EN = re.compile(rf"(?<!\w)(för)\s+(?:en|ett)[.,]\s+(?=\S+\s+{_PARTY_AFTER}(?!\w))", re.I)
_DUP = re.compile(r"(?<![\d:])(\d{1,2}(?::\d{2})?)(?:\s+\1)+(?![\d:])")
_BEFORE = re.compile(rf"(?<!\w)(?P<pre>{_PARTY_BEFORE})\s+{_TIME}(?![\d:\w]|\.\d)", re.I)
_AFTER = re.compile(rf"(?<![\w:.]){_TIME}(?P<post>\s+{_PARTY_AFTER})(?!\w)", re.I)
_HALF = re.compile(rf"(?<!\w)halv\s+{_TIME}(?![\d:\w]|\.\d)", re.I)
_BARE = re.compile(rf"(?<![\w:.]){_TIME}(?![\d:\w]|\.\d)")
_MARKED = re.compile(r"(?<!\w)(klockan|kl\.?|till|vid|runt|från)\s+$", re.I)


_WORDS = "en ett två tre fyra fem sex sju åtta nio tio elva tolv tretton fjorton femton sexton sjutton arton"
_OTHER_PARTY = re.compile(rf"(?<![\w:.])(?:\d{{1,2}}|{'|'.join(_WORDS.split())})\s+{_PARTY_AFTER}(?!\w)", re.I)


def _small(m: re.Match) -> str:
    return str(int(m.group("h")) - 12) if m.group("h") else str(int(m.group("s")))


def normalise(text: str, language: str | None, *, topic: str | None = None) -> str:
    """The transcript with Transcribe's clock-time numbers read back as the caller meant them (see above)."""
    if not text or str(language or "").split("-")[0].lower() != "sv":
        return text
    out = _DUP.sub(r"\1", text)
    out = _HESITATION_EN.sub(r"\1 ", out)
    out = _AFTER.sub(lambda m: m.group(0) if _MARKED.search(out[:m.start()])
                     else f"{_small(m)}{m.group('post')}", out)
    if not _OTHER_PARTY.search(out):       # "för 18:00 ... för fyra personer": the 18:00 is the time
        out = _BEFORE.sub(lambda m: f"{m.group('pre')} {_small(m)}", out)
    out = _HALF.sub(lambda m: f"halv {_small(m)}", out)
    if topic == "party" and not _TIME_WORDS.search(out) and not _OTHER_PARTY.search(out):
        bare = list(_BARE.finditer(out))
        if len(bare) == 1:
            m = bare[0]
            out = out[:m.start()] + _small(m) + out[m.end():]
    return out
