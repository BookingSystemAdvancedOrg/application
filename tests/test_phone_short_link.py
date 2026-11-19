"""The phone guest's card link as a short plain SMS link (owner 2026-11-19).

A real test call texted the long Stripe Checkout URL; the iPhone stopped
the link inside its #fragment (at %2F) and Stripe said "This link is
incomplete". Every guarantee SMS now carries
https://restaurang.<domain>/api/k/<10 random characters> (shared/short_link.py),
stored with the booking and served by get-reservation's GET /k/{code}.
The SMS names the restaurant as guests know it, never the company."""

import re
from datetime import timedelta

import pytest

from shared import manage_link, messages, short_link
from tenant_support import TENANT_A, TENANT_TABLE
from test_guarantee import ACCOUNT, g, hook, stored  # noqa: F401 (fixtures)
from test_phone_guarantee_link import (CHECKOUT_URL, DASHES, SITE, _card_added, _card_step_site, gc,  # noqa: F401
                                       phone_book, stripe_event)
from test_reservations import LOC_A, LOC_B, RESERVATION_TABLE, body_of, env  # noqa: F401

BASE = "https://restaurang.booqy.se/api/k"
SHORT = re.compile(r"https://restaurang\.booqy\.se/api/k/([A-Za-z0-9]{10})")
# GSM 03.38 basic set (no escape characters): one SMS part is 160, two are 306.
GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZ"
           "ÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")


@pytest.fixture
def sl(gc, monkeypatch):
    monkeypatch.setenv("SHORT_LINK_BASE", BASE + "/")
    return gc


def link_row(env, code):
    return env["res"].Table(RESERVATION_TABLE).get_item(Key={"PK": f"LINK#{code}", "SK": "LINK"}).get("Item")


def open_link(env, code, query=None):
    e = {"routeKey": "GET /k/{code}", "pathParameters": {"code": code},
         "requestContext": {"http": {"method": "GET"}}, "headers": {}}
    if query:
        e["queryStringParameters"] = query
    return env["m"]["get"].handler(e, None)


def short_booking(env):
    _, body = phone_book(env)
    match = SHORT.fullmatch(body["guaranteeLink"]["url"])
    assert match, body["guaranteeLink"]
    return body["reservationId"], match.group(1)


# --- the link in the answer and the SMS ----------------------------------------------------------

def test_phone_booking_answers_a_short_plain_link_stored_with_the_booking(sl):
    rid, code = short_booking(sl)

    url = f"{BASE}/{code}"
    assert len(url) <= 45 and short_link.PLAIN_URL.fullmatch(url)
    assert not set("#%?&=_") & set(url)
    row = link_row(sl, code)
    item = stored(sl, rid)
    assert row["target"] == CHECKOUT_URL and "doneUrl" not in row        # no site address in the test
    assert row["tenantId"] == item["tenantId"] == TENANT_A
    assert row["locationId"] == LOC_A and row["reservationId"] == rid
    assert row["expiresAt"] == item["pendingExpiresAt"] == "2026-09-08T12:32:00Z"
    assert int(row["ttl"]) == int(short_link.datetime.fromisoformat("2026-09-08T12:32:00+00:00").timestamp())
    assert "notice" not in row and "status" not in row                   # no stream consumer picks it up


def test_without_a_short_link_base_the_long_url_is_kept(gc, monkeypatch):
    for value in (None, "", "https://bad.example/a?b=c", "http://restaurang.booqy.se/api/k"):
        if value is None:
            monkeypatch.delenv("SHORT_LINK_BASE", raising=False)
        else:
            monkeypatch.setenv("SHORT_LINK_BASE", value)
        assert short_link.base() is None
    _, body = phone_book(gc)
    assert body["guaranteeLink"]["url"] == CHECKOUT_URL


def test_codes_are_random_base62_and_unique():
    codes = {short_link.new_code() for _ in range(2000)}
    assert len(codes) == 2000
    assert all(short_link.valid_code(c) and len(c) == 10 for c in codes)
    assert len(set("".join(codes))) > 55                                 # the whole alphabet is used
    for bad in (None, "", "abc", "A" * 11, "AbCdEfGh1/", "AbCdEfGh1#", "../../etc", 1234567890):
        assert not short_link.valid_code(bad)


def test_put_op_refuses_a_target_that_is_not_https():
    item = {"tenantId": "t", "locationId": "l", "reservationId": "r"}
    from datetime import datetime, timezone
    for target in ("http://x.se", "javascript:alert(1)", "//evil.example", "", None):
        with pytest.raises(ValueError):
            short_link.put_op("AbCdEfGh12", table="t", target=target, item=item,
                              expires=datetime(2026, 9, 8, tzinfo=timezone.utc), serialize=dict)


