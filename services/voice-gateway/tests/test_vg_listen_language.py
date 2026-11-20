"""Language identification, hysteresis and the switch rules (listen builder).
Scenarios follow MULTILINGUAL.md rules 1 to 9 and the language scenarios A to H."""

import pytest

from voice_gateway import languages
from voice_gateway.config import LanguageConfig
from voice_gateway.events import LanguageAction as A
from voice_gateway.listen.language_id import (LanguageTracker, UtteranceInfo, candidates_for, explicit_targets,
                                              strip_names)

EN_TEXT = "Hi I would like to book a table for four people on Friday"
SV_TEXT = "Hej jag skulle vilja boka ett bord för fyra personer på fredag"
FA_TEXT = "سلام من می‌خواهم یک میز برای چهار نفر رزرو کنم"


def tracker(**kw):
    kw.setdefault("spoken", languages.SPOKEN)
    return LanguageTracker(LanguageConfig(), **kw)


def utt(text, *, lid=(), speech=3000, asr="sv", conf=0.9, first=False, at=0):
    return UtteranceInfo(at, text, speech, asr, conf, tuple(lid), first)


# --- candidates and names ----------------------------------------------------------------------

def test_candidates_are_ours_transcribable_deduplicated_and_at_most_five():
    cfg = LanguageConfig()
    assert candidates_for("+46701234567", cfg) == ("sv", "en", "ar", "de", "pl")
    assert candidates_for("+98912000111", cfg) == ("sv", "en", "fa", "ar", "de")      # country first
    assert candidates_for("+387611222333", cfg, current="en")[:3] == ("en", "sv", "bs")
    assert candidates_for(None, cfg)[:2] == ("sv", "en")
    for caller in ("+46", "+98", "+41", "+1", "+999", None):
        c = candidates_for(caller, cfg)
        assert 2 <= len(c) <= 5 and len(set(c)) == len(c) and all(languages.to_transcribe(x) for x in c)
    assert candidates_for("+41791112233", cfg)[2:5] == ("de", "fr", "it")             # longest prefix, in order


def test_candidates_drop_languages_transcribe_cannot_hear():
    cfg = LanguageConfig(default_extra_candidates=("xx", "ar"), country_candidates={"+1": ("zz",)})
    assert candidates_for("+15551234", cfg) == ("sv", "en", "ar")


def test_strip_names_removes_proper_nouns_whole_words_only():
    names = ("Trattoria Roma", "kebab", "Storgatan")
    assert strip_names("Hej Trattoria Roma tack", names) == "Hej tack"
    assert strip_names("Kebab på Storgatan", names) == "på"
    assert strip_names("Romantic kebabs", names) == "Romantic kebabs"
    assert strip_names("jag vill till Roma", names) == "jag vill till"                 # a name part on its own
    assert strip_names("bord för två", ("Bord för två",)) == ""
    assert strip_names("hello", ()) == "hello"


@pytest.mark.parametrize("text,codes", [
    ("Can we speak English please", ["en"]), ("kan vi prata engelska", ["en"]), ("auf Deutsch bitte", ["de"]),
    ("بالفارسی لو سمحت", ["fa"]), ("ممكن بالعربي", ["ar"]), ("na bosanskom molim", ["bs"]),
    ("bahasa indonesia please", ["id"]), ("soomaali ma ku hadli kartaa", ["so"]), ("svenska eller engelska", ["sv", "en"]),
    ("hej jag vill boka", []), ("the bahasa of the street", []),
])
def test_explicit_requests_name_their_language(text, codes):
    assert explicit_targets(text) == codes


# --- rule 1: explicit -------------------------------------------------------------------------

def test_explicit_request_switches_at_once_at_any_length_and_without_scores():
    t = tracker()
    d = t.observe(utt("engelska tack", speech=500, conf=0.5))
    assert (d.action, d.candidate, d.current, d.reason) == (A.SWITCH, "en", "en", "explicit")
    assert t.switches == 0                                    # not an automatic switch


def test_explicit_request_for_the_current_language_keeps_it():
    t = tracker(current="en")
    d = t.observe(utt("can we speak english", asr="en"))
    assert d.action == A.KEEP and d.reason == "explicit_same" and d.current == "en"


def test_explicit_request_beats_stay_and_lock_but_an_unsupported_one_gets_the_fixed_answer():
    t = tracker(switches=3)
    assert t.locked
    d = t.observe(utt("auf Deutsch bitte", speech=400))
    assert d.action == A.SWITCH and d.current == "de"
    d = t.observe(utt("soomaali ma ku hadli kartaa"))
    assert d.action == A.UNSUPPORTED and d.candidate == "so" and d.current == "de"


# --- rules 2 and 4: strong evidence, never on short evidence ------------------------------------

def test_scenario_a_swedish_stays_swedish():
    t = tracker()
    d = t.observe(utt(SV_TEXT, lid=[("sv", 0.97), ("en", 0.02)], first=True))
    assert d.action == A.KEEP and d.current == "sv" and d.switches == 0


