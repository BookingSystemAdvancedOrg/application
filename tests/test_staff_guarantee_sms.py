"""Staff phone bookings with the card link by SMS (owner 2026-11-22).

A guest who calls and books a party the card guarantee covers gets the
card link by SMS from Booqy itself: the manual route with "guarantee":
"sms" books it pending (as the AI agent's "link" does), then invokes
notification:live with RequestResponse (kind reservations.guarantee-link)
while the staff are still on the call. A failed SMS releases the booking at
once (502 sms_failed); staff may send the same link again (POST
.../guarantee-link). The link url is a bearer link: never in a staff view.

Stripe is the recording fake of test_phone_guarantee_link, the Lambda
client a fake: no SMS, no Stripe objects (moto DynamoDB as elsewhere)."""

import io
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, ConnectionClosedError, ReadTimeoutError

from error_body import err
from shared import guarantee as shared_guarantee
from shared import messages
from shared import reservations as shared_r
from shared import tenant as shared_tenant
from tenant_support import TENANT_A, TENANT_TABLE
from test_guarantee import ACCOUNT, POLICY, g, hook, markers, stored  # noqa: F401 (fixtures)
from test_phone_guarantee_link import CheckoutStripe, _card_added, gc, phone_book  # noqa: F401
from test_reservations import (DAY, LOC_A, LOC_A2, LOCATION_TABLE, NOW, OWNER_A, OWNER_B,  # noqa: F401
                               RESERVATION_TABLE, STAFF_A, body_of, env, event, free_tables_at, load,
                               set_status, staff_list)

NOTIFICATION = "arn:aws:lambda:eu-north-1:000000000000:function:dev-notification:live"
SHORT_BASE = "https://restaurang.booqy.se/api/k"
GUARD_TABLE = "test-request-guard"
PHONE = "+46701234567"


class SmsStripe(CheckoutStripe):
    """CheckoutStripe plus closing a released booking's card page and
    deleting its Customer."""

    def __call__(self, method, path, params=None, *, account=None, idempotency_key=None, timeout=None):
        if path.endswith("/expire") or path.endswith("/cancel"):
            self.calls.append({"method": method, "path": path, "params": params or {}, "account": account,
                               "key": idempotency_key, "timeout": timeout})
            if self.down:
                raise shared_guarantee.stripe.StripeUnavailable("down")
            return {"id": path.split("/")[3], "status": "expired"}
        if method == "DELETE" and path.startswith("/v1/customers/"):
            self.calls.append({"method": method, "path": path, "params": params or {}, "account": account,
                               "key": idempotency_key, "timeout": timeout})
            return {"id": path.split("/")[3], "deleted": True}
        return super().__call__(method, path, params, account=account, idempotency_key=idempotency_key)


class FakeLambda:
    """notification:live as the create function sees it: `mode` is sent,
    dropped, function_error, client_error or timeout; `during` runs inside
    the invoke (the guest adds the card in that instant)."""

    def __init__(self):
        self.calls = []
        self.timeouts = []
        self.mode = "sent"
        self.during = None

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.during:
            self.during()
        if self.mode == "client_error":
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "Invoke")
        if self.mode == "timeout":
            raise ReadTimeoutError(endpoint_url="https://lambda.eu-north-1.amazonaws.com")
        if self.mode == "closed":
            raise ConnectionClosedError(endpoint_url="https://lambda.eu-north-1.amazonaws.com")
        if self.mode == "function_error":
            return {"StatusCode": 200, "FunctionError": "Unhandled",
                    "Payload": io.BytesIO(b'{"errorMessage": "sns down"}')}
        answer = {"status": "sent"} if self.mode == "sent" else {"status": "dropped", "reason": "bad_phone"}
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(answer).encode())}

    def notices(self):
        return [json.loads(c["Payload"])["notice"] for c in self.calls]


class Context:
    def __init__(self, millis):
        self.millis = millis

    def get_remaining_time_in_millis(self):
        return self.millis


@pytest.fixture
def gs(gc, monkeypatch):
    fake = SmsStripe()
    monkeypatch.setattr(shared_guarantee.stripe, "request", fake)
    gc["stripe"] = fake
    monkeypatch.setenv("NOTIFICATION_FUNCTION_NAME", NOTIFICATION)
    monkeypatch.setenv("SHORT_LINK_BASE", SHORT_BASE)
    lam = FakeLambda()

    def client(read_timeout=8):
        lam.timeouts.append(read_timeout)
        return lam

    monkeypatch.setattr(gc["m"]["create"], "_lambda_client", client)
    gc["lambda"] = lam
    return gc