def test_sms_of_the_owners_call_is_two_parts_with_a_whole_link():
    """The real call: Friday 20 November 19:00, 4 guests, 1 000 kr / 400 kr, 24 hours."""
    item = {"date": "2026-11-20", "startTime": "19:00", "partySize": 4, "language": "sv",
            "guarantee": {"noShowFee": 100000, "lateCancelFee": 40000, "cancelCutoffHours": 24}}
    link = f"{BASE}/Ab3dE5fG7h"
    text = messages.guarantee_link_sms(item, {"name": "RADIN DJ AB"}, {"name": "Restaurant 1"}, link, 30)

    assert text.startswith("Restaurant 1: ") and "RADIN" not in text
    assert f"Lägg till kort för din bokning: {link} " in text            # labelled, ends at a space
    assert "fredag 20 november kl 19:00" in text and "4 personer" in text and "30 minuter" in text
    assert "Inget dras nu." in text and "1 000 kr" in text and "400 kr" in text and "24 timmar" in text
    assert set(text) <= GSM7 and len(text) <= 306 and not DASHES.search(text)
    english = messages.guarantee_link_sms({**item, "language": "en"}, {"name": "RADIN DJ AB"},
                                          {"name": "Restaurant 1"}, link, 30)
    assert f"Add a card for your booking: {link} " in english
    assert set(english) <= GSM7 and len(english) <= 306 and not DASHES.search(english)


@pytest.mark.parametrize("tenant_name, location_name, expected", [
    ("RADIN DJ AB", "Restaurant 1", "Restaurant 1"),
    ("Radin DJ Aktiebolag", "Anar", "Anar"),
    ("Bistro Nord AB (publ)", "Södermalm", "Södermalm"),
    ("Matglädje Ek. för.", "Matglädje Kafé", "Matglädje Kafé"),
    ("Nordic Food Oy", "", "Nordic Food"),
    ("Bistro Nord AB", None, "Bistro Nord"),
    ("Bistro", "Södermalm", "Bistro Södermalm"),
    ("Bistro", "Bistro", "Bistro"),
    ("Anar", "Anar Södermalm", "Anar Södermalm"),
    ("", "Restaurant 1", "Restaurant 1"),
    ("", "", "Restaurangen"),
    ("Absolut Bar", "Vasastan", "Absolut Bar Vasastan"),
])
def test_guests_see_the_restaurant_name_never_the_company(tenant_name, location_name, expected):
    assert messages.restaurant_name({"name": tenant_name}, {"name": location_name}) == expected


def test_brand_name_is_empty_for_a_company_name():
    assert messages.brand_name({"name": "RADIN DJ AB"}) == ""
    assert messages.brand_name({"name": "  Bistro   Nord "}) == "Bistro Nord"
    assert messages.brand_name(None) == ""


# --- GET /k/{code} ---------------------------------------------------------------------------------

def test_open_link_redirects_to_the_stored_card_page(sl):
    _, code = short_booking(sl)

    response = open_link(sl, code, query={"url": "https://evil.example", "next": "https://evil.example"})

    assert response["statusCode"] == 302 and response["headers"]["Location"] == CHECKOUT_URL
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response["headers"]["Referrer-Policy"] == "no-referrer"


@pytest.mark.parametrize("code", ["AAAAAAAAAA", "short", "AbCdEfGh1/", "../../x", "", None])
def test_unknown_or_malformed_codes_get_the_swedish_expired_page(sl, code):
    short_booking(sl)
    response = open_link(sl, code)
    assert response["statusCode"] == 404 and "Location" not in response["headers"]
    assert response["headers"]["Content-Type"].startswith("text/html")
    assert "Länken har gått ut" in response["body"] and "Ring restaurangen" in response["body"]
    assert not DASHES.search(response["body"])


def test_expired_link_says_whom_to_call(sl):
    _, code = short_booking(sl)
    sl["clock"].value = sl["clock"].value + timedelta(minutes=33)

    response = open_link(sl, code)

    assert response["statusCode"] == 404 and "Location" not in response["headers"]
    assert "Länken har gått ut" in response["body"]
    assert f"Restaurang {LOC_A}" in response["body"] and "+46812345678" in response["body"]