def test_scenario_b_english_after_the_greeting_switches_on_the_first_utterance():
    t = tracker()
    d = t.observe(utt(EN_TEXT, lid=[("en", 0.96), ("sv", 0.03)], asr="en", first=True))
    assert (d.action, d.current, d.reason, d.switches) == (A.SWITCH, "en", "first_utterance", 1)


@pytest.mark.parametrize("text,speech", [("okay", 500), ("thank you", 1200), ("Trattoria Roma kebab", 3000),
                                         ("Hi I would", 900)])
def test_rule_4_short_or_wordless_evidence_never_switches(text, speech):
    t = tracker(names=("Trattoria Roma", "kebab"))
    d = t.observe(utt(text, lid=[("en", 0.99)], speech=speech, asr="en"))
    assert d.action == A.KEEP and d.current == "sv"


def test_strong_lid_needs_the_hint_not_to_disagree():
    t = tracker()
    d = t.observe(utt(SV_TEXT, lid=[("en", 0.95)], asr="en"))        # LID says en, but the words are Swedish
    assert d.action == A.KEEP


def test_strong_switch_to_a_language_with_a_hint_agreement_later_in_the_call():
    t = tracker()
    t.observe(utt(SV_TEXT, lid=[("sv", 0.9)], first=True))
    d = t.observe(utt(EN_TEXT, lid=[("en", 0.92)], asr="sv"))
    assert d.action == A.SWITCH and d.reason == "strong"


# --- rule 3: accumulated ------------------------------------------------------------------------

def test_two_moderate_utterances_in_a_row_switch_on_the_second():
    t = tracker()
    d1 = t.observe(utt("I would like a table", lid=[("en", 0.7), ("sv", 0.3)], speech=1100, asr="en"))
    d2 = t.observe(utt("for four people tonight", lid=[("en", 0.72), ("sv", 0.2)], speech=1200, asr="en"))
    assert d1.action == A.KEEP and d2.action == A.SWITCH and d2.reason == "accumulated"


def test_accumulated_needs_consecutive_utterances():
    t = tracker()
    t.observe(utt("I would like a table", lid=[("en", 0.7), ("sv", 0.3)], speech=1100, asr="en"))
    t.observe(utt("ett bord för fyra personer", lid=[("sv", 0.9)]))
    d = t.observe(utt("for four people tonight", lid=[("en", 0.72), ("sv", 0.2)], speech=1200, asr="en"))
    assert d.action == A.KEEP


# --- rule 5: uncertain -> ask once ---------------------------------------------------------------

def test_uncertain_asks_once_and_the_answer_is_an_explicit_request():
    t = tracker()
    d = t.observe(utt("I would like to book a table", lid=[("en", 0.58), ("sv", 0.4)], asr="en"))
    assert (d.action, d.candidate, d.reason) == (A.ASK, "en", "uncertain")
    d = t.observe(utt("yes please", lid=[("sv", 0.5), ("en", 0.45)]))
    assert d.action == A.SWITCH and d.current == "en" and d.reason == "asked"
    t2 = tracker()
    t2.observe(utt("I would like to book a table", lid=[("en", 0.58), ("sv", 0.4)], asr="en"))
    d = t2.observe(utt("nej tack", lid=[("sv", 0.9)]))
    assert d.action == A.KEEP and d.reason == "asked_declined" and d.current == "sv"
    d = t2.observe(utt("I would like to book a table", lid=[("en", 0.58), ("sv", 0.4)], asr="en"))
    assert d.action != A.ASK                                  # asked once per candidate


def test_a_clear_gap_between_the_two_languages_is_not_uncertain():
    t = tracker()
    d = t.observe(utt("I would like to book", lid=[("en", 0.62), ("sv", 0.2)], speech=1000, asr="en"))
    assert d.action == A.KEEP


# --- rule 6: no flip-flop ----------------------------------------------------------------------

def _switch_to_en(t):
    return t.observe(utt(EN_TEXT, lid=[("en", 0.95)], asr="en", first=True))


def test_stay_two_caller_turns_after_a_switch_then_allow_another():
    t = tracker()
    _switch_to_en(t)
    sv = lambda: t.observe(utt(SV_TEXT, lid=[("sv", 0.95)]))
    assert sv().action == A.KEEP and sv().action == A.KEEP and t.current == "en"      # two turns of stay
    d = sv()
    assert d.action == A.SWITCH and d.current == "sv" and d.switches == 2


def test_going_back_to_a_language_just_left_needs_an_explicit_request():
    t = tracker()
    _switch_to_en(t)
    t.observe(utt(EN_TEXT, lid=[("en", 0.95)], asr="en"))
    t.observe(utt(EN_TEXT, lid=[("en", 0.95)], asr="en"))
    d = t.observe(utt("jag vill prata svenska", lid=[("sv", 0.4)]))
    assert d.action == A.SWITCH and d.reason == "explicit"


