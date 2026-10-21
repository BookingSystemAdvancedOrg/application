"""shared/sms.py: how many parts a message is sent (and billed) as."""

from shared import sms


def test_gsm_messages_fit_160_then_153_per_part():
    assert sms.parts("") == 0
    assert sms.parts("Roma: Hej Anna! Ditt bord är bokat fredag 18:30, 4 personer.") == 1
    assert sms.parts("a" * 160) == 1
    assert sms.parts("a" * 161) == 2
    assert sms.parts("a" * 306) == 2
    assert sms.parts("a" * 307) == 3


def test_extension_characters_count_double():
    assert sms.length("{}") == 4 and sms.length("€") == 2
    assert sms.parts("a" * 158 + "€") == 1
    assert sms.parts("a" * 159 + "€") == 2


def test_emoji_and_non_gsm_scripts_are_ucs2_70_then_67():
    assert not sms.is_gsm("Tack! 🙂")
    assert sms.parts("Tack! 🙂") == 1
    assert sms.parts("x" * 68 + "🙂") == 1          # 68 + 2 code units = 70
    assert sms.parts("x" * 69 + "🙂") == 2
    assert sms.parts("Привет " * 10) == 1 if len("Привет " * 10) <= 70 else sms.parts("Привет " * 10) == 2
    assert sms.parts("ж" * 134) == 2 and sms.parts("ж" * 135) == 3


def test_swedish_letters_are_gsm():
    assert sms.is_gsm("Åäö ÄÖ é à ü ñ ¿ ¡ £ $")