def test_link_stops_redirecting_at_its_own_expiry(sl):
    rid, code = short_booking(sl)
    sl["res"].Table(RESERVATION_TABLE).update_item(
        Key={"PK": f"LINK#{code}", "SK": "LINK"}, UpdateExpression="SET expiresAt = :e",
        ExpressionAttributeValues={":e": "2026-09-08T12:05:00Z"})
    sl["clock"].value = sl["clock"].value + timedelta(minutes=6)
    assert open_link(sl, code)["statusCode"] == 404


def test_cancelled_booking_link_is_expired(sl):
    rid, code = short_booking(sl)
    sl["res"].Table(RESERVATION_TABLE).update_item(
        Key={"PK": f"LOCATION#{LOC_A}", "SK": f"RESERVATION#{stored(sl, rid)['date']}#{rid}"},
        UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "expired"})
    assert open_link(sl, code)["statusCode"] == 404


def test_link_of_another_tenant_never_redirects(sl):
    """A stored link whose tenant does not own its location (tampered row): nothing."""
    _, code = short_booking(sl)
    table = sl["res"].Table(RESERVATION_TABLE)
    table.update_item(Key={"PK": f"LINK#{code}", "SK": "LINK"}, UpdateExpression="SET tenantId = :t",
                      ExpressionAttributeValues={":t": "t-other"})
    assert open_link(sl, code)["statusCode"] == 404
    table.update_item(Key={"PK": f"LINK#{code}", "SK": "LINK"}, UpdateExpression="SET tenantId = :t, locationId = :l",
                      ExpressionAttributeValues={":t": TENANT_A, ":l": LOC_B})
    assert open_link(sl, code)["statusCode"] == 404


def test_inactive_restaurant_link_is_expired(sl):
    _, code = short_booking(sl)
    sl["res"].Table(TENANT_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"}, UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "suspended"})
    from shared import tenant as shared_tenant
    shared_tenant.reset_caches()
    assert open_link(sl, code)["statusCode"] == 404


def test_booked_link_without_a_site_shows_the_booked_page(sl, hook):
    rid, code = short_booking(sl)
    obj = {"id": "seti_checkout", "object": "setup_intent", "status": "succeeded", "customer": "cus_1",
           "payment_method": "pm_phone", "metadata": {"reservationId": rid, "locationId": LOC_A}}
    assert body_of(hook(stripe_event("setup_intent.succeeded", obj)))["result"] == "confirmed"

    response = open_link(sl, code)

    assert response["statusCode"] == 200 and "Ditt bord är bokat" in response["body"]
    assert "Location" not in response["headers"]


def test_booked_link_goes_to_the_manage_page_when_the_site_is_known(sl, monkeypatch):
    monkeypatch.setattr(sl["m"]["create"].site, "base", lambda tenant_id, row: (SITE, None))
    rid, code = short_booking(sl)
    item = stored(sl, rid)
    assert link_row(sl, code)["doneUrl"] == manage_link.url(SITE, item)
    assert open_link(sl, code)["headers"]["Location"] == CHECKOUT_URL      # still pending: the card page
    _card_added(sl, rid)
    from shared import guarantee
    guarantee.confirm_if_ready(stored(sl, rid), sl["clock"](), "guest")

    response = open_link(sl, code)

    assert response["statusCode"] == 302 and response["headers"]["Location"] == manage_link.url(SITE, item)


def test_site_mode_short_link_leads_to_the_manage_page(sl, monkeypatch):
    monkeypatch.setenv("PHONE_CARD_PAGE", "site")
    monkeypatch.setattr(sl["m"]["create"].site, "base", lambda tenant_id, row: (SITE, None))
    _card_step_site(sl)
    rid, code = short_booking(sl)
    item = stored(sl, rid)
    assert link_row(sl, code)["target"] == manage_link.url(SITE, item)
    response = open_link(sl, code)
    assert response["statusCode"] == 302 and response["headers"]["Location"] == manage_link.url(SITE, item)


def test_open_link_answers_a_swedish_retry_page_when_dynamodb_fails(sl, monkeypatch):
    _, code = short_booking(sl)
    from botocore.exceptions import ClientError

    def down(code_, table):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "GetItem")
    monkeypatch.setattr(sl["m"]["get"].short_link, "load", down)
    response = open_link(sl, code)
    assert response["statusCode"] == 503 and "Försök igen" in response["body"] and "Booqys fel" in response["body"]


def test_only_get_is_served(sl):
    _, code = short_booking(sl)
    e = {"routeKey": "POST /k/{code}", "pathParameters": {"code": code},
         "requestContext": {"http": {"method": "POST"}}, "headers": {}}
    assert sl["m"]["get"].handler(e, None)["statusCode"] == 405