def test_max_three_automatic_switches_then_only_explicit():
    t = tracker()
    seq = [("en", EN_TEXT, "en"), ("sv", SV_TEXT, "sv"), ("de", "Ich möchte gern einen Tisch für vier Personen "
                                                           "am Freitag reservieren bitte", "de")]
    for code, text, asr in seq:
        for _ in range(3):                                    # let the stay turns pass
            t.observe(utt(text, lid=[(code, 0.95)], asr=asr))
    assert t.switches == 3 and t.locked and t.current == "de"
    d = t.observe(utt(EN_TEXT, lid=[("en", 0.99)], asr="en"))
    assert d.action == A.KEEP
    d = t.observe(utt("can we speak english", asr="en"))
    assert d.action == A.SWITCH and d.current == "en"


def test_follow_adopts_the_brains_switch_counts_it_and_holds():
    t = tracker()
    assert t.follow("sv", 10) is None
    d = t.follow("de", 1000)
    assert (d.action, d.current, d.reason, d.switches) == (A.SWITCH, "de", "model_tag", 1)
    assert t.follow("de", 1100) is None
    assert t.observe(utt(SV_TEXT, lid=[("sv", 0.95)])).action == A.KEEP                # the stay hold applies


# --- rules 7, 8, 9 ------------------------------------------------------------------------------

def test_scenario_c_persian_is_detected_but_unsupported():
    t = tracker(names=())
    d = t.observe(utt(FA_TEXT, lid=[("fa", 0.93), ("ar", 0.05)], asr="fa", first=True))
    assert d.action == A.UNSUPPORTED and d.candidate == "fa" and d.current == "sv" and d.switches == 0
    t2 = tracker()
    d = t2.observe(utt(FA_TEXT, speech=3000, asr="sv", conf=0.9))                        # no LID, the script hint
    assert d.action == A.UNSUPPORTED and d.candidate == "fa"


def test_a_confident_single_word_of_an_unsupported_language_is_not_enough():
    t = tracker()
    assert t.observe(utt("salam", lid=[("fa", 0.95)], speech=700, asr="fa")).action == A.KEEP


def test_scenario_d_arabic_switches_to_a_supported_language():
    t = tracker()
    d = t.observe(utt("مرحبا أريد أن أحجز طاولة لأربعة أشخاص يوم الجمعة", lid=[("ar", 0.94)], asr="ar", first=True))
    assert d.action == A.SWITCH and d.current == "ar"


def test_rule_7_poor_audio_repeat_then_ask_then_callback_and_resets_when_audio_is_fine():
    t = tracker()
    bad = lambda: t.observe(utt("hmm bord", lid=[("sv", 0.3), ("en", 0.25)], speech=1500, conf=0.2))
    actions = [bad().action for _ in range(3)]
    assert actions == [A.REPEAT, A.ASK, A.CALLBACK] and t.current == "sv"
    t.observe(utt(SV_TEXT, lid=[("sv", 0.95)]))
    assert bad().action == A.REPEAT


def test_rule_7_without_lid_a_low_confidence_utterance_counts_as_poor_audio():
    t = tracker()
    assert t.observe(utt("bord fyra", conf=0.1)).action == A.REPEAT


# --- probing -----------------------------------------------------------------------------------

def test_probe_when_the_hint_points_elsewhere_or_the_asr_is_unsure():
    t = tracker(caller_e164="+98912000111")
    assert t.wants_probe(utt(EN_TEXT, asr="sv")) == ("sv", "en", "fa", "ar", "de")
    assert t.wants_probe(utt(SV_TEXT)) is None                                           # all fine
    assert t.wants_probe(utt("bord fyra", conf=0.2)) is not None                         # low ASR confidence
    assert t.wants_probe(utt(EN_TEXT, lid=[("en", 0.9)])) is None                        # scores already there
    assert t.wants_probe(utt(FA_TEXT)) is not None


def test_probe_current_language_leads_the_candidates_and_explicit_named_requests_need_none():
    t = tracker(current="en")
    assert t.wants_probe(utt("jag vill boka ett bord för fyra personer", asr="en"))[0] == "en"
    assert tracker().wants_probe(utt("kan vi prata engelska")) is None


def test_probe_can_be_switched_off():
    t = LanguageTracker(LanguageConfig(probe_on_hint=False), spoken=languages.SPOKEN)
    assert t.wants_probe(utt(EN_TEXT)) is None


def test_evidence_lists_the_pieces_without_the_text():
    t = tracker()
    ev = t.evidence(utt(EN_TEXT, lid=[("en", 0.9), ("sv", 0.1)], conf=0.2, asr="en"))
    assert [e.source for e in ev] == ["transcribe_lid", "hint", "asr_confidence"]
    assert ev[0].code == "en" and ev[0].score == 0.9 and ev[0].content_words >= 8
    assert not any(hasattr(e, "text") for e in ev)
