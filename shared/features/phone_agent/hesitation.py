"""The hesitation guard (ASSESSMENT stage 1, item 3): rules only, no model call.

The Connect line ends a caller's turn on a pause, so "jag skulle vilja boka bord
för, ehm" reaches Booqy as a whole utterance. When an utterance ends unfinished
(a filler word, or a word that cannot end a sentence such as a preposition or a
conjunction, in the call's language or the language the words show), the agent
answers only a very short backchannel ("Mm?", "Ja?") in the call's language
and keeps the fragment; the caller's next utterance is joined to it and the
agent loop sees the whole sentence. No booking state changes on a held turn
(the read-back guard counts agent turns, and a held fragment is not one).

At most MAX_HOLDS fragments in a row are held: after that the joined text goes
to the agent whatever it ends with, so a caller who always trails off is never
stuck. A fragment older than MAX_AGE_S is dropped (the caller started over).
"""

from __future__ import annotations

import re

MAX_HOLDS = 2
MAX_AGE_S = 30

# Words that only fill a pause, in any of the line's languages (never "mm", "mhm" or "aha": those answer).
FILLERS = frozenset((
    "eh ehh ehm ehmm em emm erm er öh öhm öhh äh ähm ähh uh uhh uhm um umm hmm hmmm euh euhm eeh eee ee "
    "yyy yyyy ııı ıı şey yani ano eto etto ettoo eto ehto эм э ээ эээ нуу مم اممم امم ام اه آه یعنی ممم "
    "えーと えっと えー あの あのー 음 어 그 저 उम्म अं").split())

# Words that cannot end a finished utterance, per language (lowercase). Separable verb particles that
# often end a sentence (German an/mit/zu/auf, Dutch op/aan/mee, Danish på/i/om) are left out on purpose;
# Turkish is left out (its postpositions end complete phrases: "dört kişi için").
DANGLING = {
    "sv": "och eller men för till på med om att i av åt från kl klockan vid som utan mot efter innan",
    "en": "and or but for to at with of the a an on in from about because than into onto",
    "de": "und oder aber für um am im vom beim den dem einen eine einem weil dass gegen ohne nach",
    "fr": "et ou mais pour à avec de du des le la les un une dans sur parce chez vers",
    "es": "y o pero para a con de del el la los las un una en por porque hacia",
    "it": "e o ma per a con di del della il lo gli un una in alle al alla dalle",
    "pl": "i oraz lub albo ale dla na o z w do przy że bo od",
    "ar": "و أو او لكن في على من إلى الى مع عن لأن الساعة",
    "fa": "و یا اما برای در با به از که ساعت چون",
    "bs": "i ili ali za na u sa s od do o kod jer",
    "hr": "i ili ali za na u sa s od do o kod jer",
    "sr": "i ili ali za na u sa s od do o kod jer",
    "id": "dan atau tapi untuk di ke dari dengan pada jam yang karena",
    "nl": "en of maar voor om met van de het een naar",
    "pt": "e ou mas para com de do da o um uma em no na por porque",
    "da": "og eller men for til med at af fra",
    "no": "og eller men for til med at av fra",
    "fi": "ja tai mutta että koska",
    "ru": "и или но для на в с к по о потому",
    "uk": "і або але для на в з до по про",
    "cs": "a nebo ale pro na v s k do o",
    "ca": "i o però per a amb de del el la un una en",
}
DANGLING = {lang: frozenset(words.split()) for lang, words in DANGLING.items()}

# These words after one of these verbs are a verb particle ("lägga till", "boka om", "check in"): the
# sentence is finished.
PARTICLES = {
    "sv": frozenset("till om på i med av åt från efter mot vid".split()),
    "en": frozenset("on in to with for at from about".split()),
}
PARTICLE_VERBS = {
    "sv": frozenset("lägga lägg la lade ta tar tog ha boka bokar ringa ringer ring komma kommer kom följa följer "
                    "följ hänga stänga håller hålla sätta sätter ändra gå går höra hör titta tittar tänka tänker "
                    "räkna räknar vänta väntar".split()),
    "en": frozenset("hold holding carry go come log put call check sign turn look pick find hang get".split()),
}

# A question that starts with a question word may end on a preposition ("who am I speaking with").
QUESTION_WORDS = frozenset((
    "vad vilken vilket vilka vem var när hur varför what which who whom where when how why was welche welcher "
    "welches wer wo wann wie warum quoi quel quelle qui où quand comment pourquoi qué cuál quién dónde cuándo "
    "cómo cosa quale chi dove quando come perché co który gdzie kiedy jak dlaczego wat welke wie waar wanneer "
    "hoe waarom").split())

# The backchannel, per language: one is picked by turn, so it varies.
BACKCHANNEL = {
    "sv": ("Mm?", "Ja?"),
    "en": ("Mm?", "Yes?"),
    "de": ("Ja?", "Mhm?"),
    "fr": ("Oui?", "Mm?"),
    "es": ("¿Sí?", "Mm?"),
    "it": ("Sì?", "Mm?"),
    "pl": ("Tak?", "Mhm?"),
    "tr": ("Evet?", "Hı hı?"),
    "ar": ("نعم؟", "أجل؟"),
    "fa": ("بله؟", "خب؟"),
    "bs": ("Da?", "Mhm?"),
    "hr": ("Da?", "Mhm?"),
    "sr": ("Da?", "Mhm?"),
    "id": ("Ya?", "Hm?"),
    "nl": ("Ja?", "Mm?"),
    "pt": ("Sim?", "Hum?"),
    "da": ("Ja?", "Mm?"),
    "no": ("Ja?", "Mm?"),
    "fi": ("Niin?", "Joo?"),
    "cs": ("Ano?", "Mhm?"),
    "ca": ("Sí?", "Mm?"),
    "ja": ("はい？", "ええ？"),
    "ko": ("네?", "음?"),
    "hi": ("जी?", "हाँ?"),
    "ru": ("Да?", "Угу?"),
    "uk": ("Так?", "Угу?"),
    "is": ("Já?", "Mm?"),
}

_TOKEN = re.compile(r"\w+(?:['’]\w+)?")      # digits too: "klockan 19" is finished
_OPEN_END = re.compile(r"(,|\.\.\.|…|\s-)\s*$")


def _tokens(text):
    return [t.casefold() for t in _TOKEN.findall(str(text or ""))]


def unfinished(text, langs=("sv",)) -> bool:
    """The utterance ends in a pause filler, an open punctuation mark or a
    word that cannot end a sentence in one of `langs`."""
    raw = str(text or "").strip()
    if not raw or raw.endswith("?"):
        return False
    tokens = _tokens(raw)
    if not tokens:
        return False
    last = tokens[-1]
    if last in FILLERS or _OPEN_END.search(raw):
        return True
    if tokens[0] in QUESTION_WORDS:
        return False
    before = tokens[-2] if len(tokens) > 1 else None
    for lang in dict.fromkeys(x for x in langs if x):
        particle = last in PARTICLES.get(lang, ()) and before in PARTICLE_VERBS.get(lang, ())
        if last in DANGLING.get(lang, ()) and not particle:
            return True
    return False


def backchannel(lang, n=0) -> str:
    options = BACKCHANNEL.get(lang) or BACKCHANNEL["en"]
    return options[int(n or 0) % len(options)]


def joined(fragment, text) -> str:
    return " ".join(x for x in (str(fragment or "").strip(), str(text or "").strip()) if x)