def sms_book(env, context=None, **extra):
    payload = {"date": DAY, "startTime": "18:00", "tableIds": ["table-b"], "partySize": 3,
               "name": "Anna Svensson", "phone": PHONE, "source": "phone", "guarantee": "sms", **extra}
    response = env["m"]["create"].handler(
        event("POST", "/locations/{locationId}/reservations/manual", staff=STAFF_A, body=payload), context)
    return response, body_of(response)


def resend(env, rid, who=STAFF_A, location=LOC_A, context=None):
    return env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/{reservationId}/guarantee-link", location=location,
        reservation=rid, staff=who), context)


def checkout_rid(env):
    (session,) = [c for c in env["stripe"].calls if c["path"] == "/v1/checkout/sessions"]
    return session["params"]["client_reference_id"]


def rows(env):
    items = env["res"].Table(RESERVATION_TABLE).query(
        KeyConditionExpression=Key("PK").eq(f"LOCATION#{LOC_A}") & Key("SK").begins_with("RESERVATION#"))
    return items["Items"]


def stripe_paths(env):
    return [c["path"] for c in env["stripe"].calls]


# --- a new booking ---------------------------------------------------------------------------------

def test_staff_sms_booking_is_pending_and_texts_the_link_once(gs):
    response, body = sms_book(gs)

    assert response["statusCode"] == 201, body
    rid = body["reservationId"]
    item = stored(gs, rid)
    link = item["guaranteeLink"]
    assert body["status"] == item["status"] == "pending" and body["source"] == "phone"
    assert link["by"] == "booqy" and link["sends"] == 1 and link["page"] == "stripe"
    assert link["textedAt"] == "2026-09-08T12:00:00Z" and link["url"].startswith(SHORT_BASE + "/")
    code = link["url"].rsplit("/", 1)[1]
    row = gs["res"].Table(RESERVATION_TABLE).get_item(Key={"PK": f"LINK#{code}", "SK": "LINK"})["Item"]
    assert row["reservationId"] == rid                                   # the same target as the LINK# row
    assert "notice" not in item                                           # the stream sends nothing now
    assert len(markers(gs)) == 1 and "table-b" not in free_tables_at(gs, "18:00")

    (call,) = gs["lambda"].calls
    assert call["FunctionName"] == NOTIFICATION and call["InvocationType"] == "RequestResponse"
    (notice,) = gs["lambda"].notices()
    tenant_row = shared_tenant.get_tenant(TENANT_A)
    location = shared_tenant.get_location(TENANT_A, LOC_A)
    assert notice == {"version": 1, "kind": "reservations.guarantee-link", "tenantId": TENANT_A,
                      "locationId": LOC_A, "channel": "sms", "to": PHONE,
                      "text": messages.guarantee_link_sms(item, tenant_row, location, link["url"], 30),
                      "replyTo": None}
    assert link["url"] in notice["text"] and "30 minuter" in notice["text"]

    assert body["guaranteeLink"] == {"by": "booqy", "sends": 1, "textedAt": "2026-09-08T12:00:00Z",
                                     "canResend": True, "minutes": 30, "expiresAt": "2026-09-08T12:32:00Z",
                                     "page": "stripe", "sms": "sent"}
    assert link["url"] not in response["body"] and code not in response["body"]
    assert "cs_test_1" not in response["body"]


