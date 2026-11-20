"""Scenario scripts (data). Owner: builder 'harness'.

Voice A to F: ASSESSMENT.md test plan. Language A to H: MULTILINGUAL.md
section 5. Each caller step is spoken by a Polly caller voice in its
language (caller_audio.py), sent through LocalAdapter over a simulated
8 kHz mu-law line, at real-time pace. `pause_ms` inside a step is a real
pause in the caller audio (two Polly clips with silence between), which
is what scenario voice A needs. Steps run in order; a step waits for its
trigger first.

Triggers:
  "after_quiet"     the assistant has been silent quiet_ms (default 700)
  "during_reply"    the assistant has played at least `after_ms` of its
                    current reply (barge-in scenarios)
  "immediately"     right after the previous step's audio

Expectations are checked by asserts.py on the recorded events, the
transcripts (Transcribe of the assistant track, or the AgentSentence
texts) and the brain's fake tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Piece:
    """One stretch of caller speech. `gloss`: what the fake agent model (the
    application's scripted simulator, which only knows Swedish and English)
    reads for this text in fake mode; the fake Transcribe still hears `text`.
    Live mode ignores it."""
    text: str
    pause_after_ms: int = 0
    gloss: str = ""


@dataclass(frozen=True)
class Step:
    language: str
    pieces: tuple[Piece, ...]
    trigger: Literal["after_quiet", "during_reply", "immediately"] = "after_quiet"
    after_ms: int = 0
    label: str = ""


@dataclass(frozen=True)
class Expect:
    no_cutoff_before_ms: int | None = None          # no TurnEnd inside the caller's hesitation
    max_hesitation_acks: int | None = None          # at most this many backchannels in the hesitation
    party_size: int | None = None                   # the booking / hold made has this party size
    booked: bool | None = None                      # create_reservation succeeded (fake tools)
    booked_only_after_yes: bool = False
    backchannels_between: tuple[int, int] | None = None   # count range during the monologue
    backchannel_overlaps: int | None = 0
    barge_in_stop_max_ms: int | None = None
    readback_repeated_after_barge_in: bool = False
    final_language: str | None = None
    switches: int | None = None
    asked_language: bool | None = None
    unsupported_reply: bool | None = None
    callback_topic: str | None = None
    no_invented_times: bool = False
    fee_rule: bool = False                          # fee sentence, "Vill du ha mer information om det?", no amount
    readback_in_new_language_first: bool = False    # a switch before the yes: read-back repeated in the new language
    voice_of_each_sentence_matches_language: bool = True
    eos_to_first_audio_p50_max_ms: int | None = 1500


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    steps: tuple[Step, ...]
    expect: Expect
    tenant: dict = field(default_factory=dict)      # overrides of the fake tenant (guarantee rules, full slots)
    caller: str = "+46700000001"                    # fake number; its prefix picks LID candidates (lang-C: +46,
                                                    # so Persian is found by the hint + probe path, not the prefix)


SV, EN = "sv", "en"

VOICE = (
    Scenario("voice-A", "Hesitation", (
        Step(SV, (Piece("Jag skulle vilja boka bord för", 400), Piece("ehm", 1500),
                  Piece("fyra personer på fredag klockan sju."),)),),
        Expect(no_cutoff_before_ms=2500, max_hesitation_acks=1, party_size=4)),
    Scenario("voice-B", "No backchannel during a monologue (owner 2026-11-20)", (
        Step(SV, (Piece("Hej, jag vill boka ett bord på lördag för sex personer.", 450),
                  Piece("Vi firar min mammas sjuttioårsdag, så det vore fint med ett lugnt bord.", 450),
                  Piece("En av gästerna är allergisk mot nötter och en är vegetarian.", 450),
                  Piece("Helst runt halv sju om det går."),)),),
        Expect(backchannels_between=(0, 0), backchannel_overlaps=0)),
    Scenario("voice-C", "Correction", (
        Step(SV, (Piece("Jag vill boka ett bord i morgon klockan sju för fyra, nej förlåt, fem personer."),)),
        Step(SV, (Piece("Ja, det stämmer."),)),
        Step(SV, (Piece("Anna Svensson.", gloss="Jag heter Anna Svensson."),))),
        Expect(party_size=5, booked_only_after_yes=True)),
    Scenario("voice-D", "Interruption of the read-back", (
        Step(SV, (Piece("Jag vill boka bord för två i morgon klockan åtta."),)),
        Step(SV, (Piece("Vänta, jag menar klockan nio."),), trigger="during_reply", after_ms=1200),
        Step(SV, (Piece("Ja."),))),
        Expect(barge_in_stop_max_ms=300, readback_repeated_after_barge_in=True, booked_only_after_yes=True)),
    Scenario("voice-E", "Normal booking with the fee rule", (
        Step(SV, (Piece("Hej, jag vill boka ett bord för åtta personer på lördag klockan sju."),)),
        Step(SV, (Piece("Nej tack, det går bra."),)),
        Step(SV, (Piece("Ja, det stämmer."),)),
        Step(SV, (Piece("Karl Andersson.", gloss="Jag heter Karl Andersson."),)),
        Step(SV, (Piece("Ja, det stämmer."),))),
        Expect(fee_rule=True, booked=True, booked_only_after_yes=True),
        tenant={"guaranteeFromPartySize": 7}),
    Scenario("voice-F", "Unavailable time", (
        Step(SV, (Piece("Har ni ett bord för fyra på fredag klockan sju?"),)),
        Step(SV, (Piece("Nej, då ringer jag en annan dag."),))),
        Expect(no_invented_times=True, booked=False),
        tenant={"fullSlots": ["fri 19:00"]}),
)

LANGUAGE = (
    Scenario("lang-A", "Swedish booking", (
        Step(SV, (Piece("Hej, jag vill boka ett bord för tre i morgon klockan fem."),)),
        Step(SV, (Piece("Ja, tack."),)),
        Step(SV, (Piece("Lena Berg.", gloss="Jag heter Lena Berg."),)),
        Step(SV, (Piece("Ja, det stämmer."),))),
        Expect(final_language=SV, switches=0, booked=True)),
    Scenario("lang-B", "English after the Swedish greeting", (
        Step(EN, (Piece("Hi, I would like to book a table for two tomorrow at eight, please."),)),
        Step(EN, (Piece("Yes, that is right."),)),
        Step(EN, (Piece("John Smith.", gloss="My name is John Smith."),)),
        Step(EN, (Piece("Yes, that is right.", gloss="Yes."),))),
        Expect(final_language=EN, switches=1, booked=True)),
    Scenario("lang-C", "Persian booking (no AWS voice: unsupported + callback)", (
        Step("fa", (Piece("سلام، می‌خواهم برای فردا ساعت هشت یک میز برای چهار نفر رزرو کنم.",
                          gloss="Hello, I want to book a table for four tomorrow at 8."),)),
        Step("fa", (Piece("بله، لطفاً با من تماس بگیرید.", gloss="Yes."),))),
        Expect(unsupported_reply=True, callback_topic="language:fa", switches=1)),
    Scenario("lang-D", "Arabic booking", (
        Step("ar", (Piece("مرحبا، أريد حجز طاولة لشخصين غداً الساعة الثامنة مساءً.",
                          gloss="Hello, I want to book a table for two people tomorrow at 8 pm."),)),
        Step("ar", (Piece("نعم، هذا صحيح.", gloss="Yes, that is right."),)),
        Step("ar", (Piece("أحمد.", gloss="My name is Ahmed."),)),
        Step("ar", (Piece("نعم، صحيح.", gloss="Yes, that is right."),))),
        Expect(final_language="ar", switches=1, booked=True)),
    Scenario("lang-E", "Mid-call switch English to Swedish", (
        Step(EN, (Piece("Hello, do you have a table for four on Friday evening?"),)),
        Step(SV, (Piece("Förlåt, vi kan ta det på svenska istället. Klockan sju passar bra."),))),
        Expect(final_language=SV, switches=2)),
    Scenario("lang-F", "Uncertain language (short, mixed, the restaurant name)", (
        Step(EN, (Piece("Okay."),)),
        Step(SV, (Piece("Trattoria Roma, ja, kebab?"),))),
        Expect(final_language=SV, switches=0)),
    Scenario("lang-G", "Context kept across a switch before confirming", (
        Step(SV, (Piece("Jag vill boka bord för fyra på lördag klockan sju."),)),
        Step(EN, (Piece("Sorry, can we continue in English? Is that the table for four on Saturday?",
                        gloss="Saturday at 7 pm for four people."),)),
        Step(EN, (Piece("Yes, please book it.", gloss="Yes."),)),
        Step(EN, (Piece("Maria Lopez.", gloss="My name is Maria Lopez."),))),
        Expect(final_language=EN, party_size=4, booked_only_after_yes=True, readback_in_new_language_first=True)),
    Scenario("lang-H", "Interruption keeps the language", (
        Step(EN, (Piece("Hi, a table for two tomorrow at seven, please."),)),
        Step(EN, (Piece("Wait, sorry, make that half past seven."),), trigger="during_reply", after_ms=1200),
        Step(EN, (Piece("Yes."),))),
        Expect(final_language=EN, barge_in_stop_max_ms=300, readback_repeated_after_barge_in=True)),
)

EXTRA = (
    Scenario("lang-I", "German booking", (
        Step("de", (Piece("Guten Abend, ich möchte einen Tisch für zwei Personen morgen um acht Uhr reservieren.",
                          gloss="Hello, I want to book a table for two people tomorrow at 8 pm."),)),
        Step("de", (Piece("Ja, das stimmt.", gloss="Yes, that is right."),)),
        Step("de", (Piece("Hans Müller.", gloss="My name is Hans Mueller."),)),
        Step("de", (Piece("Ja, richtig.", gloss="Yes, that is right."),))),
        Expect(final_language="de", switches=1, booked=True)),
    Scenario("lang-J", "Turkish booking", (
        Step("tr", (Piece("Merhaba, yarın akşam saat sekizde iki kişilik bir masa ayırtmak istiyorum.",
                          gloss="Hello, I want to book a table for two people tomorrow at 8 pm."),)),
        Step("tr", (Piece("Evet, doğru.", gloss="Yes, that is right."),)),
        Step("tr", (Piece("Ayşe Yılmaz.", gloss="My name is Ayse Yilmaz."),)),
        Step("tr", (Piece("Evet, doğru.", gloss="Yes, that is right."),))),
        Expect(final_language="tr", switches=1, booked=True)),
    Scenario("lang-K", "Polish booking", (
        Step("pl", (Piece("Dzień dobry, chciałbym zarezerwować stolik dla dwóch osób jutro na godzinę dwudziestą.",
                          gloss="Hello, I want to book a table for two people tomorrow at 8 pm."),)),
        Step("pl", (Piece("Tak, zgadza się.", gloss="Yes, that is right."),)),
        Step("pl", (Piece("Jan Kowalski.", gloss="My name is Jan Kowalski."),)),
        Step("pl", (Piece("Tak, dokładnie.", gloss="Yes, that is right."),))),
        Expect(final_language="pl", switches=1, booked=True)),
)

ALL = VOICE + LANGUAGE + EXTRA
BY_KEY = {s.key: s for s in ALL}


def get(key: str) -> Scenario:
    """A scenario by key; a unique prefix or the short form ("A", "voice-a", "langC") also works."""
    if key in BY_KEY:
        return BY_KEY[key]
    norm = key.lower().replace("_", "-").replace(" ", "")
    for k, sc in BY_KEY.items():
        if k.lower() == norm or k.lower().replace("-", "") == norm.replace("-", ""):
            return sc
    hits = [sc for k, sc in BY_KEY.items() if k.lower().startswith(norm)]
    if len(hits) == 1:
        return hits[0]
    raise KeyError(f"unknown scenario {key!r}; known: {', '.join(BY_KEY)}")


def step_text(step: Step) -> str:
    return " ".join(p.text for p in step.pieces)


def validate(scenario: Scenario) -> list[str]:
    """Structural problems of one scenario (empty: fine). Used by the tests and the CLI."""
    problems = []
    if not scenario.steps:
        problems.append("no steps")
    for i, step in enumerate(scenario.steps):
        if not step.pieces:
            problems.append(f"step {i}: no pieces")
        if step.trigger == "during_reply" and step.after_ms <= 0:
            problems.append(f"step {i}: during_reply needs after_ms")
        if step.trigger == "during_reply" and i == 0:
            problems.append(f"step {i}: nothing to talk over yet")
        for j, piece in enumerate(step.pieces):
            if not piece.text.strip():
                problems.append(f"step {i} piece {j}: empty text")
            if "\u2013" in piece.text or "\u2014" in piece.text:
                problems.append(f"step {i} piece {j}: dash")
            if step.language not in ("sv", "en") and not piece.gloss:
                problems.append(f"step {i} piece {j}: a {step.language} piece needs a gloss for fake mode")
    return problems
