"""Phone bookings with a card guarantee (owner 2026-11-18): the AI phone
agent books the table pending through the manual route, which opens a
Stripe Checkout page (mode setup) on the restaurant's account; the caller
gets the link by SMS; the setup_intent.succeeded webhook of the Checkout
SetupIntent turns the booking reserved; an unconfirmed one is released by
the expiry job.

Stripe is a recording fake, DynamoDB is moto (as tests/test_guarantee.py).
The agent's tools: tests/test_phone_agent_guarantee_link.py."""

import re
from datetime import timedelta

import pytest

from error_body import err  # noqa: F401
from shared import guarantee as shared_guarantee
from shared import manage_link, stripe_client
from shared import tenant as shared_tenant
from tenant_support import TENANT_A, TENANT_TABLE
from test_guarantee import ACCOUNT, FakeStripe, book, confirm, g, hook, markers, stored  # noqa: F401 (fixtures)
from test_reservations import (DAY, LOC_A, NOW, STAFF_A, body_of, env, event, free_tables_at,  # noqa: F401
                               guest_get, load)

real_stripe_request = stripe_client.request
DASHES = re.compile("[\u2013\u2014]|&[nm]dash;")
CHECKOUT_URL = "https://checkout.stripe.com/c/pay/cs_test_1"


class CheckoutStripe(FakeStripe):
    """FakeStripe plus Checkout Sessions; `completed` is the SetupIntent of
    a submitted page (None: the guest has not added the card)."""

    completed = None
    down = False
    rid = None

    def __call__(self, method, path, params=None, *, account=None, idempotency_key=None, timeout=None):
        if path.startswith("/v1/setup_intents/") and method == "GET":
            self.calls.append({"method": method, "path": path, "params": params or {},
                               "account": account, "key": idempotency_key})
            if self.down:
                raise stripe_client.StripeUnavailable("down")
            return {"id": "seti_1", "status": self.setup_status, "customer": "cus_1", "payment_method": "pm_card",
                    "client_secret": "seti_1_secret_abc", "metadata": {"reservationId": self.rid}}
        if path.startswith("/v1/checkout/sessions"):
            self.calls.append({"method": method, "path": path, "params": params or {},
                               "account": account, "key": idempotency_key})
            if method == "GET":
                if self.down:
                    raise stripe_client.StripeUnavailable("down")
                assert path == "/v1/checkout/sessions/cs_test_1" and params == {"expand": ["setup_intent", "setup_intent.payment_method"]}
                return {"id": "cs_test_1", "status": "complete" if self.completed else "open",
                        "setup_intent": self.completed}
            return {"id": "cs_test_1", "url": CHECKOUT_URL, "setup_intent": None}
        return super().__call__(method, path, params, account=account, idempotency_key=idempotency_key)


@pytest.fixture
def gc(g, monkeypatch):
    from shared import stripe_client
    fake = CheckoutStripe()
    monkeypatch.setattr(stripe_client, "request", fake)
    g["stripe"] = fake
    return g


def phone_book(env, party=3, tables=("table-b",), **extra):
    payload = {"date": DAY, "startTime": "18:00", "tableIds": list(tables), "partySize": party,
               "name": "Anna Svensson", "phone": "+46701234567", "source": "phone", "guarantee": "link", **extra}
    response = env["m"]["create"].handler(
        event("POST", "/locations/{locationId}/reservations/manual", staff=STAFF_A, body=payload), None)
    return response, body_of(response)


# --- the manual route ------------------------------------------------------------------------------