def test_sms_mode_small_party_or_policy_off_is_reserved_without_invoke(gs):
    _, small = sms_book(gs, partySize=1, tableIds=["table-a"])
    assert small["status"] == "reserved" and "guaranteeLink" not in small
    gs["res"].Table(LOCATION_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"}, UpdateExpression="REMOVE guarantee")
    shared_tenant.reset_caches()
    _, off = sms_book(gs)
    assert off["status"] == "reserved" and "guaranteeLink" not in off and "guaranteeSkipped" not in off
    assert gs["lambda"].calls == [] and gs["stripe"].calls == []
    assert stored(gs, off["reservationId"])["notice"]["type"] == "confirmed"   # the normal confirmation


def test_sms_mode_booking_starting_within_47_minutes_is_reserved_without_a_card(gs):
    gs["clock"].value = datetime(2026, 9, 20, 15, 14, tzinfo=timezone.utc)    # 17:14 local, 18:00 is 46 min away
    response, body = sms_book(gs)

    assert response["statusCode"] == 201 and body["status"] == "reserved"
    assert body["guaranteeSkipped"] == "too_soon" and "guaranteeLink" not in body
    item = stored(gs, body["reservationId"])
    assert item["history"][0]["cardSkipped"] == "too_soon" and item["notice"]["type"] == "confirmed"
    assert gs["lambda"].calls == [] and gs["stripe"].calls == []

    gs["clock"].value = datetime(2026, 9, 20, 15, 43, tzinfo=timezone.utc)    # 17:43: 18:30 is 47 min away
    _, fits = sms_book(gs, startTime="18:30", tableIds=["table-c"])
    assert fits["status"] == "pending" and "guaranteeSkipped" not in fits and len(gs["lambda"].calls) == 1


def test_sms_mode_honours_the_phone_agent_setting_skip(gs, monkeypatch):
    table = "test-phone-agent"
    gs["res"].create_table(
        TableName=table, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                              {"AttributeName": "SK", "AttributeType": "S"}])
    monkeypatch.setenv("PHONE_AGENT_TABLE_NAME", table)
    assert sms_book(gs)[1]["status"] == "pending"                          # no row: the default "link"
    gs["res"].Table(table).put_item(Item={"PK": f"TENANT#{TENANT_A}", "SK": f"LOC#{LOC_A}#CONFIG",
                                          "phoneGuarantee": "skip"})
    _, body = sms_book(gs, tableIds=["table-c"])
    assert body["status"] == "reserved" and "guaranteeLink" not in body and len(gs["lambda"].calls) == 1


@pytest.mark.parametrize("phone", ["+4681234567", "+4915112345678", "+4722123456", "+35891234567"])
def test_sms_mode_refuses_landline_and_foreign_numbers_before_stripe(gs, phone):
    response, body = sms_book(gs, phone=phone)

    assert response["statusCode"] == 400 and body == err("guarantee sms needs a mobile number")
    assert gs["stripe"].calls == [] and gs["lambda"].calls == []
    assert rows(gs) == [] and markers(gs) == [] and "table-b" in free_tables_at(gs, "18:00")


@pytest.mark.parametrize("phone", ["070-999 88 77", "070 000 00 00", "0701111111"])
def test_sms_mode_refuses_the_restaurants_own_number_and_fillers(gs, phone):
    gs["res"].Table(LOCATION_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"},
        UpdateExpression="SET phoneNumber = :p", ExpressionAttributeValues={":p": "070-999 88 77"})
    shared_tenant.reset_caches()
    response, body = sms_book(gs, phone=phone)
    assert response["statusCode"] == 400 and body == err("guarantee sms needs the guest's own mobile number")
    assert gs["stripe"].calls == [] and rows(gs) == []


def test_sms_mode_without_notification_env_is_503_before_stripe(gs, monkeypatch):
    monkeypatch.delenv("NOTIFICATION_FUNCTION_NAME")
    response, body = sms_book(gs)
    assert response["statusCode"] == 503 and body == {"error": "sms_unavailable"}
    assert gs["stripe"].calls == [] and rows(gs) == [] and "table-b" in free_tables_at(gs, "18:00")


@pytest.mark.parametrize("mode", ["dropped", "function_error", "client_error", "timeout"])
def test_failed_text_releases_the_booking_and_answers_502(gs, mode):
    gs["lambda"].mode = mode
    response, body = sms_book(gs)

    assert response["statusCode"] == 502 and body == {"error": "sms_failed"}
    rid = checkout_rid(gs)
    item = stored(gs, rid)
    assert item["status"] == "expired" and item["history"][-1]["action"] == "link_failed"
    assert item["history"][-1]["by"] == item["createdBy"] and "notice" not in item
    assert "table-b" in free_tables_at(gs, "18:00") and markers(gs) == []
    (close,) = [c for c in gs["stripe"].calls if c["path"].endswith("/expire")]
    assert close["path"] == "/v1/checkout/sessions/cs_test_1/expire" and close["account"] == ACCOUNT
    assert 1 <= close["timeout"] <= 5
    # The Customer made for a booking that never was (the guest's data) is deleted.
    (forget,) = [c for c in gs["stripe"].calls if c["method"] == "DELETE"]
    assert forget["path"] == f"/v1/customers/{item['stripeCustomerId']}" and forget["account"] == ACCOUNT
    assert 1 <= forget["timeout"] <= 5
    assert len(gs["lambda"].calls) == 1                                   # botocore retries are off
    # Fixed and booked again: a fresh booking and a fresh link.
    gs["lambda"].mode = "sent"
    assert sms_book(gs)[1]["status"] == "pending"


