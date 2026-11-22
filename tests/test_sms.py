"""shared/sms.py: where an SMS may go. allowed_prefixes() is the
notification Lambda's rule (Nordic codes, SMS_ALLOWED_PREFIXES);
textable() the stricter one for the card link SMS Booqy sends for a staff
phone booking (Nordic mobile numbers only); filler() spots made-up
numbers such as 070 000 00 00."""

import pytest

from shared import sms


@pytest.mark.parametrize("phone", [
    "+46701234567", "+46721234567", "+46731234567", "+46761234567", "+46791234567",   # Swedish mobiles
    "+4741234567", "+4791234567",                                                     # Norway 4 and 9
    "+4512345678", "+4520123456",                                                     # Denmark, any 8 digits
    "+358401234567", "+3584512345", "+358501234567", "+3585012345678",               # Finland 04x and 050
    "+3546123456", "+3547123456", "+3548123456",                                     # Iceland 6, 7, 8
])
def test_nordic_mobiles_are_textable(phone):
    assert sms.textable(phone)


@pytest.mark.parametrize("phone", [
    "+4681234567", "+46812345678", "+4631123456", "+46101234567",   # Swedish landlines and 010
    "+46711234567", "+46741234567", "+46751234567", "+46781234567",  # 071, 074, 075, 078: not mobiles
    "+4670123456", "+467012345678",                                   # too short, too long
    "+4722123456", "+4751234567", "+474123456",                     # Norwegian landlines, short
    "+451234567", "+45123456789",                                     # Danish, not 8 digits
    "+35891234567", "+3584123", "+358401234567890",                  # Finnish landline, short, long
    "+3545123456", "+35461234567",                                    # Icelandic landline, long
    "+4915112345678", "+447700900123", "+15551234567",               # outside the Nordic codes
    "0701234567", "", None, 46701234567,
])
def test_other_numbers_are_not(phone):
    assert not sms.textable(phone)


def test_textable_stays_within_the_allowed_prefixes(monkeypatch):
    monkeypatch.setenv("SMS_ALLOWED_PREFIXES", "+47, +45")
    assert sms.allowed_prefixes() == ("+47", "+45")
    assert not sms.textable("+46701234567") and sms.textable("+4741234567") and sms.textable("+4512345678")
    monkeypatch.setenv("SMS_ALLOWED_PREFIXES", "+44")
    assert not sms.textable("+447700900123")                          # allowed, but no known mobile range
    monkeypatch.delenv("SMS_ALLOWED_PREFIXES")
    assert sms.allowed_prefixes() == sms.NORDIC_PREFIXES == ("+46", "+45", "+47", "+358", "+354")


@pytest.mark.parametrize("phone, made_up", [
    ("+46700000000", True), ("+46701111111", True), ("+46777777777", True), ("+4511111111", True),
    ("+46701234567", False), ("+46700000001", False), ("+4741234567", False), (None, False),
])
def test_filler_numbers(phone, made_up):
    assert sms.filler(phone) is made_up
