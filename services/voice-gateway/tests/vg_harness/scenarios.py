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
    text: str
    pause_after_ms: int = 0


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
    Scenario("voice-B", "Backchannel during a monologue", (
        Step(SV, (Piece("Hej, jag vill boka ett bord på lördag för sex personer.", 450),
                  Piece("Vi firar min mammas sjuttioårsdag, så det vore fint med ett lugnt bord.", 450),
                  Piece("En av gästerna är allergisk mot nötter och en är vegetarian.", 450),
                  Piece("Helst runt halv sju om det går."),)),),
        Expect(backchannels_between=(1, 2), backchannel_overlaps=0)),
    Scenario("voice-C", "Correction", (
        Step(SV, (Piece("Jag vill boka ett bord i morgon klockan sju för fyra, nej förlåt, fem personer."),)),
        Step(SV, (Piece("Ja, det stämmer."),)),
        Step(SV, (Piece("Anna Svensson."),))),
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
        Step(SV, (Piece("Karl Andersson."),))),
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
        Step(SV, (Piece("Hej, jag vill boka ett bord för tre i morgon klockan sex."),)),
        Step(SV, (Piece("Ja, tack."),)),
        Step(SV, (Piece("Lena Berg."),))),
        Expect(final_language=SV, switches=0, booked=True)),
    Scenario("lang-B", "English after the Swedish greeting", (
        Step(EN, (Piece("Hi, I would like to book a table for two tomorrow at eight, please."),)),
        Step(EN, (Piece("Yes, that is right."),)),
        Step(EN, (Piece("John Smith."),))),
        Expect(final_language=EN, switches=1, booked=True)),
    Scenario("lang-C", "Persian booking (no AWS voice: unsupported + callback)", (
        Step("fa", (Piece("سلام، می‌خواهم برای فردا ساعت هشت یک میز برای چهار نفر رزرو کنم."),)),
        Step("fa", (Piece("بله، لطفاً با من تماس بگیرید."),))),
        Expect(unsupported_reply=True, callback_topic="language:fa", switches=0)),
    Scenario("lang-D", "Arabic booking", (
        Step("ar", (Piece("مرحبا، أريد حجز طاولة لشخصين غداً الساعة الثامنة مساءً."),)),
        Step("ar", (Piece("نعم، هذا صحيح."),)),
        Step("ar", (Piece("أحمد."),))),
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
        Step(EN, (Piece("Sorry, can we continue in English? Is that the table for four on Saturday?"),)),
        Step(EN, (Piece("Yes, please book it."),)),
        Step(EN, (Piece("Maria Lopez."),))),
        Expect(final_language=EN, party_size=4, booked_only_after_yes=True, readback_repeated_after_barge_in=False)),
    Scenario("lang-H", "Interruption keeps the language", (
        Step(EN, (Piece("Hi, a table for two tomorrow at seven, please."),)),
        Step(EN, (Piece("Wait, sorry, make that half past seven."),), trigger="during_reply", after_ms=1200),
        Step(EN, (Piece("Yes."),))),
        Expect(final_language=EN, barge_in_stop_max_ms=300, readback_repeated_after_barge_in=True)),
)

ALL = VOICE + LANGUAGE