def test_failed_text_on_a_site_card_page_cancels_its_setup_intent(gs, monkeypatch):
    monkeypatch.setenv("PHONE_CARD_PAGE", "site")
    monkeypatch.setattr(gs["m"]["create"].site, "base", lambda tenant_id, row: ("https://bistro.se", None))
    gs["res"].Table(TENANT_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
        UpdateExpression="SET cardStepOnManagePage = :on", ExpressionAttributeValues={":on": True})
    shared_tenant.reset_caches()
    gs["lambda"].mode = "dropped"
    response, _ = sms_book(gs)
    assert response["statusCode"] == 502
    assert "/v1/setup_intents/seti_1/cancel" in stripe_paths(gs)


def test_release_conflict_after_card_returns_reserved(gs):
    def card_added():
        rid = checkout_rid(gs)
        _card_added(gs, rid)
        shared_guarantee.confirm_if_ready(stored(gs, rid), NOW, "guest")

    gs["lambda"].mode = "dropped"
    gs["lambda"].during = card_added
    response, body = sms_book(gs)

    assert response["statusCode"] == 201 and body["status"] == "reserved"
    item = stored(gs, body["reservationId"])
    assert item["status"] == "reserved" and item["stripePaymentMethodId"] == "pm_phone"
    assert not [p for p in stripe_paths(gs) if p.endswith("/expire")]   # the page stays as it was
    assert not [c for c in gs["stripe"].calls if c["method"] == "DELETE"]
    assert "url" not in body["guaranteeLink"] and body["guaranteeLink"]["canResend"] is False


def _failing_release(gs, monkeypatch, failure):
    """The release transaction fails with `failure()` every time; the
    booking's own write goes through."""
    mod = gs["m"]["create"]
    real = mod.r.transact
    release = [{"Release": "the booking"}]

    def transact(ops):
        if ops == release:
            raise failure()
        return real(ops)

    monkeypatch.setattr(mod.guarantee, "expire_items", lambda *args, **kwargs: release)
    monkeypatch.setattr(mod.r, "transact", transact)


@pytest.mark.parametrize("failure", [
    lambda: ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
                        "TransactWriteItems"),
    lambda: shared_r.Conflict("changed"),
], ids=["dynamodb_error", "conflict_twice"])
def test_a_failed_release_is_logged_and_keeps_its_card_page(gs, monkeypatch, caplog, failure):
    gs["lambda"].mode = "dropped"
    _failing_release(gs, monkeypatch, failure)
    with caplog.at_level(logging.INFO):
        response, body = sms_book(gs)

    assert response["statusCode"] == 502 and body == {"error": "sms_failed"}
    item = stored(gs, checkout_rid(gs))
    assert item["status"] == "pending" and markers(gs)                   # left to the expiry sweep
    # Its card page stays open and its Customer kept: a booking that still
    # holds tables keeps a working link (the resend), never a dead one.
    assert not [c for c in gs["stripe"].calls if c["path"].endswith("/expire") or c["method"] == "DELETE"]
    errors = [json.loads(rec.getMessage()) for rec in caplog.records if rec.levelno == logging.ERROR]
    assert [e["guaranteeLinkRelease"] for e in errors] == ["failed"]
    assert errors[0]["reservationId"] == item["reservationId"] and errors[0]["reason"]


def test_logs_never_carry_the_phone_the_text_or_the_link(gs, caplog):
    secrets = []
    with caplog.at_level(logging.INFO):
        for mode, table in (("sent", "table-b"), ("dropped", "table-c"), ("function_error", "table-c"),
                            ("client_error", "table-c"), ("timeout", "table-c"), ("closed", "table-c")):
            gs["lambda"].mode = mode
            sms_book(gs, tableIds=[table])
        rid = stored(gs, checkout_rid_first(gs))["reservationId"]
        gs["lambda"].mode = "timeout"
        resend(gs, rid)
    for notice in gs["lambda"].notices():
        url = notice["text"].split("https://", 1)[1].split()[0]
        secrets += [notice["text"], "https://" + url, url.rsplit("/", 1)[1]]
    # Booqy's own log lines (botocore and moto debug lines are not shipped at INFO).
    logged = "\n".join(rec.getMessage() for rec in caplog.records
                       if not rec.name.startswith(("botocore", "boto3", "moto", "urllib3")))
    assert "guaranteeLinkSms" in logged and "guaranteeLinkPage" in logged
    for needle in [PHONE, PHONE[1:], "0701234567", "cs_test_1", "checkout.stripe.com", *secrets]:
        assert needle not in logged, needle


