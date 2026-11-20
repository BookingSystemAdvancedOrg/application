"""SentenceChunker and FillerPolicy (builder 'converse'). Pure."""

import pytest

from voice_gateway.config import SpeakConfig
from voice_gateway.events import ToolStarted
from voice_gateway.speak.chunker import SentenceChunker
from voice_gateway.speak.fillers import FILLERS, FillerPolicy

CFG = SpeakConfig()


def chunks(text, step=3, cfg=CFG):
    """Streams `text` in small deltas, as ConverseStream does, then flushes."""
    c = SentenceChunker(cfg)
    out = []
    for i in range(0, len(text), step):
        out += c.feed(text[i:i + step])
    return out + c.flush()


@pytest.mark.parametrize("step", [1, 3, 7, 1000])
def test_sentences_split_regardless_of_delta_size(step):
    text = "Vi har ett bord ledigt på fredag. Passar klockan sju? Toppen!"
    assert chunks(text, step) == ["Vi har ett bord ledigt på fredag.", "Passar klockan sju?", "Toppen!"]


@pytest.mark.parametrize("text,expected", [
    ("Jag kan boka kl. 18.30 på fredag. Passar det?", ["Jag kan boka kl. 18.30 på fredag.", "Passar det?"]),
    ("Vi har bord kl 18.30. Passar det?", ["Vi har bord kl 18.30.", "Passar det?"]),
    ("Vi kan t.ex. ta fredag. Eller lördag?", ["Vi kan t.ex. ta fredag.", "Eller lördag?"]),
    ("Det finns bl.a. glutenfritt och veganskt. Något mer?",
     ["Det finns bl.a. glutenfritt och veganskt.", "Något mer?"]),
    ("Det tar ca. tio minuter att gå hit. Välkommen!", ["Det tar ca. tio minuter att gå hit.", "Välkommen!"]),
    ("Bokningen har nr. 4 i kön just nu. Okej?", ["Bokningen har nr. 4 i kön just nu.", "Okej?"]),
    ("Den 24.12. klockan 18 har vi plats. Vill du boka?", ["Den 24.12. klockan 18 har vi plats.", "Vill du boka?"]),
    ("Priset är 12.5 procent lägre i dag. Okej?", ["Priset är 12.5 procent lägre i dag.", "Okej?"]),
    ("Ses vi klockan 18:30 då? Bra.", ["Ses vi klockan 18:30 då?", "Bra."]),
    ("Mr. Smith, your table is ready! Anything else?", ["Mr. Smith, your table is ready!", "Anything else?"]),
    ("Hmm... let me see what we have here. One moment.", ["Hmm... let me see what we have here.", "One moment."]),
    ("Okej… jag kollar fredag kväll åt dig. Ett ögonblick.",
     ["Okej… jag kollar fredag kväll åt dig.", "Ett ögonblick."]),
    ('Restaurangen heter "Mat & Co. Bar" och ligger nära. Vill du komma?',
     ['Restaurangen heter "Mat & Co. Bar" och ligger nära.', "Vill du komma?"]),
    ("Das ist am 5. Mai um 18 Uhr frei. Passt das?", ["Das ist am 5. Mai um 18 Uhr frei.", "Passt das?"]),
    ("Det kostar 200 kr. Vill du boka?", ["Det kostar 200 kr.", "Vill du boka?"]),
    ("Det blir på namnet Anna B. Svensson då. Stämmer det?", ["Det blir på namnet Anna B. Svensson då.",
                                                              "Stämmer det?"]),
    ("¿Para cuántas personas sería la reserva? Gracias.", ["¿Para cuántas personas sería la reserva?", "Gracias."]),
    ("هل تريد حجز طاولة لأربعة أشخاص؟ شكرا.", ["هل تريد حجز طاولة لأربعة أشخاص؟", "شكرا."]),
])
def test_abbreviations_numbers_dates_ellipses_quotes(text, expected):
    assert chunks(text) == expected


def test_first_chunk_of_a_turn_has_a_minimum_length():
    assert chunks("Okej. Vi har plats på fredag klockan sju. Passar det?") == [
        "Okej. Vi har plats på fredag klockan sju.", "Passar det?"]
    assert chunks("Absolut! Ja.") == ["Absolut! Ja."]           # the round ended: nothing is lost
    c = SentenceChunker(CFG)
    assert c.feed("Okej. ") == []
    assert c.feed("Jag kollar fredag åt dig. ") == ["Okej. Jag kollar fredag åt dig."]
    assert c.feed("Ja. ") == ["Ja."]                            # only the first chunk waits
    c.reset(first_of_turn=True)
    assert c.feed("Ja. ") == []
    c.reset()
    assert c.feed("Ja. ") == ["Ja."]


