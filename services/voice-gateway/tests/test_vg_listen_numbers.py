"""Transcribe sv-SE writes small numbers as clock times (live 2026-11-20: "för tre" came back as "för 15:00 15:00"):
listen/numbers.py reads them back with the context (listen builder)."""

import pytest

from voice_gateway.listen.numbers import normalise


@pytest.mark.parametrize("text,topic,want", [
    ("Hej, jag vill boka ett bord för 15:00 15:00 i morgon klockan 18:00.", None,
     "Hej, jag vill boka ett bord för 3 i morgon klockan 18:00."),
    ("16:00 personer på fredag", None, "4 personer på fredag"),
    ("Hej, jag vill boka ett bord för 3:00 imorgon klockan 6", None,          # live lang-A
     "Hej, jag vill boka ett bord för 3 imorgon klockan 6"),
    ("Vi är 16:00.", None, "Vi är 4."),
    ("bord för 19 i morgon klockan 20", None, "bord för 7 i morgon klockan 20"),
    ("klockan 19:00 för 16:00 personer", None, "klockan 19:00 för 4 personer"),
    ("15:00", "party", "3"),                                  # the answer to "hur många blir ni?"
    ("15:00.", "party", "3."),
    ("halv 19:00 passar", None, "halv 7 passar"),
    ("Jag skulle vilja boka bord för en. Fyra personer på fredag klockan 7.", None,
     "Jag skulle vilja boka bord för Fyra personer på fredag klockan 7."),       # "för, ehm... fyra personer"
    ("ett bord för en person", None, "ett bord för en person"),                  # a party of one stays
    # times stay times
    ("klockan 17:00", None, "klockan 17:00"),
    ("15:00", "time", "15:00"),
    ("15:00", None, "15:00"),
    ("till 18:00 för 3 personer", None, "till 18:00 för 3 personer"),
    ("boka för 18:00 i morgon för fyra personer", None, "boka för 18:00 i morgon för fyra personer"),
    ("klockan 15.30", None, "klockan 15.30"),
])
def test_swedish_numbers(text, topic, want):
    assert normalise(text, "sv", topic=topic) == want


def test_other_languages_are_left_alone():
    assert normalise("a table for 15:00 15:00", "en") == "a table for 15:00 15:00"
    assert normalise("", "sv") == ""