def checkout_rid_first(env):
    return [c for c in env["stripe"].calls if c["path"] == "/v1/checkout/sessions"][0]["params"][
        "client_reference_id"]


def test_short_of_time_nothing_is_sent_and_the_booking_is_released(gs):
    response, body = sms_book(gs, context=Context(5500))
    assert response["statusCode"] == 502 and body == {"error": "sms_failed"}
    assert gs["lambda"].calls == [] and stored(gs, checkout_rid(gs))["status"] == "expired"


def test_the_read_timeout_follows_the_time_left(gs):
    sms_book(gs, context=Context(10000))
    sms_book(gs, context=Context(25000), tableIds=["table-c"])
    sms_book(gs, tableIds=["table-a"], partySize=2)
    assert gs["lambda"].timeouts == [6, 8, 8]


@pytest.mark.parametrize("extra, message", [
    ({"source": "walk_in"}, "guarantee sms is for phone bookings"),
    ({"source": "staff"}, "guarantee sms is for phone bookings"),
    ({"phone": None}, "guarantee sms needs the guest's phone number"),
    ({"source": "walk_in", "guarantee": "waived"}, "guarantee waived is for phone bookings"),
    ({"guarantee": "deposit"}, "guarantee must be link or sms"),
])
def test_sms_mode_is_for_phone_bookings(gs, extra, message):
    response, body = sms_book(gs, **extra)
    assert response["statusCode"] == 400 and body == err(message)
    assert gs["stripe"].calls == [] and gs["lambda"].calls == []


def test_waived_books_reserved_and_logs_it(gs):
    _, body = sms_book(gs, guarantee="waived")
    assert body["status"] == "reserved" and "guaranteeLink" not in body
    item = stored(gs, body["reservationId"])
    assert [h["action"] for h in item["history"]] == ["created", "guarantee_waived"]
    assert item["history"][1]["by"] == item["createdBy"] and item["notice"]["type"] == "confirmed"
    assert gs["stripe"].calls == [] and gs["lambda"].calls == []
    _, small = sms_book(gs, guarantee="waived", partySize=1, tableIds=["table-a"])
    assert [h["action"] for h in stored(gs, small["reservationId"])["history"]] == ["created"]   # nothing waived


def test_waived_needs_no_phone_check_of_its_own(gs):
    response, body = sms_book(gs, guarantee="waived", phone=None)
    assert response["statusCode"] == 400 and body == err("phone is required for a phone booking")


# --- request_guard limits ---------------------------------------------------------------------------