def test_phone_booking_with_guarantee_link_is_pending_with_a_checkout_page(gc):
    response, body = phone_book(gc)

    assert response["statusCode"] == 201, body
    rid = body["reservationId"]
    assert body["status"] == "pending" and body["source"] == "phone"
    assert body["guaranteeLink"] == {"url": CHECKOUT_URL, "minutes": 30, "expiresAt": "2026-09-08T12:32:00Z",
                                     "page": "stripe"}
    item = stored(gc, rid)
    assert item["pendingExpiresAt"] == "2026-09-08T12:32:00Z" and item["checkoutSessionId"] == "cs_test_1"
    assert item["stripeAccountId"] == ACCOUNT and item["stripeCustomerId"] == "cus_1"
    assert "setupIntentId" not in item and "notice" not in item          # confirmation waits for the card
    assert item["guarantee"]["noShowFee"] == 60000 and item["guarantee"]["lateCancelFee"] == 30000
    assert "table-b" not in free_tables_at(gc, "18:00") and len(markers(gc)) == 1
    (session,) = [c for c in gc["stripe"].calls if c["path"] == "/v1/checkout/sessions"]
    params = session["params"]
    assert session["account"] == ACCOUNT and session["key"] == f"checkout-{rid}"
    assert params["mode"] == "setup" and params["customer"] == "cus_1" and params["payment_method_types"] == ["card"]
    assert params["locale"] == "sv" and params["expires_at"] == int((NOW + timedelta(minutes=31)).timestamp())
    assert params["success_url"] == params["cancel_url"] == "https://booqy.se"     # no site address in the test
    meta = {"reservationId": rid, "locationId": LOC_A, "tenantId": item["tenantId"], "date": DAY}
    assert params["setup_intent_data"]["metadata"] == meta and params["metadata"] == meta
    assert "600 kr" in params["custom_text"]["submit"]["message"]
    assert not [c for c in gc["stripe"].calls if c["path"] == "/v1/setup_intents"]
    # Stored for staff (who texted it, resends); the caller (the agent) texted it.
    assert item["guaranteeLink"] == {"url": CHECKOUT_URL, "page": "stripe", "by": "caller", "sends": 1}
    assert set(body) - {"guaranteeLink"} == set(gc["m"]["create"].r.staff_view(item)) - {"guaranteeLink"}


def test_phone_booking_in_english_gets_an_english_checkout_page(gc):
    _, body = phone_book(gc, language="en")
    (session,) = [c for c in gc["stripe"].calls if c["path"] == "/v1/checkout/sessions"]
    assert session["params"]["locale"] == "en" and body["status"] == "pending"


def test_staff_bookings_without_the_link_and_small_parties_are_reserved(gc):
    _, plain = phone_book(gc, guarantee=None)
    assert plain["status"] == "reserved" and "guaranteeLink" not in plain
    _, small = phone_book(gc, party=1, tables=("table-a",))
    assert small["status"] == "reserved" and "guaranteeLink" not in small
    assert not [c for c in gc["stripe"].calls if c["path"] == "/v1/checkout/sessions"]


@pytest.mark.parametrize("extra, message", [
    ({"guarantee": "card"}, "guarantee must be link or sms"),
    ({"source": "walk_in"}, "guarantee link is for phone bookings"),
    ({"phone": None}, "guarantee link needs the guest's phone number"),
])
def test_guarantee_link_request_is_validated(gc, extra, message):
    response, body = phone_book(gc, **extra)
    assert response["statusCode"] == 400 and body == err(message)


# --- the webhook: the Checkout page's SetupIntent ------------------------------------------------

def test_webhook_confirms_a_phone_booking_from_its_checkout_setup_intent(gc, hook):
    _, body = phone_book(gc)
    rid = body["reservationId"]
    obj = {"id": "seti_checkout", "object": "setup_intent", "status": "succeeded", "customer": "cus_1",
           "payment_method": "pm_phone", "metadata": {"reservationId": rid, "locationId": LOC_A}}

    other_customer = {**obj, "customer": "cus_other"}
    assert body_of(hook(stripe_event("setup_intent.succeeded", other_customer)))["result"] == \
        "ignored:card_not_confirmed"
    assert body_of(hook(stripe_event("setup_intent.succeeded", obj, account="acct_other")))["result"] == "ignored"
    assert stored(gc, rid)["status"] == "pending"

    response = hook(stripe_event("setup_intent.succeeded", obj))

    assert body_of(response)["result"] == "confirmed"
    item = stored(gc, rid)
    assert item["status"] == "reserved" and item["setupIntentId"] == "seti_checkout"
    assert item["stripePaymentMethodId"] == "pm_phone" and item["notice"]["type"] == "confirmed"
    assert "pendingExpiresAt" not in item and markers(gc) == []
    assert item["history"][-1]["action"] == "guarantee_confirmed"
    assert body_of(hook(stripe_event("setup_intent.succeeded", obj)))["result"] == "ignored"   # duplicate delivery


