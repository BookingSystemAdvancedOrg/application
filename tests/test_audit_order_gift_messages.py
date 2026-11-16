"""Audit #52: receipt and cancellation e-mails name the gift card part and
the card part of an order separately, and a 0 kr card refund stays 0 kr."""

from shared import order_messages as om

LINE = {"name": "Pizza", "quantity": 1, "unitPrice": 30000, "total": 30000, "vatRate": 12}
BASE = {"orderId": "o-1", "number": 7, "mode": "pickup", "customerName": "Anna Svensson", "lines": [LINE],
        "totalAmount": 30000, "vat": [{"rate": 12, "vat": 3214, "net": 26786}],
        "createdAt": "2026-11-16T12:00:00Z"}
TENANT = {"name": "Krogen"}


def _mail(kind, item):
    subject, text, html_body = om.email(kind, item, TENANT, {}, None)
    return text, html_body


def test_card_only_cancellation_is_unchanged():
    text, _ = _mail("order_cancelled", {**BASE, "refund": {"amount": 30000}})
    assert "Hela beloppet, 300 kr, betalas tillbaka till ditt kort" in text


def test_gift_card_only_cancellation_goes_back_to_the_gift_card():
    item = {**BASE, "refund": {"amount": 0}, "giftCard": {"last4": "4321", "debitedOre": 30000,
                                                         "refundedOre": 30000}}
    text, html_body = _mail("order_cancelled", item)
    assert "300 kr" in text and "presentkort ****4321" in text
    assert "kort inom" not in text and "till ditt kort" not in text
    assert "presentkort ****4321" in html_body


def test_split_cancellation_names_both_parts():
    item = {**BASE, "refund": {"amount": 10000}, "giftCard": {"last4": "4321", "debitedOre": 20000}}
    text, _ = _mail("order_cancelled", item)
    assert "100 kr till ditt kort" in text and "200 kr till ditt presentkort ****4321" in text
    assert "Hela beloppet, 100 kr" not in text


def test_split_receipt_shows_the_gift_card_and_the_card_part():
    item = {**BASE, "giftCard": {"last4": "4321", "debitedOre": 20000}}
    text, html_body = _mail("order_confirmed", item)
    assert "Totalt: 300 kr" in text
    assert "Presentkort ****4321: -200 kr" in text
    assert "Kort (Stripe): 100 kr" in text
    assert "Presentkort ****4321" in html_body and "-200 kr" in html_body


def test_card_only_receipt_still_says_card():
    text, _ = _mail("order_confirmed", BASE)
    assert "Betalning: Kort (Stripe)" in text and "Presentkort" not in text


def test_english_split_cancellation():
    item = {**BASE, "language": "en", "refund": {"amount": 10000},
            "giftCard": {"last4": "4321", "debitedOre": 20000}}
    text, _ = _mail("order_cancelled", item)
    assert "100 kr to your card" in text and "200 kr to your gift card ****4321" in text