@pytest.fixture
def guarded(gs, monkeypatch):
    gs["res"].create_table(
        TableName=GUARD_TABLE, KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
    monkeypatch.setenv("REQUEST_GUARD_TABLE_NAME", GUARD_TABLE)
    return gs


def attempts(env):
    return sorted((row["PK"].split("#")[0], int(row["attempts"]))
                  for row in env["res"].Table(GUARD_TABLE).scan()["Items"])


def test_link_sms_are_limited_per_staff_user_and_location(guarded, monkeypatch):
    mod = guarded["m"]["create"]
    monkeypatch.setattr(mod, "LINK_SMS_STAFF_LIMIT", (2, 3600))
    assert [sms_book(guarded, tableIds=[t])[0]["statusCode"] for t in ("table-b", "table-c")] == [201, 201]
    limited, body = sms_book(guarded, startTime="20:00")
    assert limited["statusCode"] == 429 and body == {"error": "sms_limit", "retryAfter": body["retryAfter"]}
    assert 0 < int(limited["headers"]["Retry-After"]) == body["retryAfter"] <= 3600   # the body for the admin (CORS)
    assert len(guarded["stripe"].keys("/v1/checkout/sessions")) == 2 and len(guarded["lambda"].calls) == 2
    assert attempts(guarded) == [("link-sms-loc", 2), ("link-sms-sub", 2)]
    monkeypatch.setattr(mod, "LINK_SMS_STAFF_LIMIT", (20, 3600))
    monkeypatch.setattr(mod, "LINK_SMS_LOCATION_LIMIT", (2, 86400))
    day_limited = sms_book(guarded, startTime="20:00")[1]
    assert day_limited["error"] == "sms_limit" and 3600 < day_limited["retryAfter"] <= 86400
    assert sms_book(guarded, startTime="20:00", partySize=1, tableIds=["table-a"])[0]["statusCode"] == 201
    text = json.dumps(guarded["res"].Table(GUARD_TABLE).scan()["Items"], default=str)
    assert LOC_A not in text and "46701234567" not in text                 # hashes only


def test_link_sms_counters_are_given_back_when_nothing_was_sent(guarded):
    sms_book(guarded)                                                     # counted: sent
    assert sms_book(guarded)[1] == err("table_taken")                     # nothing booked
    guarded["lambda"].mode = "dropped"
    assert sms_book(guarded, tableIds=["table-c"])[1] == {"error": "sms_failed"}
    guarded["lambda"].mode = "timeout"                                    # it may have gone out: kept
    assert sms_book(guarded, tableIds=["table-c"])[1] == {"error": "sms_failed"}
    assert attempts(guarded) == [("link-sms-loc", 2), ("link-sms-sub", 2)]
    guarded["lambda"].mode = "closed"                                     # the request went out: kept too
    assert sms_book(guarded, tableIds=["table-c"])[1] == {"error": "sms_failed"}
    assert attempts(guarded) == [("link-sms-loc", 3), ("link-sms-sub", 3)]


@pytest.mark.parametrize("mode, kept", [("timeout", True), ("closed", True), ("dropped", False),
                                        ("function_error", False), ("client_error", False)])
def test_a_failed_resend_keeps_the_counters_only_when_it_may_have_gone_out(guarded, mode, kept):
    rid = booked(guarded)
    assert attempts(guarded) == [("link-sms-loc", 1), ("link-sms-sub", 1)]
    guarded["lambda"].mode = mode
    assert body_of(resend(guarded, rid)) == {"error": "sms_failed"}
    count = 2 if kept else 1
    assert attempts(guarded) == [("link-sms-loc", count), ("link-sms-sub", count)]
    assert stored(guarded, rid)["guaranteeLink"]["sends"] == 2         # the booking's own counter stays used


# --- sending the link again --------------------------------------------------------------------------

def booked(env):
    response, body = sms_book(env)
    assert response["statusCode"] == 201, body
    return body["reservationId"]


def test_resend_texts_the_same_link_with_minutes_left(gs):
    rid = booked(gs)
    url = stored(gs, rid)["guaranteeLink"]["url"]
    gs["clock"].value = NOW + timedelta(minutes=10)

    response = resend(gs, rid)

    assert response["statusCode"] == 200, response
    body = body_of(response)
    assert body["status"] == "pending" and body["guaranteeLink"] == {
        "by": "booqy", "sends": 2, "textedAt": "2026-09-08T12:10:00Z", "canResend": True}
    first, again = gs["lambda"].notices()
    assert again["to"] == PHONE and again["kind"] == "reservations.guarantee-link"
    assert url in again["text"] and "20 minuter" in again["text"] and "30 minuter" in first["text"]
    item = stored(gs, rid)
    assert item["guaranteeLink"]["sends"] == 2 and item["guaranteeLink"]["url"] == url
    assert item["history"][-1]["action"] == "link_resent" and item["history"][-1]["sends"] == 2
    assert int(item["version"]) == 2 and "notice" not in item
    assert url not in response["body"]


def test_resend_works_for_an_agent_booking(gs):
    _, body = phone_book(gs)                                              # guarantee "link": the agent texted it
    response = resend(gs, body["reservationId"])
    assert response["statusCode"] == 200
    assert body_of(response)["guaranteeLink"]["by"] == "caller" and len(gs["lambda"].calls) == 1


def test_resend_limit_and_closing_window(gs):
    rid = booked(gs)
    assert resend(gs, rid)["statusCode"] == 200
    assert body_of(resend(gs, rid))["guaranteeLink"]["canResend"] is False   # the third and last SMS
    assert body_of(resend(gs, rid)) == {"error": "resend_limit"}
    assert len(gs["lambda"].calls) == 3

    second = sms_book(gs, tableIds=["table-c"])[1]["reservationId"]
    gs["clock"].value = NOW + timedelta(minutes=27, seconds=30)           # 2 of the 30 minutes left
    assert body_of(resend(gs, second)) == {"error": "link_closed"}
    gs["clock"].value = NOW + timedelta(minutes=26)                        # 4 left
    assert resend(gs, second)["statusCode"] == 200


def test_resend_refuses_what_is_not_pending_or_not_a_mobile(gs):
    rid = booked(gs)
    gs["res"].Table(RESERVATION_TABLE).update_item(
        Key={"PK": f"LOCATION#{LOC_A}", "SK": f"RESERVATION#{DAY}#{rid}"},
        UpdateExpression="SET customerPhone = :p", ExpressionAttributeValues={":p": "+4681234567"})
    assert body_of(resend(gs, rid)) == {"error": "not_textable"}
    _, plain = sms_book(gs, partySize=1, tableIds=["table-a"])
    assert body_of(resend(gs, plain["reservationId"])) == {"error": "not_pending"}
    assert set_status(gs, rid, "cancelled")["statusCode"] == 200
    assert body_of(resend(gs, rid)) == {"error": "not_pending"}
    assert len(gs["lambda"].calls) == 1


@pytest.mark.parametrize("phone", ["+46700000000", "+46709998877"])
def test_resend_refuses_a_filler_or_the_restaurants_own_number(gs, phone):
    rid = booked(gs)
    gs["res"].Table(LOCATION_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"},
        UpdateExpression="SET phoneNumber = :p", ExpressionAttributeValues={":p": "070-999 88 77"})
    shared_tenant.reset_caches()
    # Staff edited the number of the pending booking after it was made.
    gs["res"].Table(RESERVATION_TABLE).update_item(
        Key={"PK": f"LOCATION#{LOC_A}", "SK": f"RESERVATION#{DAY}#{rid}"},
        UpdateExpression="SET customerPhone = :p", ExpressionAttributeValues={":p": phone})
    assert body_of(resend(gs, rid)) == {"error": "not_textable"}
    assert len(gs["lambda"].calls) == 1 and stored(gs, rid)["guaranteeLink"]["sends"] == 1


def test_resend_without_notification_env_is_503(gs, monkeypatch):
    rid = booked(gs)
    monkeypatch.delenv("NOTIFICATION_FUNCTION_NAME")
    assert body_of(resend(gs, rid)) == {"error": "sms_unavailable"}
    assert stored(gs, rid)["guaranteeLink"]["sends"] == 1


def test_resend_claim_conflict_is_409(gs, monkeypatch):
    rid = booked(gs)
    real = shared_r.load

    def raced(location_id, reservation_id):
        item = real(location_id, reservation_id)
        # The other click claimed the second SMS between this read and the claim.
        gs["res"].Table(RESERVATION_TABLE).update_item(
            Key={"PK": f"LOCATION#{LOC_A}", "SK": f"RESERVATION#{DAY}#{rid}"},
            UpdateExpression="SET guaranteeLink.sends = :two", ExpressionAttributeValues={":two": 2})
        return item

    monkeypatch.setattr(shared_r, "load", raced)
    response = resend(gs, rid)
    assert response["statusCode"] == 409 and body_of(response) == {"error": "changed_retry"}
    assert len(gs["lambda"].calls) == 1


def test_resend_failure_keeps_booking_pending_and_counts(gs):
    rid = booked(gs)
    gs["lambda"].mode = "function_error"
    response = resend(gs, rid)
    assert response["statusCode"] == 502 and body_of(response) == {"error": "sms_failed"}
    item = stored(gs, rid)
    assert item["status"] == "pending" and item["guaranteeLink"]["sends"] == 2 and markers(gs)
    assert item["history"][-1]["action"] == "link_resent"


def test_resend_counts_against_the_sms_limits(guarded, monkeypatch):
    rid = booked(guarded)
    monkeypatch.setattr(guarded["m"]["create"], "LINK_SMS_STAFF_LIMIT", (2, 3600))
    assert resend(guarded, rid)["statusCode"] == 200
    limited = body_of(resend(guarded, rid))
    assert limited["error"] == "sms_limit" and 0 < limited["retryAfter"] <= 3600
    assert stored(guarded, rid)["guaranteeLink"]["sends"] == 2


def test_resend_other_location_is_404(gs):
    rid = booked(gs)
    assert resend(gs, rid, location=LOC_A2)["statusCode"] == 404                    # staff of LOC_A only
    assert resend(gs, rid, who=OWNER_A, location=LOC_A2)["statusCode"] == 404       # not that location's
    assert resend(gs, "f" * 32)["statusCode"] == 404
    assert resend(gs, rid, who=OWNER_B)["statusCode"] == 404                        # another tenant's
    assert len(gs["lambda"].calls) == 1


def test_resend_route_is_post_only(gs):
    response = gs["m"]["create"].handler(event(
        "GET", "/locations/{locationId}/reservations/{reservationId}/guarantee-link", reservation="r" * 32,
        staff=STAFF_A), None)
    assert response["statusCode"] == 405 and response["headers"]["Allow"] == "POST"


# --- views ----------------------------------------------------------------------------------------------

def test_staff_views_never_carry_the_link_url(gs):
    response, body = sms_book(gs)
    rid = body["reservationId"]
    url = stored(gs, rid)["guaranteeLink"]["url"]
    one = gs["m"]["get"].handler(event("GET", "/locations/{locationId}/reservations/{reservationId}",
                                       reservation=rid, staff=OWNER_A), None)
    listed = staff_list(gs)
    for answer in (response, one, listed):
        assert answer["statusCode"] in (200, 201)
        assert url not in answer["body"] and url.rsplit("/", 1)[1] not in answer["body"]
        assert "checkout.stripe.com" not in answer["body"]
    (item,) = [i for i in body_of(listed)["items"] if i["reservationId"] == rid]
    assert item["guaranteeLink"] == {"by": "booqy", "sends": 1, "textedAt": "2026-09-08T12:00:00Z",
                                     "canResend": True}
    assert body_of(one)["guaranteeLink"] == item["guaranteeLink"]
    cancelled = set_status(gs, rid, "cancelled")
    assert url not in cancelled["body"] and body_of(cancelled)["guaranteeLink"]["canResend"] is False


def test_link_view_rules():
    now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    item = {"status": "pending", "pendingExpiresAt": "2026-09-08T12:32:00Z", "customerPhone": PHONE,
            "guaranteeLink": {"url": "https://x/k/a", "by": "caller", "sends": 1}}
    assert shared_guarantee.link_view(item, now) == {"by": "caller", "sends": 1, "canResend": True}
    assert shared_guarantee.link_minutes_left(item, now) == 30
    assert shared_guarantee.link_minutes_left(item, now + timedelta(minutes=29, seconds=30)) == 0
    assert shared_guarantee.link_minutes_left({}, now) == 0
    assert shared_guarantee.link_view({**item, "customerPhone": "+4681234567"}, now)["canResend"] is False
    assert shared_guarantee.link_view({**item, "customerPhone": "+46700000000"}, now)["canResend"] is False
    assert shared_guarantee.link_view({**item, "status": "reserved"}, now)["canResend"] is False
    assert shared_guarantee.link_view({**item, "guaranteeLink": {"sends": 3, "url": "u"}}, now)["canResend"] is False
    assert shared_guarantee.link_view({**item, "guaranteeLink": {"sends": 1}}, now)["canResend"] is False
    assert shared_guarantee.link_view(item, now + timedelta(minutes=28))["canResend"] is False
    assert shared_guarantee.link_view({"status": "pending"}, now) is None
    start = now + timedelta(minutes=47)
    assert shared_guarantee.phone_link_fits(now, start) and not shared_guarantee.phone_link_fits(
        now, start - timedelta(seconds=1))


# --- cancelling a pending booking, the GDPR export ------------------------------------------------------

def test_cancelling_a_pending_booking_tells_the_guest_nothing(gs):
    rid = booked(gs)
    response = set_status(gs, rid, "cancelled")
    assert response["statusCode"] == 200
    item = stored(gs, rid)
    assert item["status"] == "cancelled_by_restaurant" and "notice" not in item
    assert "table-b" in free_tables_at(gs, "18:00") and markers(gs) == []
    # the guest's card page closes with it (best effort)
    assert any(c["path"] == "/v1/checkout/sessions/cs_test_1/expire" for c in gs["stripe"].calls)


def test_cancelling_a_pending_booking_stands_when_the_card_page_cannot_close(gs):
    rid = booked(gs)
    gs["stripe"].down = True
    response = set_status(gs, rid, "cancelled")
    assert response["statusCode"] == 200
    assert stored(gs, rid)["status"] == "cancelled_by_restaurant"


def test_the_guest_data_export_leaves_out_the_card_link_url():
    privacy = load("guest-privacy", "gp_export_link")
    row = {"PK": "LOCATION#l", "SK": "RESERVATION#d#r", "ttl": 1, "customerPhone": PHONE,
           "guaranteeLink": {"url": "https://restaurang.booqy.se/api/k/Ab3dE5fG7h", "page": "stripe",
                             "by": "booqy", "sends": 2, "textedAt": "2026-09-08T12:00:00Z"}}
    view = privacy._export_view(row)
    assert view["guaranteeLink"] == {"page": "stripe", "by": "booqy", "sends": 2,
                                     "textedAt": "2026-09-08T12:00:00Z"}
    assert "Ab3dE5fG7h" not in json.dumps(view) and view["customerPhone"] == PHONE