def stripe_event(kind, obj, account=ACCOUNT, livemode=False):
    return {"id": "evt_phone", "type": kind, "account": account, "livemode": livemode, "data": {"object": obj}}


def test_unconfirmed_phone_booking_is_released_by_the_expiry_job_without_a_message(gc, hook):
    _, body = phone_book(gc)
    rid = body["reservationId"]
    reminders = load("reservation-reminders", "res_reminders_phone")
    gc["clock"].value = gc["clock"].value + timedelta(minutes=31)
    assert reminders.handler({}, None)["expired"] == 0                    # the page closed, the table still held
    gc["clock"].value = gc["clock"].value + timedelta(minutes=2)

    assert reminders.handler({}, None)["expired"] == 1

    item = stored(gc, rid)
    assert item["status"] == "expired" and "notice" not in item and markers(gc) == []
    assert "table-b" in free_tables_at(gc, "18:00")
    late = {"id": "seti_late", "status": "succeeded", "customer": "cus_1", "payment_method": "pm_late",
            "metadata": {"reservationId": rid, "locationId": LOC_A}}
    assert body_of(hook(stripe_event("setup_intent.succeeded", late)))["result"] == "ignored"
    assert stored(gc, rid)["status"] == "expired"


def test_confirm_without_the_webhook_waits_for_the_card(gc):
    _, body = phone_book(gc)
    item = stored(gc, body["reservationId"])
    from shared import reservations as r
    with pytest.raises(r.InvalidState):
        shared_guarantee.confirm_if_ready(item, NOW, "guest")
    assert stored(gc, body["reservationId"])["status"] == "pending"


def _card_added(gc, rid, customer="cus_1"):
    gc["stripe"].completed = {"id": "seti_checkout", "object": "setup_intent", "status": "succeeded",
                              "customer": customer, "payment_method": "pm_phone",
                              "metadata": {"reservationId": rid, "locationId": LOC_A}}


def test_guest_back_from_the_checkout_page_confirms_without_the_webhook(gc):
    _, body = phone_book(gc)
    rid = body["reservationId"]
    _card_added(gc, rid, customer="cus_other")
    from shared import reservations as r
    with pytest.raises(r.InvalidState):                  # someone else's SetupIntent
        shared_guarantee.confirm_if_ready(stored(gc, rid), NOW, "guest")
    _card_added(gc, rid)

    done = shared_guarantee.confirm_if_ready(stored(gc, rid), NOW, "guest")

    item = stored(gc, rid)
    assert done["status"] == item["status"] == "reserved" and item["setupIntentId"] == "seti_checkout"
    assert item["stripePaymentMethodId"] == "pm_phone" and item["notice"]["type"] == "confirmed"


def test_checkout_page_returns_to_the_manage_page_when_the_site_is_known(gc, monkeypatch):
    mod = gc["m"]["create"]
    monkeypatch.setattr(mod.site, "base", lambda tenant_id, row: ("https://bistro.se", None))
    _, body = phone_book(gc)
    item = stored(gc, body["reservationId"])
    (session,) = [c for c in gc["stripe"].calls if c["path"] == "/v1/checkout/sessions"]
    assert session["params"]["success_url"] == manage_link.url("https://bistro.se", item)
    assert session["params"]["cancel_url"] == "https://bistro.se/"


