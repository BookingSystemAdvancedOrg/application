"""Card-guarantee terms in guest messages and the guest view: the fees the
guest accepted (the snapshot) and the free-cancellation deadline, worded
like the booking site's terms (customer-front-end src/shared/bookingTerms.ts)."""

from datetime import datetime, timezone

from shared import guarantee, messages
from shared import reservations as r

BASE = {"tenantId": "t", "reservationId": "r1", "status": "reserved", "date": "2026-10-30",
        "startTime": "18:00", "endTime": "20:00", "bookedFor": "2026-10-30T17:00:00Z",
        "endsAt": "2026-10-30T19:00:00Z", "timezone": "Europe/Stockholm", "partySize": 4,
        "customerName": "Anna Gäst", "stripePaymentMethodId": "pm_1", "stripeCustomerId": "cus_1",
        "stripeAccountId": "acct_1"}


def item(no_show=250, late=150, hours=24, party=4):
    return {**BASE, "partySize": party, "guarantee": {
        "currency": "sek", "noShowFee": no_show * party * 100, "lateCancelFee": late * party * 100,
        "noShowFeePerPerson": no_show, "lateCancelFeePerPerson": late, "cancelCutoffHours": hours}}


def test_both_fees_with_the_exact_free_cancellation_deadline():
    assert messages.guarantee_lines(item(), "sv") == [
        "Uteblivet besök: 250 kr/person (1 000 kr för 4 personer).",
        "Avbokning senare än 24 timmar före: 150 kr/person (600 kr för 4 personer).",
        "Kostnadsfri avbokning senast torsdag 29 oktober kl 18:00.",
        "Restaurangen kan efterskänka avgiften vid sjukdom eller liknande.",
    ]


def test_no_show_only_is_free_to_cancel_until_the_start():
    assert messages.guarantee_lines(item(late=0), "sv") == [
        "Uteblivet besök: 250 kr/person (1 000 kr för 4 personer).",
        "Kostnadsfri avbokning fram till bokad tid.",
        "Restaurangen kan efterskänka avgiften vid sjukdom eller liknande.",
    ]


def test_late_cancel_only_one_hour_one_person_and_english():
    lines = messages.guarantee_lines(item(no_show=0, late=1500, hours=1, party=1), "sv")
    assert lines[:2] == ["Avbokning senare än 1 timme före: 1 500 kr/person.",
                         "Kostnadsfri avbokning senast fredag 30 oktober kl 17:00."]
    assert messages.guarantee_lines(item(), "en")[:3] == [
        "No-show: 250 kr/person (1 000 kr for 4 people).",
        "Cancelling later than 24 hours before: 150 kr/person (600 kr for 4 people).",
        "Free cancellation until Thursday 29 October at 18:00.",
    ]


def test_deadline_follows_the_restaurants_clock_across_dst():
    # Summer time ends 25 Oct 2026: 48 h before 18:00 CET on the 26th is 19:00 CEST on the 24th,
    # the instant the fee starts to apply.
    dst = {**item(hours=48), "date": "2026-10-26", "bookedFor": "2026-10-26T17:00:00Z"}
    assert "lördag 24 oktober kl 19:00" in messages.guarantee_lines(dst, "sv")[2]


def test_no_lines_without_a_card_on_file():
    assert messages.guarantee_lines({**BASE, "stripePaymentMethodId": None, "guarantee": item()["guarantee"]}) == []
    assert messages.guarantee_lines({k: v for k, v in BASE.items()}) == []


def test_confirmation_email_repeats_the_fees():
    _, text, html_body = messages.guest_email("confirmed", item(), {"name": "Roma"}, {}, "https://x/b")
    assert "Kortgaranti (inget har dragits från kortet)" in text
    assert "- Kostnadsfri avbokning senast torsdag 29 oktober kl 18:00." in text
    assert "Uteblivet besök: 250 kr/person" in html_body
    _, cancelled, _ = messages.guest_email("cancelled_by_guest", item(), {"name": "Roma"}, {}, None)
    assert "Kortgaranti" not in cancelled


def test_guest_view_has_the_free_cancellation_deadline():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert r.guest_view(item(), now=now)["guarantee"]["freeCancelUntil"] == "2026-10-29T17:00:00Z"
    assert r.guest_view(item(late=0), now=now)["guarantee"]["freeCancelUntil"] is None
    assert guarantee.free_cancel_until(item(hours=0)) is None