def test_long_read_back_is_cut_at_a_comma_past_the_middle_while_streaming():
    text = ("Då har jag fyra personer på fredag den 24 december klockan halv sju, på namnet Anna Andersson, "
            "och vi skickar en bekräftelse. Stämmer det?")
    c = SentenceChunker(CFG)
    early = []
    for i in range(0, len(text), 4):
        early += c.feed(text[i:i + 4])
        if early:
            assert i < text.index("Stämmer")       # the first piece left before the sentence ended
            break
    out = chunks(text)
    assert out[0].endswith("halv sju,") and out[-1] == "Stämmer det?"
    assert " ".join(out) == " ".join(text.split())
    assert all(len(p) <= CFG.clause_split_chars or "," not in p[len(p) // 2:] for p in out)


def test_numbers_with_commas_are_not_cut():
    text = "Det blir 1,5 timme eller 2,5 timmar beroende på vilken meny ni väljer och hur många ni blir till slut."
    assert chunks(text) == [text]


def test_whitespace_normalised_and_no_empty_chunks():
    assert chunks("  Hej   där.\n\n  Hur   mår du?   ") == ["Hej där. Hur mår du?"]
    assert chunks("") == [] and chunks("   \n ") == []
    c = SentenceChunker(CFG)
    assert c.flush() == [] and c.feed("") == []


def test_ambiguous_period_waits_for_the_next_word():
    c = SentenceChunker(CFG)
    c.reset()
    assert c.feed("Vi har bord kl 18.30. ") == []          # could be "18.30. kl" continuing
    assert c.feed("Passar") == ["Vi har bord kl 18.30."]
    assert c.flush() == ["Passar"]


def test_no_dashes_in_the_module():
    import inspect
    from voice_gateway.speak import chunker, fillers
    for mod in (chunker, fillers):
        src = inspect.getsource(mod)
        assert chr(0x2013) not in src and chr(0x2014) not in src


# --- fillers ---------------------------------------------------------------------------------

def test_filler_after_the_delay_when_nothing_else_plays():
    p = FillerPolicy(CFG)
    p.on_tool_started(ToolStarted(1000, 1, "get_availability"))
    assert p.due(1000 + CFG.filler_delay_ms - 20, "sv", assistant_busy=False) is None
    assert p.due(1000 + CFG.filler_delay_ms, "sv", assistant_busy=False) == 0
    assert p.due(1000 + CFG.filler_delay_ms + 20, "sv", assistant_busy=False) is None   # once per turn


def test_no_filler_for_own_sentence_tools_or_when_the_answer_came():
    p = FillerPolicy(CFG)
    for name in ("end_call", "transfer_to_staff"):
        p.on_tool_started(ToolStarted(0, 1, name))
        assert p.due(5000, "sv", assistant_busy=False) is None
    p.on_tool_started(ToolStarted(0, 1, "get_availability"))
    p.on_tool_finished(300)
    p.on_assistant_audio(400)                                   # the answer
    assert p.due(5000, "sv", assistant_busy=False) is None
    p.new_turn()
    p.on_tool_started(ToolStarted(6000, 2, "get_availability"))
    assert p.due(7000, "sv", assistant_busy=True) is None       # something plays
    assert p.due(7020, "sv", assistant_busy=False) is None      # quiet for 20 ms only
    assert p.due(7700, "sv", assistant_busy=False) is not None
    p.new_turn()
    p.on_tool_started(ToolStarted(9000, 3, "get_availability"))
    p.turn_done()
    assert p.due(12000, "sv", assistant_busy=False) is None


def test_after_the_models_own_preamble_the_filler_waits_twice_as_long():
    """"Då kollar jag." then a slow tool: no "Ett ögonblick, jag kollar" right after it."""
    p = FillerPolicy(CFG)
    p.new_turn()
    p.on_assistant_audio(1000)                                  # the preamble is queued
    p.on_tool_started(ToolStarted(1100, 1, "get_availability"))
    now = 1100
    while now < 2200:                                           # the preamble plays until 2200
        assert p.due(now, "sv", assistant_busy=True) is None
        now += 20
    assert p.due(2200 + CFG.filler_delay_ms, "sv", assistant_busy=False) is None
    assert p.due(2200 + 2 * CFG.filler_delay_ms, "sv", assistant_busy=False) == 0


def test_filler_rotation_cooldown_and_languages():
    p = FillerPolicy(CFG)
    seen, now = [], 0
    for turn in range(6):
        p.new_turn()
        p.on_tool_started(ToolStarted(now, turn, "get_availability"))
        got = p.due(now + CFG.filler_delay_ms, "sv", assistant_busy=False)
        seen.append(got)
        now += CFG.filler_cooldown_ms + 1000
    assert seen == [0, 1, 2, 0, 1, 2]
    assert all(a != b for a, b in zip(seen, seen[1:]))

    p = FillerPolicy(CFG)
    p.on_tool_started(ToolStarted(0, 1, "get_availability"))
    assert p.due(800, "sv", assistant_busy=False) == 0
    p.new_turn()
    p.on_tool_started(ToolStarted(1000, 2, "get_availability"))
    assert p.due(2000, "sv", assistant_busy=False) is None      # cooldown

    p = FillerPolicy(CFG)
    p.on_tool_started(ToolStarted(0, 1, "get_availability"))
    assert p.due(800, "ar", assistant_busy=False) is None       # no line in that language: none


def test_refused_filler_does_not_count():
    p = FillerPolicy(CFG)
    p.on_tool_started(ToolStarted(0, 1, "get_availability"))
    assert p.due(800, "sv", assistant_busy=False) == 0
    p.refused("sv")
    assert p.played_this_turn is False and p.last_ms is None
    p.on_tool_started(ToolStarted(900, 1, "create_booking"))
    assert p.due(1600, "sv", assistant_busy=False) == 0


def test_filler_lines_are_booqy_wording():
    assert FILLERS["sv"][0] == "Ett ögonblick, jag kollar."
    for lines in FILLERS.values():
        for line in lines:
            assert "AI" not in line.split() and line.endswith(".")