def test_expiry_job_confirms_a_phone_booking_whose_webhook_is_late(gc, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_ARN", "arn:aws:secretsmanager:eu-north-1:000000000000:secret:stripe")
    _, body = phone_book(gc)
    rid = body["reservationId"]
    reminders = load("reservation-reminders", "res_reminders_phone_late")
    gc["clock"].value = gc["clock"].value + timedelta(minutes=33)
    _card_added(gc, rid)

    assert reminders.handler({}, None)["expired"] == 0

    item = stored(gc, rid)
    assert item["status"] == "reserved" and item["setupIntentId"] == "seti_checkout" and markers(gc) == []


def test_expiry_job_waits_while_stripe_is_down_then_expires(gc, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_ARN", "arn:aws:secretsmanager:eu-north-1:000000000000:secret:stripe")
    _, body = phone_book(gc)
    rid = body["reservationId"]
    reminders = load("reservation-reminders", "res_reminders_phone_down")
    gc["stripe"].down = True
    gc["clock"].value = gc["clock"].value + timedelta(minutes=33)
    assert reminders.handler({}, None)["expired"] == 0 and stored(gc, rid)["status"] == "pending"
    gc["clock"].value = gc["clock"].value + timedelta(hours=1)
    assert reminders.handler({}, None)["expired"] == 1 and stored(gc, rid)["status"] == "expired"


def test_guarantee_link_sms_names_the_booking_fees_and_the_window():
    from shared import messages
    item = {"date": "2026-09-20", "startTime": "18:00", "partySize": 3, "language": "sv",
            "guarantee": {"noShowFee": 60000, "lateCancelFee": 30000, "cancelCutoffHours": 24}}
    text = messages.guarantee_link_sms(item, {"name": "Bistro"}, {"name": "Bistro"}, CHECKOUT_URL, 30)
    assert text.startswith("Bistro: ") and CHECKOUT_URL in text and "söndag 20 september kl 18:00" in text
    assert "3 personer" in text and "30 minuter" in text and "600 kr" in text and "300 kr" in text
    assert "24 timmar" in text and not DASHES.search(text)
    assert text.endswith(" Avgifter: 600 kr vid uteblivet besök, 300 kr vid avbokning senare än 24 timmar "
                         "före besöket.")
    english = messages.guarantee_link_sms({**item, "language": "en"}, {"name": "Bistro"}, {}, CHECKOUT_URL, 30)
    assert "Sunday 20 September at 18:00" in english and "30 minutes" in english
    assert english.endswith(" Fees: 600 kr for a no-show, 300 kr if you cancel less than 24 hours before "
                            "the visit.")
    one = messages.guarantee_link_sms({**item, "guarantee": {"noShowFee": 60000}}, {"name": "B"}, {}, "x", 30)
    assert one.endswith(" Avgift: 600 kr vid uteblivet besök.")


# --- PHONE_CARD_PAGE=site: the card on the restaurant's own site, as online --------------------------

SITE = "https://bistro.se"


def _card_step_site(env, on=True):
    """The tenant's live site runs site-kit 0.4.0 or later (cardStepOnManagePage)."""
    env["res"].Table(TENANT_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
        UpdateExpression="SET cardStepOnManagePage = :on", ExpressionAttributeValues={":on": on})
    shared_tenant.reset_caches()


@pytest.fixture
def site_page(gc, monkeypatch):
    monkeypatch.setenv("PHONE_CARD_PAGE", "site")
    monkeypatch.setattr(gc["m"]["create"].site, "base", lambda tenant_id, row: (SITE, None))
    _card_step_site(gc)
    return gc


def _site_booking(gc):
    _, body = phone_book(gc)
    rid = body["reservationId"]
    gc["stripe"].rid = rid
    item = stored(gc, rid)
    return rid, item, manage_link.token_for(item)


def test_site_mode_books_pending_with_a_setup_intent_and_texts_the_manage_link(site_page):
    response, body = phone_book(site_page)

    assert response["statusCode"] == 201 and body["status"] == "pending"
    item = stored(site_page, body["reservationId"])
    assert body["guaranteeLink"] == {"url": manage_link.url(SITE, item), "minutes": 30,
                                     "expiresAt": "2026-09-08T12:32:00Z", "page": "site"}
    assert body["guaranteeLink"]["url"].startswith(f"{SITE}/bokning/{LOC_A}/{item['reservationId']}#")
    assert item["setupIntentId"] == "seti_1" and item["stripeCustomerId"] == "cus_1"
    assert item["stripeAccountId"] == ACCOUNT and "checkoutSessionId" not in item
    assert item["pendingExpiresAt"] == "2026-09-08T12:32:00Z" and len(markers(site_page)) == 1
    assert item["guarantee"]["noShowFee"] == 60000 and "notice" not in item
    assert "seti_1_secret_abc" not in str(item)                         # the client secret is never stored
    (setup,) = [c for c in site_page["stripe"].calls if c["path"] == "/v1/setup_intents"]
    assert setup["account"] == ACCOUNT and setup["key"] == f"setup-{item['reservationId']}"
    assert setup["params"]["usage"] == "off_session" and setup["params"]["metadata"]["reservationId"] == \
        item["reservationId"]
    assert not [c for c in site_page["stripe"].calls if c["path"] == "/v1/checkout/sessions"]


def test_site_mode_without_a_site_address_falls_back_to_the_stripe_page(gc, monkeypatch):
    monkeypatch.setenv("PHONE_CARD_PAGE", "site")
    _, body = phone_book(gc)
    assert body["guaranteeLink"]["url"] == CHECKOUT_URL and body["guaranteeLink"]["page"] == "stripe"


@pytest.mark.parametrize("flag", [None, False, "true"])
def test_site_mode_needs_a_site_with_the_card_step(gc, monkeypatch, flag):
    """A site still on site-kit 0.3.x has no card form: its guests get the Stripe page."""
    monkeypatch.setenv("PHONE_CARD_PAGE", "site")
    monkeypatch.setattr(gc["m"]["create"].site, "base", lambda tenant_id, row: (SITE, None))
    if flag is not None:
        _card_step_site(gc, flag)
    _, body = phone_book(gc)
    assert body["guaranteeLink"]["url"] == CHECKOUT_URL and body["guaranteeLink"]["page"] == "stripe"
    assert "setupIntentId" not in stored(gc, body["reservationId"])


@pytest.mark.parametrize("value", [None, "stripe", "other"])
def test_stripe_mode_is_the_default(gc, monkeypatch, value):
    monkeypatch.setattr(gc["m"]["create"].site, "base", lambda tenant_id, row: (SITE, None))
    if value is None:
        monkeypatch.delenv("PHONE_CARD_PAGE", raising=False)
    else:
        monkeypatch.setenv("PHONE_CARD_PAGE", value)
    _, body = phone_book(gc)
    assert body["guaranteeLink"]["url"] == CHECKOUT_URL and "setupIntentId" not in stored(gc, body["reservationId"])


def test_manage_page_get_hands_the_card_form_to_a_pending_booking_with_a_valid_token(site_page):
    rid, item, token = _site_booking(site_page)
    site_page["stripe"].setup_status = "requires_payment_method"

    response = guest_get(site_page, rid, token)

    view = body_of(response)
    assert response["statusCode"] == 200 and view["status"] == "pending"
    assert view["setup"] == {"clientSecret": "seti_1_secret_abc", "stripeAccount": ACCOUNT,
                             "expiresAt": item["pendingExpiresAt"]}
    terms = view["guarantee"]
    assert terms["noShowFee"] == 60000 and terms["lateCancelFee"] == 30000 and terms["cancelCutoffHours"] == 24
    assert terms["noShowFeePerPerson"] == 200 and terms["lateCancelFeePerPerson"] == 100
    (read,) = [c for c in site_page["stripe"].calls if c["path"] == "/v1/setup_intents/seti_1"]
    assert read["method"] == "GET" and read["account"] == ACCOUNT


def test_manage_page_get_never_shows_the_card_form_otherwise(site_page):
    rid, item, token = _site_booking(site_page)
    stripe = site_page["stripe"]
    stripe.setup_status = "requires_payment_method"

    assert guest_get(site_page, rid, "wrong")["statusCode"] == 404
    assert guest_get(site_page, rid, "")["statusCode"] == 404
    stripe.down = True                                                   # Stripe down: the page, no form
    down = guest_get(site_page, rid, token)
    assert down["statusCode"] == 200 and "setup" not in body_of(down)
    assert body_of(down)["setupUnavailable"] is True                     # still held: reload, not "failed"
    assert body_of(down)["pendingExpiresAt"] == item["pendingExpiresAt"]
    stripe.down = False
    stripe.setup_status = "succeeded"                                    # the card is already in
    assert "setup" not in body_of(guest_get(site_page, rid, token))
    stripe.setup_status = "requires_payment_method"
    site_page["clock"].value = site_page["clock"].value + timedelta(minutes=33)     # the hold ran out
    assert "setup" not in body_of(guest_get(site_page, rid, token))
    stripe.down = True
    assert "setupUnavailable" not in body_of(guest_get(site_page, rid, token))
    stripe.down = False
    assert "seti_1_secret_abc" not in str(body_of(guest_get(site_page, rid, token)))


def test_manage_page_get_loads_when_the_stripe_secret_cannot_be_read(site_page, monkeypatch):
    """Secrets Manager denies or throttles: the booking still loads (reload hint), never a 503."""
    rid, item, token = _site_booking(site_page)
    monkeypatch.setattr(stripe_client, "request", real_stripe_request)
    monkeypatch.setenv("STRIPE_SECRET_ARN", "arn:aws:secretsmanager:eu-north-1:000000000000:secret:missing")
    stripe_client.reset_cache()
    response = guest_get(site_page, rid, token)
    stripe_client.reset_cache()
    assert response["statusCode"] == 200
    view = body_of(response)
    assert view["status"] == "pending" and "setup" not in view and view["setupUnavailable"] is True


def test_manage_page_get_no_form_once_the_booking_is_confirmed(site_page):
    rid, item, token = _site_booking(site_page)
    assert confirm(site_page, rid, token)["statusCode"] == 200          # the card form posted, Stripe says ok
    reserved = body_of(guest_get(site_page, rid, token))
    assert reserved["status"] == "reserved" and "setup" not in reserved
    assert stored(site_page, rid)["stripePaymentMethodId"] == "pm_card"


def test_manage_page_get_no_form_for_a_stripe_page_phone_booking(gc):
    _, body = phone_book(gc)
    item = stored(gc, body["reservationId"])
    view = body_of(guest_get(gc, item["reservationId"], manage_link.token_for(item)))
    assert view["status"] == "pending" and "setup" not in view


def test_an_online_pending_booking_gets_its_card_form_back_on_the_manage_page(gc):
    response, body = book(gc)
    assert response["statusCode"] == 201 and body["setup"]["clientSecret"] == "seti_1_secret_abc"
    gc["stripe"].setup_status = "requires_payment_method"
    view = body_of(guest_get(gc, body["reservationId"], body["manageToken"]))
    assert view["setup"] == body["setup"]


def test_site_mode_webhook_confirms_like_an_online_booking(site_page, hook):
    rid, _, _ = _site_booking(site_page)
    obj = {"id": "seti_1", "object": "setup_intent", "status": "succeeded", "customer": "cus_1",
           "payment_method": "pm_site", "metadata": {"reservationId": rid, "locationId": LOC_A}}
    assert body_of(hook(stripe_event("setup_intent.succeeded", obj)))["result"] == "confirmed"
    item = stored(site_page, rid)
    assert item["status"] == "reserved" and item["stripePaymentMethodId"] == "pm_site" and markers(site_page) == []


def test_expiry_job_confirms_a_site_card_booking_whose_webhook_is_late(site_page, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_ARN", "arn:aws:secretsmanager:eu-north-1:000000000000:secret:stripe")
    rid, _, _ = _site_booking(site_page)
    reminders = load("reservation-reminders", "res_reminders_site_late")
    site_page["clock"].value = site_page["clock"].value + timedelta(minutes=33)

    assert reminders.handler({}, None)["expired"] == 0

    item = stored(site_page, rid)
    assert item["status"] == "reserved" and item["setupIntentId"] == "seti_1" and markers(site_page) == []


def test_expiry_job_releases_a_site_card_booking_without_a_card(site_page, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_ARN", "arn:aws:secretsmanager:eu-north-1:000000000000:secret:stripe")
    rid, _, _ = _site_booking(site_page)
    site_page["stripe"].setup_status = "requires_payment_method"
    reminders = load("reservation-reminders", "res_reminders_site_none")
    site_page["clock"].value = site_page["clock"].value + timedelta(minutes=33)

    assert reminders.handler({}, None)["expired"] == 1

    assert stored(site_page, rid)["status"] == "expired" and "table-b" in free_tables_at(site_page, "18:00")
