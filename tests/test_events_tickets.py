"""Events and tickets (feature K, ADR-K, CONTRACTS.md 6.K, 9.2): routes K1-K22,
the stripe.event / accounting.events / insights.events / privacy / purge
hooks, the sweep and nightly jobs and the two-tenant isolation suite.
Stripe and notification are fakes; nothing leaves the test."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from shared import accounting, events
from shared import stripe_client as stripe
from shared.features import events_tickets as et
from shared.features.events_tickets import qr, series
from tenant_support import (LOC_A, LOC_A2, LOC_B, REGION, TENANT_A, TENANT_B, features_on, location_row,
                            seed_tenancy, tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
TABLE = "test-events-tickets"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
ACCT_A, ACCT_B = "acct_aaaaaaaaaaaaaa", "acct_bbbbbbbbbbbbbb"
E1, E2, EB = "01EVENTAAAAAAAAAAAAAAAAAAA", "01EVENTSERIESAAAAAAAAAAAAA", "01EVENTBBBBBBBBBBBBBBBBBBB"
T1, T2 = "01TYPEDINNERAAAAAAAAAAAAAA", "01TYPEEARLYAAAAAAAAAAAAAAA"
START = "2026-12-04T18:00:00Z"
OCC1 = f"{E1}-20261204T1800Z"


def load(name):
    spec = importlib.util.spec_from_file_location(f"et_{name.replace('-', '_')}", ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeStripe:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.known_refunds = []  # what GET /v1/refunds answers (the 24 h lookup)

    def __call__(self, method, path, params=None, *, account=None, idempotency_key=None):
        self.calls.append({"method": method, "path": path, "params": params, "account": account,
                           "key": idempotency_key})
        if self.fail:
            raise stripe.StripeUnavailable("down")
        if method == "GET" and path == "/v1/refunds":
            return {"data": [r for r in self.known_refunds if r.get("payment_intent") == params["payment_intent"]]}
        if path == "/v1/payment_intents":
            pid = f"pi_{idempotency_key[-12:]}"
            return {"id": pid, "client_secret": f"{pid}_secret", "amount": params["amount"]}
        if method == "GET" and path.startswith("/v1/payment_intents/"):
            pid = path.rsplit("/", 1)[-1]
            return {"id": pid, "client_secret": f"{pid}_secret", "status": "requires_payment_method"}
        if path.endswith("/cancel"):
            return {"id": path.split("/")[3], "status": "canceled"}
        return {"id": f"re_{len(self.calls)}", "status": "succeeded"}

    def refunds(self):
        return [c for c in self.calls if c["path"] == "/v1/refunds" and c["method"] == "POST"]


class FakeContext:
    """A Lambda context whose clock the test moves (ms left)."""

    def __init__(self, remaining_ms):
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self.remaining_ms


def guard_table(monkeypatch):
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName="test-guard", KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
    monkeypatch.setenv("REQUEST_GUARD_TABLE_NAME", "test-guard")


class FakeLambda:
    def __init__(self):
        self.calls = []
        self.status = 202

    def invoke(self, **kwargs):
        self.calls.append(json.loads(kwargs["Payload"]))
        return {"StatusCode": self.status}

    def kinds(self):
        return [c["notice"]["kind"] for c in self.calls]


def _create_table():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"}
                              for a in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")],
        GlobalSecondaryIndexes=[
            {"IndexName": "byOccurrence", "Projection": {"ProjectionType": "ALL"},
             "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                           {"AttributeName": "GSI1SK", "KeyType": "RANGE"}]},
            {"IndexName": "byLocationDay", "Projection": {"ProjectionType": "ALL"},
             "KeySchema": [{"AttributeName": "GSI2PK", "KeyType": "HASH"},
                           {"AttributeName": "GSI2SK", "KeyType": "RANGE"}]}])


def no_batch_get(monkeypatch):
    """The Lambda role (infra) grants GetItem/PutItem/UpdateItem/DeleteItem/Query
    only: any BatchGetItem in these tests fails like AccessDenied would, and so
    does a transaction with a ConditionCheck entry (dynamodb:ConditionCheckItem
    is not in the policy either)."""
    import botocore.client
    real = botocore.client.BaseClient._make_api_call

    def guarded(self, operation_name, api_params):
        if operation_name == "BatchGetItem":
            raise AssertionError("BatchGetItem is not granted to the events-tickets Lambda")
        if operation_name == "TransactWriteItems" and any("ConditionCheck" in i for i in api_params.get("TransactItems", [])):
            raise AssertionError("ConditionCheckItem is not granted to the events-tickets Lambda")
        return real(self, operation_name, api_params)
    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", guarded)


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
        monkeypatch.setenv("ENVIRONMENT", "dev")
        monkeypatch.setenv("EVENTS_TICKETS_TABLE_NAME", TABLE)
        monkeypatch.setenv("NOTIFICATION_FUNCTION_NAME", "dev-notification:live")
        monkeypatch.setenv("CUSTOMER_SITE_URL", "https://dev-site.example")
        monkeypatch.setenv("USER_TABLE_NAME", "test-user")
        secret = boto3.client("secretsmanager", region_name=REGION).create_secret(Name="pepper", SecretString="q" * 48)
        monkeypatch.setenv("EVENTS_TICKETS_PEPPER_SECRET_ARN", secret["ARN"])
        on = features_on("events")
        seed_tenancy(monkeypatch,
                     tenant_a=tenant_row(TENANT_A, features=on, slug="resta",
                                         stripe={"accountId": ACCT_A, "chargesEnabled": True}),
                     tenant_b=tenant_row(TENANT_B, features=on, slug="restb",
                                         stripe={"accountId": ACCT_B, "chargesEnabled": True}),
                     locations_a=[location_row(TENANT_A, LOC_A, timezone="Europe/Stockholm"),
                                  location_row(TENANT_A, LOC_A2)],
                     locations_b=[location_row(TENANT_B, LOC_B)])
        _create_table()
        no_batch_get(monkeypatch)
        from shared import dynamo
        dynamo._resource = None
        dynamo._client = None
        et.reset_pepper()
        fake = FakeStripe()
        monkeypatch.setattr(stripe, "request", fake)
        clock = SimpleNamespace(value=NOW)
        monkeypatch.setattr(et, "now", lambda: clock.value)
        published = []
        monkeypatch.setattr(events, "publish", lambda f, t, d: published.append((t, d)) or "id")
        api = load("events-tickets")
        mail = FakeLambda()
        api._clients["lambda"] = mail
        yield SimpleNamespace(api=api, stripe=fake, mail=mail, clock=clock, published=published,
                              table=boto3.resource("dynamodb", region_name=REGION).Table(TABLE))
        dynamo._resource = None
        dynamo._client = None


# --- helpers -------------------------------------------------------------------------------------

OWNER_A, OWNER_B, STAFF_A = (TENANT_A, "owner_user"), (TENANT_B, "owner_user"), (TENANT_A, "staff_user")
L = "/locations/{locationId}/events"
OCC = L + "/{eventId}/occurrences/{occurrenceId}"


def call(e, method, template, *, params=None, body=None, query=None, claims=None):
    event = {"routeKey": f"{method} {template}", "pathParameters": dict(params or {}), "queryStringParameters": query,
             "headers": {}, "requestContext": {"http": {"method": method, "sourceIp": "203.0.113.5"}},
             "body": json.dumps(body) if body is not None else None}
    if claims:
        with_tenant_claims(event, *claims)
    response = e.api.handler(event, None)
    return response["statusCode"], (json.loads(response["body"]) if response.get("body") else None)


def event_body(event_id=E1, **over):
    body = {"eventId": event_id, "title": "Vinmiddag", "description": "Fem rätter och viner.",
            "occurrences": [{"startAt": START, "endAt": "2026-12-04T22:00:00Z", "capacity": 4}],
            "ticketTypes": [{"typeId": T1, "name": "Middag", "priceOre": 89500, "vatRate": 12, "perOrderMax": 4,
                             "sortOrder": 1}],
            "settings": {"perOrderMax": 6, "transferAllowed": True}}
    body.update(over)
    return body


def create(e, claims=OWNER_A, loc=LOC_A, **over):
    return call(e, "POST", L, params={"locationId": loc}, body=event_body(**over), claims=claims)


def publish(e, event_id=E1, version=1, loc=LOC_A, claims=OWNER_A):
    return call(e, "POST", L + "/{eventId}/publish", params={"locationId": loc, "eventId": event_id},
                body={"version": version}, claims=claims)


def published(e, **over):
    assert create(e, **over)[0] == 201
    status, body = publish(e, event_id=over.get("event_id", E1))
    assert status == 200, body
    return body


def buy(e, order_id="01ORDERAAAAAAAAAAAAAAAAAAA", qty=2, occ=OCC1, loc=LOC_A, type_id=T1, **over):
    body = {"orderId": order_id, "occurrenceId": occ, "lines": [{"typeId": type_id, "qty": qty}],
            "buyerName": "Sara Svensson", "buyerEmail": "Sara@Example.se", **over}
    return call(e, "POST", "/public/locations/{locationId}/events/checkout", params={"locationId": loc}, body=body)


def pay(e, order_id="01ORDERAAAAAAAAAAAAAAAAAAA", tenant_id=TENANT_A, account=ACCT_A, kind="payment_intent.succeeded"):
    order = e.table.get_item(Key=et.order_key(tenant_id, order_id))["Item"]
    obj = {"id": order.get("paymentIntentId"), "amount": int(order["amountOre"]),
           "metadata": {"kind": "ticket", "tenantId": tenant_id, "orderId": order_id}}
    return e.api.handler({"hook": "stripe.event", "hookVersion": 1, "tenantId": tenant_id, "type": kind,
                          "account": account, "object": obj})


def occ_row(e, tenant_id=TENANT_A, event_id=E1, start="20261204T1800Z"):
    return e.table.get_item(Key=et.occ_key(tenant_id, event_id, start))["Item"]


def tickets(e, tenant_id=TENANT_A):
    return e.table.query(KeyConditionExpression="PK = :p AND begins_with(SK, :s)",
                         ExpressionAttributeValues={":p": f"TENANT#{tenant_id}", ":s": "TICKET#"})["Items"]


def sold(e):
    assert published(e)
    assert buy(e)[0] == 200
    assert pay(e)["result"] == "issued"
    return tickets(e)


def token_of(e, order_id="01ORDERAAAAAAAAAAAAAAAAAAA"):
    return qr.token("o", TENANT_A, order_id)


# --- K1-K5 events --------------------------------------------------------------------------------

def test_create_get_list_publish(env):
    status, body = create(env)
    assert status == 201 and body["status"] == "draft" and body["occurrences"][0]["occurrenceId"] == OCC1
    assert body["occurrences"][0]["capacity"] == 4 and body["ticketTypes"][0]["priceOre"] == 89500
    status, listing = call(env, "GET", L, params={"locationId": LOC_A}, claims=STAFF_A)
    assert status == 200 and listing["events"][0]["eventId"] == E1 and listing["events"][0]["capacity"] == 4
    status, body = publish(env)
    assert status == 200 and body["status"] == "published" and body["version"] == 2
    assert [p[0] for p in env.published] == ["Event.Published"]


def test_create_validation(env):
    bad_capacity = [{"startAt": START, "endAt": "2026-12-04T22:00:00Z", "capacity": 0}]
    assert create(env, occurrences=bad_capacity) == (400, {"error": "invalid_capacity", "code": "invalid_capacity",
                                                           "field": "capacity"})
    bad_price = [{"typeId": T1, "name": "x", "priceOre": -1, "vatRate": 12}]
    assert create(env, ticketTypes=bad_price)[1]["code"] == "invalid_price"
    bad_vat = [{"typeId": T1, "name": "x", "priceOre": 100, "vatRate": 7}]
    assert create(env, ticketTypes=bad_vat)[1]["code"] == "invalid_vat_rate"
    too_long = {"freq": "weekly", "until": "2028-01-01"}
    assert create(env, series=too_long)[1]["code"] == "invalid_series"
    window = [{"startAt": START, "endAt": "2026-12-04T22:00:00Z", "capacity": 4,
               "salesCloseAt": "2026-12-05T00:00:00Z"}]
    assert create(env, occurrences=window)[1]["code"] == "invalid_window"
    assert create(env, imageKey="x.jpg")[1]["code"] == "unknown_image"
    assert create(env, settings={"tablePick": True})[1]["code"] == "table_pick_not_available"


def test_create_owner_only_foreign_and_retry(env):
    assert create(env, claims=STAFF_A)[0] == 403
    assert create(env, claims=OWNER_B)[0] == 404  # LOC_A is A's
    assert create(env)[0] == 201
    status, body = create(env)  # same eventId: the same event, nothing new
    assert status == 201 and len(body["occurrences"]) == 1


def test_entitlement_off_refused(env):
    from shared import tenant as shared_tenant
    boto3.resource("dynamodb", region_name=REGION).Table("test-tenant").put_item(
        Item=tenant_row(TENANT_A, features=features_on(), stripe={"accountId": ACCT_A}))
    shared_tenant.reset_caches()
    assert create(env)[1] == {"error": "feature_not_in_plan"}
    assert call(env, "GET", L, params={"locationId": LOC_A}, claims=OWNER_A)[0] == 403
    assert call(env, "GET", "/public/locations/{locationId}/events", params={"locationId": LOC_A})[0] == 404
    assert buy(env)[0] == 404


def test_get_staff_has_no_revenue_and_foreign_404(env):
    sold(env)
    _, owner = call(env, "GET", L + "/{eventId}", params={"locationId": LOC_A, "eventId": E1}, claims=OWNER_A)
    _, staff = call(env, "GET", L + "/{eventId}", params={"locationId": LOC_A, "eventId": E1}, claims=STAFF_A)
    assert owner["occurrences"][0]["revenueOre"] == 179000 and "revenueOre" not in staff["occurrences"][0]
    assert call(env, "GET", L + "/{eventId}", params={"locationId": LOC_B, "eventId": E1}, claims=OWNER_B)[0] == 404
    assert call(env, "GET", L + "/{eventId}", params={"locationId": LOC_A2, "eventId": E1}, claims=OWNER_A)[0] == 404


def test_update_version_and_publish_twice(env):
    create(env)
    p = {"locationId": LOC_A, "eventId": E1}
    status, body = call(env, "PUT", L + "/{eventId}", params=p, claims=OWNER_A,
                        body={"title": "Ny titel", "description": "", "settings": {}, "version": 1})
    assert status == 200 and body["title"] == "Ny titel" and body["version"] == 2
    stale = call(env, "PUT", L + "/{eventId}", params=p, claims=OWNER_A,
                 body={"title": "X", "description": "", "settings": {}, "version": 1})
    assert stale[0] == 409 and stale[1]["code"] == "changed_retry"
    assert call(env, "PUT", L + "/{eventId}", params=p, claims=STAFF_A,
                body={"title": "X", "version": 2})[0] == 403
    assert publish(env, version=2)[0] == 200
    status, body = publish(env, version=2)
    assert status == 200 and body["status"] == "published" and len(body["occurrences"]) == 1
    assert [p[0] for p in env.published] == ["Event.Published"]


def test_series_generated_once_on_publish(env):
    create(env, event_id=E2, series={"freq": "weekly", "until": "2027-01-01", "exceptions": ["2026-12-25"]},
           occurrences=[{"startAt": START, "endAt": "2026-12-04T22:00:00Z", "capacity": 10,
                         "salesCloseAt": "2026-12-04T16:00:00Z"}])
    status, body = publish(env, event_id=E2)
    starts = [o["startAt"] for o in body["occurrences"]]
    assert status == 200 and starts == ["2026-12-04T18:00:00Z", "2026-12-11T18:00:00Z", "2026-12-18T18:00:00Z",
                                        "2027-01-01T18:00:00Z"]
    assert body["occurrences"][1]["salesCloseAt"] == "2026-12-11T16:00:00Z"
    assert publish(env, event_id=E2, version=2)[1]["occurrences"] == body["occurrences"]


def test_series_keeps_local_time_and_monthly_weekday():
    tz = et.zone({"timezone": "Europe/Stockholm"})
    first = datetime(2027, 3, 19, 18, 0, tzinfo=timezone.utc)  # 19:00 CET, a 3rd Friday
    weekly = series.starts(first, timedelta(hours=3), {"freq": "weekly", "until": "2027-04-02"}, tz)
    assert [s.astimezone(tz).strftime("%H:%M") for s, _ in weekly] == ["19:00", "19:00", "19:00"]
    monthly = series.starts(first, timedelta(hours=3), {"freq": "monthly", "until": "2027-06-30"}, tz)
    assert [s.astimezone(tz).date().isoformat() for s, _ in monthly] == ["2027-03-19", "2027-04-16", "2027-05-21",
                                                                         "2027-06-18"]


# --- K6-K8 ---------------------------------------------------------------------------------------

def test_ticket_types_put_delete(env):
    create(env)
    p = {"locationId": LOC_A, "eventId": E1, "typeId": T2}
    body = {"name": "Early bird", "priceOre": 69500, "vatRate": 12, "perOrderMax": 2, "sortOrder": 0,
            "tier": {"untilSold": 2}}
    status, t = call(env, "PUT", L + "/{eventId}/ticket-types/{typeId}", params=p, body=body, claims=OWNER_A)
    assert status == 200 and t["tier"] == {"untilSold": 2}
    assert call(env, "PUT", L + "/{eventId}/ticket-types/{typeId}", params=p, body=body, claims=OWNER_A)[1] == t
    assert call(env, "PUT", L + "/{eventId}/ticket-types/{typeId}", params=p, claims=OWNER_A,
                body={**body, "vatRate": 7})[1]["code"] == "invalid_vat_rate"
    assert call(env, "PUT", L + "/{eventId}/ticket-types/{typeId}", params=p, body=body, claims=STAFF_A)[0] == 403
    assert call(env, "PUT", L + "/{eventId}/ticket-types/{typeId}", params={**p, "locationId": LOC_B}, body=body,
                claims=OWNER_B)[0] == 404
    status, t2 = call(env, "PUT", L + "/{eventId}/ticket-types/{typeId}", params=p, claims=OWNER_A,
                      body={**body, "name": "Tidig", "version": 1})
    assert status == 200 and t2["name"] == "Tidig" and t2["version"] == 2
    assert call(env, "PUT", L + "/{eventId}/ticket-types/{typeId}", params=p, claims=OWNER_A,
                body={**body, "version": 1})[1]["code"] == "changed_retry"
    assert call(env, "DELETE", L + "/{eventId}/ticket-types/{typeId}", params=p, claims=OWNER_A)[0] == 204
    assert call(env, "DELETE", L + "/{eventId}/ticket-types/{typeId}", params=p, claims=OWNER_A)[0] == 204


def test_delete_type_with_sales_refused(env):
    sold(env)
    p = {"locationId": LOC_A, "eventId": E1, "typeId": T1}
    assert call(env, "DELETE", L + "/{eventId}/ticket-types/{typeId}", params=p, claims=OWNER_A)[1]["code"] == \
        "has_sales"


def test_occurrence_capacity_never_below_sold(env):
    sold(env)
    p = {"locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1}
    assert call(env, "PUT", OCC, params=p, body={"capacity": 1, "version": 1}, claims=OWNER_A)[1]["code"] == \
        "invalid_capacity"
    status, occ = call(env, "PUT", OCC, params=p, body={"capacity": 10, "version": 1}, claims=OWNER_A)
    assert status == 200 and occ["capacity"] == 10 and occ["left"] == 8
    assert call(env, "PUT", OCC, params=p, body={"capacity": 10, "version": 1}, claims=OWNER_A)[1]["code"] == \
        "changed_retry"
    assert call(env, "PUT", OCC, params=p, body={"capacity": 9, "version": 2}, claims=STAFF_A)[0] == 403
    assert call(env, "PUT", OCC, params={**p, "occurrenceId": f"{E1}-20261205T1800Z"},
                body={"version": 2}, claims=OWNER_A)[0] == 404


# --- editing ticket types and occurrences after sales ----------------------------------------------

TYPE = L + "/{eventId}/ticket-types/{typeId}"
TP = {"locationId": LOC_A, "eventId": E1, "typeId": T1}
DINNER = {"name": "Middag", "priceOre": 89500, "vatRate": 12, "perOrderMax": 4, "sortOrder": 1}


def type_row(e, type_id=T1, tenant_id=TENANT_A):
    return e.table.get_item(Key=et.type_key(tenant_id, E1, type_id))["Item"]


def edit_type(e, version, claims=OWNER_A, params=TP, **over):
    return call(e, "PUT", TYPE, params=params, body={**DINNER, **over, "version": version}, claims=claims)


def consistent(row, cap=None):
    """seatsLeft is always cap - taken and taken is always sold + held."""
    assert int(row["taken"]) == int(row["sold"]) + int(row["held"])
    if cap is None:
        assert "seatsLeft" not in row
    else:
        assert int(row["seatsLeft"]) == cap - int(row["taken"])


def test_price_and_vat_edit_apply_to_later_sales_only(env):
    """Money: paid and held orders keep the price and VAT rate they were sold
    at (lines, tickets, TX rows, refunds, Bokföring); later checkouts pay the
    new price at the new rate, and the VAT change is flagged for the accountant."""
    sold(env)  # 2 x 895 kr at 12 %, paid
    assert buy(env, order_id="01ORDERHELDAAAAAAAAAAAAAAA", qty=1)[0] == 200  # held at 895 kr
    status, t = edit_type(env, 1, priceOre=99500, vatRate=25, name="Festmiddag", includes="Fem rätter")
    assert status == 200, t
    assert (t["priceOre"], t["vatRate"], t["name"], t["includes"], t["sold"], t["held"], t["version"]) == \
        (99500, 25, "Festmiddag", "Fem rätter", 2, 1, 2)
    assert t["vatReview"] == et.VAT_REVIEW
    assert t["vatChanges"] == [{"fromRate": 12, "toRate": 25, "at": "2026-11-05T12:00:00Z", "by": t["vatChanges"][0]["by"],
                                "soldBefore": 2}]
    # the held order pays and is issued at the price it was shown
    assert pay(env, order_id="01ORDERHELDAAAAAAAAAAAAAAA")["result"] == "issued"
    held = env.table.get_item(Key=et.order_key(TENANT_A, "01ORDERHELDAAAAAAAAAAAAAAA"))["Item"]
    assert held["amountOre"] == 89500 and held["lines"][0]["priceOre"] == 89500 and held["lines"][0]["vatRate"] == 12
    old = sorted(tickets(env), key=lambda x: x["ticketId"])
    assert len(old) == 3 and {(int(x["priceOre"]), int(x["vatRate"])) for x in old} == {(89500, 12)}
    # a new checkout pays the new price at the new rate
    status, body = buy(env, order_id="01ORDERNEWAAAAAAAAAAAAAAAA", qty=1)
    assert status == 200 and body["amountSek"] == 995.0
    assert pay(env, order_id="01ORDERNEWAAAAAAAAAAAAAAAA")["result"] == "issued"
    new = [x for x in tickets(env) if x["orderId"] == "01ORDERNEWAAAAAAAAAAAAAAAA"]
    assert [(int(x["priceOre"]), int(x["vatRate"])) for x in new] == [(99500, 25)]
    assert occ_row(env)["revenueOre"] == 3 * 89500 + 99500
    # refunding an old ticket returns what was paid for it, at its own rate
    status, _ = call(env, "POST", L + "/tickets/{ticketId}/refund",
                     params={"locationId": LOC_A, "ticketId": old[0]["ticketId"]},
                     body={"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "Sjuk"}, claims=OWNER_A)
    assert status == 200 and env.stripe.refunds()[-1]["params"]["amount"] == 89500
    refund_tx = env.table.get_item(Key=et.tx_key(TENANT_A, "01TXREFUNDAAAAAAAAAAAAAAAA"))["Item"]
    assert refund_tx["vatLines"] == [{"rate": 12, "netOre": -79911, "vatOre": -9589}]
    out = env.api.handler({"hook": "accounting.events", "tenantId": TENANT_A, "locationId": LOC_A,
                           "first": "2026-11-05", "last": "2026-11-05"})
    postings = out["events"][0]["postings"]
    assert sum(p["amount"] for p in postings) == 0
    by_role = {}
    for p in postings:
        by_role[p["role"]] = by_role.get(p["role"], 0) + p["amount"]
    # 3 sold and 1 refunded at 12 % (2 x 895 kr net), 1 sold at 25 %
    assert by_role == {"stripe": 2 * 89500 + 99500, "sales12": -(179000 - 19179), "vat12": -19179,
                       "sales25": -79600, "vat25": -19900}
    # a second VAT change appends to the trail; a price-only change does not
    assert edit_type(env, 2, priceOre=99500, vatRate=12)[1]["vatChanges"][-1]["toRate"] == 12
    status, t = edit_type(env, 3, priceOre=79500, vatRate=12)
    assert status == 200 and len(t["vatChanges"]) == 2 and t["vatReview"] == et.VAT_REVIEW
    consistent(type_row(env))


def test_type_capacity_never_below_sold_and_held(env):
    published(env, ticketTypes=[{**DINNER, "typeId": T1, "capacity": 3}])
    assert buy(env, qty=2)[0] == 200  # held, not paid
    status, body = edit_type(env, 1, capacity=1)
    assert (status, body["code"], body["field"]) == (400, "invalid_capacity", "capacity")
    status, body = edit_type(env, 1, capacity=3, tier={"untilSold": 1})
    assert (status, body["code"], body["field"]) == (400, "invalid_capacity", "tier")
    # down to exactly sold + held: sold out for guests
    status, t = edit_type(env, 1, capacity=2)
    assert status == 200 and t["capacity"] == 2
    consistent(type_row(env), 2)
    _, detail = call(env, "GET", "/public/locations/{locationId}/events/{eventId}", params={"locationId": LOC_A,
                                                                                           "eventId": E1})
    assert detail["ticketTypes"][0]["left"] == 0 and not detail["ticketTypes"][0]["available"]
    assert buy(env, order_id="01ORDER2AAAAAAAAAAAAAAAAAA", qty=1)[1]["code"] == "type_sold_out"
    # raised again: open for sale
    assert edit_type(env, 2, capacity=4)[0] == 200
    consistent(type_row(env), 4)
    assert buy(env, order_id="01ORDER3AAAAAAAAAAAAAAAAAA", qty=1)[0] == 200
    consistent(type_row(env), 4)
    # the cap removed and added back
    assert edit_type(env, 3)[0] == 200
    consistent(type_row(env))
    assert edit_type(env, 4, capacity=5)[0] == 200
    consistent(type_row(env), 5)
    # validation, role, foreign tenant and other location, stale version
    assert edit_type(env, 5, priceOre=-1)[1]["code"] == "invalid_price"
    assert edit_type(env, 5, vatRate=7)[1]["code"] == "invalid_vat_rate"
    assert edit_type(env, 5, tier={"availableFrom": "2026-12-01T00:00:00Z",
                                   "availableUntil": "2026-11-01T00:00:00Z"})[1]["code"] == "invalid_window"
    assert edit_type(env, 5, claims=STAFF_A)[0] == 403
    assert edit_type(env, 5, claims=OWNER_B, params={**TP, "locationId": LOC_B})[0] == 404
    assert edit_type(env, 5, claims=OWNER_B)[0] == 404  # B's token on A's location
    assert edit_type(env, 5, params={**TP, "locationId": LOC_A2})[0] == 404
    assert edit_type(env, 4, capacity=6)[1]["code"] == "changed_retry"
    assert type_row(env)["capacity"] == 5 and type_row(env)["version"] == 5


def test_type_sale_window_edit(env):
    published(env)
    status, t = edit_type(env, 1, tier={"availableFrom": "2026-11-10T08:00:00Z", "availableUntil": "2026-12-01T00:00Z"})
    assert status == 200 and t["tier"] == {"availableFrom": "2026-11-10T08:00:00Z",
                                           "availableUntil": "2026-12-01T00:00:00Z"}
    assert buy(env)[1]["code"] == "type_sold_out"  # not on sale yet
    env.clock.value = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
    assert buy(env)[0] == 200
    env.clock.value = datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)
    assert buy(env, order_id="01ORDER2AAAAAAAAAAAAAAAAAA", qty=1)[1]["code"] == "type_sold_out"  # tier ended
    assert edit_type(env, 2)[0] == 200  # window removed: on sale until the occurrence's sales close
    assert buy(env, order_id="01ORDER2AAAAAAAAAAAAAAAAAA", qty=1)[0] == 200


def test_edits_refused_once_the_event_is_over_or_cancelled(env):
    sold(env)
    op = {"locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1}
    env.clock.value = datetime(2026, 12, 4, 22, 0, tzinfo=timezone.utc)  # the end
    assert edit_type(env, 1, name="Ny")[1]["code"] == "invalid_state"
    assert call(env, "PUT", OCC, params=op, body={"capacity": 10, "version": 1}, claims=OWNER_A)[1]["code"] == \
        "invalid_state"
    env.clock.value = NOW
    assert edit_type(env, 1, name="Ny")[0] == 200  # a draft or a running event can still be edited
    assert call(env, "POST", OCC + "/cancel", params=op, body={"jobId": "01JOBAAAAAAAAAAAAAAAAAAAAA", "version": 1},
                claims=OWNER_A)[0] == 202
    assert call(env, "PUT", OCC, params=op, body={"capacity": 10, "version": 2}, claims=OWNER_A)[1]["code"] == \
        "invalid_state"
    assert edit_type(env, 2, name="Nyare")[1]["code"] == "invalid_state"  # its only occurrence is cancelled


def test_type_and_occurrence_edits_refused_with_the_feature_off(env):
    published(env)
    from shared import tenant as shared_tenant
    boto3.resource("dynamodb", region_name=REGION).Table("test-tenant").put_item(
        Item=tenant_row(TENANT_A, features=features_on(), stripe={"accountId": ACCT_A}))
    shared_tenant.reset_caches()
    assert edit_type(env, 1, priceOre=1)[0] == 403
    assert call(env, "PUT", OCC, params={"locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1},
                body={"capacity": 10, "version": 1}, claims=OWNER_A)[0] == 403
    assert type_row(env)["priceOre"] == 89500


def _sale_during_read(env, monkeypatch, sk_suffix, qty, order_id="01ORDERRACEAAAAAAAAAAAAAAA"):
    """Runs a guest's checkout right after the edit has read the row ending in
    `sk_suffix` (once): the edit then writes against fresher counters."""
    real = et.get
    armed = {"on": True}

    def get(key):
        item = real(key)
        if armed["on"] and key.get("SK", "").endswith(sk_suffix):
            armed["on"] = False
            assert buy(env, order_id=order_id, qty=qty)[0] == 200
        return item
    monkeypatch.setattr(et, "get", get)


def test_sale_during_a_type_capacity_edit_is_never_oversold(env, monkeypatch):
    published(env, ticketTypes=[{**DINNER, "typeId": T1, "capacity": 3}])
    _sale_during_read(env, monkeypatch, f"#TYPE#{T1}", 2)
    status, body = edit_type(env, 1, capacity=1)  # read taken 0, but 2 were held before the write
    assert (status, body["code"]) == (400, "invalid_capacity")
    row = type_row(env)
    assert row["capacity"] == 3 and row["version"] == 1
    consistent(row, 3)
    _sale_during_read(env, monkeypatch, f"#TYPE#{T1}", 1, order_id="01ORDERRACEBBBBBBBBBBBBBBB")
    status, t = edit_type(env, 1, capacity=3)  # the sale fits (3 taken of 3): the write goes through
    assert status == 200 and t["held"] == 3
    consistent(type_row(env), 3)


def test_sale_during_an_occurrence_capacity_edit_is_never_oversold(env, monkeypatch):
    published(env)  # capacity 4
    op = {"locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1}
    _sale_during_read(env, monkeypatch, "#OCC#20261204T1800Z", 3)
    status, body = call(env, "PUT", OCC, params=op, body={"capacity": 2, "version": 1}, claims=OWNER_A)
    assert (status, body["code"]) == (400, "invalid_capacity")
    row = occ_row(env)
    assert row["capacity"] == 4 and row["taken"] == 3 and row["seatsLeft"] == 1 and row["version"] == 1
    # down to exactly sold + held: sold out; up again: open
    status, occ = call(env, "PUT", OCC, params=op, body={"capacity": 3, "version": 1}, claims=OWNER_A)
    assert status == 200 and occ["left"] == 0 and occ["held"] == 3
    _, detail = call(env, "GET", "/public/locations/{locationId}/events/{eventId}", params={"locationId": LOC_A,
                                                                                           "eventId": E1})
    assert detail["occurrences"][0]["soldOut"]
    assert buy(env, order_id="01ORDER2AAAAAAAAAAAAAAAAAA", qty=1)[1]["code"] == "sold_out"
    status, occ = call(env, "PUT", OCC, params=op, body={"capacity": 6, "version": 2}, claims=OWNER_A)
    assert status == 200 and occ["left"] == 3 and occ_row(env)["seatsLeft"] == 3
    assert buy(env, order_id="01ORDER3AAAAAAAAAAAAAAAAAA", qty=3)[0] == 200
    row = occ_row(env)
    assert row["seatsLeft"] == 0 and row["taken"] == 6 == row["sold"] + row["held"]
    assert call(env, "PUT", OCC, params={**op, "locationId": LOC_A2}, body={"capacity": 9, "version": 3},
                claims=OWNER_A)[0] == 404
    assert call(env, "PUT", OCC, params=op, body={"capacity": 9, "version": 3}, claims=OWNER_B)[0] == 404


def test_cap_added_during_a_checkout_is_not_oversold(env):
    """The guest's page read an uncapped type; the owner caps it at 1 before
    the hold is written: the hold fails (reload) instead of taking 2."""
    published(env)
    api = env.api
    real = api._hold_items

    def cap_then_hold(*args, **kwargs):
        assert edit_type(env, 1, capacity=1)[0] == 200
        return real(*args, **kwargs)
    api._hold_items = cap_then_hold
    try:
        assert buy(env, qty=2)[1]["code"] == "changed_retry"
    finally:
        api._hold_items = real
    consistent(type_row(env), 1)
    assert buy(env, order_id="01ORDER2AAAAAAAAAAAAAAAAAA", qty=1)[0] == 200
    consistent(type_row(env), 1)
    assert buy(env, order_id="01ORDER3AAAAAAAAAAAAAAAAAA", qty=1)[1]["code"] == "type_sold_out"


@pytest.mark.parametrize("change", ["add", "remove"])
def test_cap_changed_during_a_refund_or_hold_expiry_keeps_counters(env, change):
    """The owner adds or removes a type's cap between a refund's (or a hold
    expiry's) read of the type and its write: the write's cap guard fails,
    it is rebuilt from a fresh read, and seatsLeft stays cap - taken."""
    capped = change == "remove"
    published(env, ticketTypes=[{**DINNER, "typeId": T1, **({"capacity": 4} if capped else {})}])
    assert buy(env)[0] == 200 and pay(env)["result"] == "issued"
    assert buy(env, order_id="01ORDER2AAAAAAAAAAAAAAAAAA", qty=1)[0] == 200  # held, will expire
    api = env.api
    real = api._txn
    pending = []
    calls = {"n": 0}

    def edit_then_txn(items):
        calls["n"] += 1
        if pending:
            assert edit_type(env, int(type_row(env)["version"]), **pending.pop())[0] == 200
        return real(items)
    api._txn = edit_then_txn
    try:
        pending.append({} if capped else {"capacity": 6})
        ticket_id = sorted(tickets(env), key=lambda x: x["ticketId"])[0]["ticketId"]
        status, _ = call(env, "POST", L + "/tickets/{ticketId}/refund", params={"locationId": LOC_A,
                                                                               "ticketId": ticket_id},
                         body={"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "Sjuk"}, claims=OWNER_A)
        assert status == 200 and calls["n"] == 2  # the first write failed its cap guard, the retry went through
        row = type_row(env)
        assert row["sold"] == 1 and row["held"] == 1
        consistent(row, None if capped else 6)
        pending.append({"capacity": 7} if capped else {})
        env.clock.value = NOW + timedelta(minutes=20)
        assert api.run_sweep()["expired"] == 1 and calls["n"] == 4
    finally:
        api._txn = real
    row = type_row(env)
    assert row["sold"] == 1 and row["held"] == 0
    consistent(row, 7 if capped else None)
    assert len(env.stripe.refunds()) == 1
    assert occ_row(env)["seatsLeft"] == 3 and occ_row(env)["taken"] == 1


# --- K15-K17 public and checkout --------------------------------------------------------------------

def test_public_list_and_detail_only_published(env):
    create(env)
    assert call(env, "GET", "/public/locations/{locationId}/events/{eventId}",
                params={"locationId": LOC_A, "eventId": E1})[0] == 404  # draft
    assert call(env, "GET", "/public/locations/{locationId}/events", params={"locationId": LOC_A})[1] == \
        {"events": []}
    publish(env)
    _, listing = call(env, "GET", "/public/locations/{locationId}/events", params={"locationId": LOC_A})
    assert listing["events"] == [{"eventId": E1, "title": "Vinmiddag", "nextOccurrence": START,
                                  "fromPriceSek": 895.0, "soldOut": False}]
    status, detail = call(env, "GET", "/public/locations/{locationId}/events/{eventId}",
                          params={"locationId": LOC_A, "eventId": E1})
    assert status == 200 and detail["occurrences"][0]["left"] == 4 and detail["ticketTypes"][0]["available"]
    assert call(env, "GET", "/public/locations/{locationId}/events/{eventId}",
                params={"locationId": LOC_B, "eventId": E1})[0] == 404


def test_checkout_holds_and_retries_return_the_same_intent(env):
    published(env)
    status, body = buy(env)
    assert status == 200 and body["clientSecret"].endswith("_secret") and body["amountSek"] == 1790.0
    assert body["stripeAccount"] == ACCT_A
    intent = next(c for c in env.stripe.calls if c["path"] == "/v1/payment_intents")
    assert intent["account"] == ACCT_A and intent["key"] == "ticket-01ORDERAAAAAAAAAAAAAAAAAAA"
    assert intent["params"]["metadata"]["kind"] == "ticket"
    assert occ_row(env)["held"] == 2 and occ_row(env)["seatsLeft"] == 2
    again = buy(env)
    assert again[1]["clientSecret"] == body["clientSecret"] and occ_row(env)["held"] == 2
    assert len([c for c in env.stripe.calls if c["path"] == "/v1/payment_intents"]) == 1


def test_checkout_validation(env):
    published(env)
    assert buy(env, qty=5)[1]["code"] == "too_many_tickets"
    assert buy(env, buyerEmail="nope")[1]["code"] == "invalid_email"
    assert buy(env, tableIds=["t1"])[1]["code"] == "table_pick_not_available"
    assert buy(env, type_id=T2)[0] == 404
    assert buy(env, loc=LOC_B)[0] == 404  # A's occurrence at B's location
    env.clock.value = datetime(2026, 12, 4, 18, 30, tzinfo=timezone.utc)
    assert buy(env)[1]["code"] == "sales_closed"


def test_checkout_turnstile_required(env, monkeypatch):
    published(env)
    monkeypatch.setenv("TURNSTILE_REQUIRED", "true")
    from shared import turnstile

    def verify(token, ip=None, action=None):
        raise ValueError("bad")
    monkeypatch.setattr(turnstile, "verify", verify)
    assert buy(env)[1]["code"] == "turnstile_failed"
    assert occ_row(env)["held"] == 0


def test_last_seats_never_oversold(env):
    published(env)
    assert buy(env, order_id="01ORDER1AAAAAAAAAAAAAAAAAA", qty=3)[0] == 200
    status, body = buy(env, order_id="01ORDER2AAAAAAAAAAAAAAAAAA", qty=2)
    assert (status, body["code"]) == (409, "sold_out")
    assert buy(env, order_id="01ORDER3AAAAAAAAAAAAAAAAAA", qty=1)[0] == 200
    assert buy(env, order_id="01ORDER4AAAAAAAAAAAAAAAAAA", qty=1)[1]["code"] == "sold_out"
    row = occ_row(env)
    assert row["held"] == 4 and row["seatsLeft"] == 0 and row["taken"] == 4


def test_owner_editing_a_type_does_not_fail_the_checkout(env):
    """The hold is conditional on seatsLeft (and the price shown), not on the
    type's version: a rename between the page load and the checkout is fine."""
    published(env, ticketTypes=[{"typeId": T1, "name": "Middag", "priceOre": 50000, "vatRate": 12, "capacity": 3}])
    api = env.api
    real = api._hold_items
    p = {"locationId": LOC_A, "eventId": E1, "typeId": T1}

    def edit_then_hold(body):
        def hook(*args, **kwargs):
            status, _ = call(env, "PUT", L + "/{eventId}/ticket-types/{typeId}", params=p, claims=OWNER_A, body=body)
            assert status == 200
            return real(*args, **kwargs)
        return hook

    # the price changed under the guest: not sold out, reload (the hold is conditional on the price shown)
    api._hold_items = edit_then_hold({"name": "Middag", "priceOre": 60000, "vatRate": 12, "capacity": 3, "version": 1})
    try:
        assert buy(env, order_id="01ORDER1AAAAAAAAAAAAAAAAAA", qty=1)[1]["code"] == "changed_retry"
    finally:
        api._hold_items = real
    assert env.table.get_item(Key=et.type_key(TENANT_A, E1, T1))["Item"]["held"] == 0
    # a rename (version bump) between the page load and the checkout: the hold goes through
    api._hold_items = edit_then_hold({"name": "Festmiddag", "priceOre": 60000, "vatRate": 12, "capacity": 3,
                                      "version": 2})
    try:
        assert buy(env, qty=2)[0] == 200
    finally:
        api._hold_items = real
    row = env.table.get_item(Key=et.type_key(TENANT_A, E1, T1))["Item"]
    assert row["name"] == "Festmiddag" and row["version"] == 3 and row["held"] == 2 and row["seatsLeft"] == 1
    order = env.table.get_item(Key=et.order_key(TENANT_A, "01ORDERAAAAAAAAAAAAAAAAAAA"))["Item"]
    assert order["amountOre"] == 120000
    assert buy(env, order_id="01ORDER3AAAAAAAAAAAAAAAAAA", qty=1)[0] == 200
    assert buy(env, order_id="01ORDER4AAAAAAAAAAAAAAAAAA", qty=1)[1]["code"] == "type_sold_out"


def test_type_capacity_and_early_bird(env):
    published(env, ticketTypes=[{"typeId": T1, "name": "Middag", "priceOre": 50000, "vatRate": 12, "capacity": 1},
                                {"typeId": T2, "name": "Early", "priceOre": 40000, "vatRate": 12,
                                 "tier": {"untilSold": 1}}])
    assert buy(env, order_id="01ORDER1AAAAAAAAAAAAAAAAAA", qty=1)[0] == 200
    assert buy(env, order_id="01ORDER2AAAAAAAAAAAAAAAAAA", qty=1)[1]["code"] == "type_sold_out"
    assert buy(env, order_id="01ORDER3AAAAAAAAAAAAAAAAAA", qty=1, type_id=T2)[0] == 200
    assert buy(env, order_id="01ORDER4AAAAAAAAAAAAAAAAAA", qty=1, type_id=T2)[1]["code"] == "type_sold_out"


def test_stripe_down_releases_the_hold(env):
    published(env)
    env.stripe.fail = True
    assert buy(env)[1]["code"] == "stripe_unavailable"
    assert occ_row(env)["held"] == 0 and occ_row(env)["seatsLeft"] == 4


# --- issue, webhook ------------------------------------------------------------------------------

def test_webhook_issues_once_and_emails(env):
    all_tickets = sold(env)
    assert len(all_tickets) == 2 and {t["status"] for t in all_tickets} == {"valid"}
    assert all(not any(k.lower().startswith("code") for k in t) for t in all_tickets)  # no QR in the table
    assert [r["SK"][:5] for r in env.table.scan()["Items"] if r["SK"].startswith("CODE#")] == []
    row = occ_row(env)
    assert row["sold"] == 2 and row["held"] == 0 and row["revenueOre"] == 179000
    assert pay(env)["result"] == "already_issued" and len(tickets(env)) == 2
    assert env.mail.kinds() == ["events-tickets.tickets"]
    text = env.mail.calls[0]["notice"]["text"]
    assert "https://dev-site.example/biljett?restaurang=resta#" in text
    assert ("Ticket.Sold", {"tenantId": TENANT_A, "locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1,
                            "orderId": "01ORDERAAAAAAAAAAAAAAAAAAA", "qty": 2, "amountOre": 179000}) in env.published


def test_webhook_failed_email_is_sent_on_replay(env):
    published(env)
    buy(env)
    env.mail.status = 500
    with pytest.raises(RuntimeError):
        pay(env)
    env.mail.status = 202
    env.clock.value = NOW + timedelta(minutes=5)
    assert pay(env)["result"] == "already_issued"
    assert env.mail.kinds()[-1] == "events-tickets.tickets" and len(tickets(env)) == 2


def test_foreign_account_and_tenant_ignored(env):
    published(env)
    buy(env)
    assert pay(env, account=ACCT_B)["result"] == "ignored:account_mismatch"
    order = env.table.get_item(Key=et.order_key(TENANT_A, "01ORDERAAAAAAAAAAAAAAAAAAA"))["Item"]
    foreign = {"hook": "stripe.event", "tenantId": TENANT_B, "type": "payment_intent.succeeded", "account": ACCT_B,
               "object": {"id": order["paymentIntentId"], "amount": int(order["amountOre"]),
                          "metadata": {"kind": "ticket", "tenantId": TENANT_B, "orderId": order["orderId"]}}}
    assert env.api.handler(foreign)["result"] == "ignored:unknown_order"
    assert tickets(env) == []


def test_free_tickets_issue_without_stripe(env):
    published(env, ticketTypes=[{"typeId": T1, "name": "Quiz", "priceOre": 0, "vatRate": 25}])
    status, body = buy(env)
    assert status == 200 and "clientSecret" not in body and body["amountSek"] == 0
    assert len(tickets(env)) == 2 and env.stripe.calls == []
    assert env.mail.kinds() == ["events-tickets.tickets"]


def test_hold_expiry_releases_and_late_payment(env):
    published(env)
    buy(env, qty=4)
    env.clock.value = NOW + timedelta(minutes=16)
    assert env.api.handler({"job": "sweep"})["expired"] == 1
    assert occ_row(env)["seatsLeft"] == 4 and occ_row(env)["held"] == 0
    assert env.api.handler({"job": "sweep"})["expired"] == 0  # idempotent
    assert any(c["path"].endswith("/cancel") for c in env.stripe.calls)
    assert buy(env, order_id="01ORDERLATERAAAAAAAAAAAAAA", qty=4)[0] == 200
    # the first guest's payment still arrives: no seats left, so the whole payment is refunded
    assert pay(env)["result"] == "refunded:sold_out"
    assert env.stripe.refunds()[-1]["key"] == "ticket-late-01ORDERAAAAAAAAAAAAAAAAAAA"


def test_late_payment_with_free_seats_is_issued(env):
    published(env)
    buy(env)
    env.clock.value = NOW + timedelta(minutes=16)
    env.api.handler({"job": "sweep"})
    assert pay(env)["result"] == "issued"
    assert occ_row(env)["sold"] == 2 and occ_row(env)["seatsLeft"] == 2



def test_late_payment_while_the_owner_caps_the_type_is_issued_once(env):
    """A payment arriving after its hold expired takes the seats again; the
    owner capping the type between that read and the write fails the cap
    guard once, the issue is rebuilt and the counters stay cap - taken."""
    published(env)
    buy(env)
    env.clock.value = NOW + timedelta(minutes=16)
    env.api.handler({"job": "sweep"})
    api = env.api
    real = api._txn
    pending = [{"capacity": 3}]

    def edit_then_txn(items):
        if pending:
            assert edit_type(env, 1, **pending.pop())[0] == 200
        return real(items)
    api._txn = edit_then_txn
    try:
        assert pay(env)["result"] == "issued"
    finally:
        api._txn = real
    consistent(type_row(env), 3)
    assert type_row(env)["sold"] == 2 and len(tickets(env)) == 2 and not env.stripe.refunds()
    assert pay(env)["result"] == "already_issued"

# --- K18-K22 ticket page and transfers --------------------------------------------------------------

def test_ticket_page_and_pass(env):
    sold(env)
    status, page = call(env, "GET", "/public/tickets/{token}", params={"token": token_of(env)})
    assert status == 200 and len(page["tickets"]) == 2 and page["transferAllowed"] and not page["resaleAllowed"]
    assert page["wallet"] == {"apple": False, "google": False}
    first = page["tickets"][0]
    assert first["qr"] == qr.payload(TENANT_A, OCC1, first["ticketId"])
    assert call(env, "GET", "/public/tickets/{token}", params={"token": token_of(env) + "x"})[0] == 404
    forged = qr.token("o", TENANT_B, "01ORDERAAAAAAAAAAAAAAAAAAA")
    assert call(env, "GET", "/public/tickets/{token}", params={"token": forged})[0] == 404
    assert call(env, "GET", "/public/tickets/{token}/pass", params={"token": token_of(env)},
                query={"ticketId": first["ticketId"], "kind": "apple"})[0] == 404


def test_gift_transfer_accept_and_cancel(env):
    sold(env)
    tok = token_of(env)
    ticket_id = sorted(t["ticketId"] for t in tickets(env))[0]
    body = {"transferId": "01TRANSFERAAAAAAAAAAAAAAAA", "ticketId": ticket_id, "kind": "gift",
            "toEmail": "vän@example.se"}
    assert call(env, "POST", "/public/tickets/{token}/transfers", params={"token": tok},
                body={**body, "kind": "resale"})[1]["code"] == "transfer_closed"
    assert call(env, "POST", "/public/tickets/{token}/transfers", params={"token": tok},
                body={**body, "toEmail": "x"})[1]["code"] == "invalid_email"
    status, transfer = call(env, "POST", "/public/tickets/{token}/transfers", params={"token": tok}, body=body)
    assert status == 201 and transfer["status"] == "pending"
    assert call(env, "POST", "/public/tickets/{token}/transfers", params={"token": tok}, body=body)[0] == 201
    assert env.mail.kinds()[-1] == "events-tickets.transfer"
    accept = qr.token("x", TENANT_A, "01TRANSFERAAAAAAAAAAAAAAAA")
    status, out = call(env, "POST", "/public/ticket-transfers/{transferToken}/accept",
                       params={"transferToken": accept}, body={"name": "Vän Vänsson"})
    assert status == 200 and qr.read_token(out["ticketToken"])[0] == "t"
    again = call(env, "POST", "/public/ticket-transfers/{transferToken}/accept", params={"transferToken": accept},
                 body={"name": "Vän Vänsson"})
    assert again == (200, out)
    old = env.table.get_item(Key=et.ticket_key(TENANT_A, ticket_id))["Item"]
    assert old["status"] == "transferred"
    _, page = call(env, "GET", "/public/tickets/{token}", params={"token": out["ticketToken"]})
    assert page["tickets"][0]["holderName"] == "Vän Vänsson" and page["tickets"][0]["status"] == "valid"
    assert call(env, "DELETE", "/public/tickets/{token}/transfers/{transferId}",
                params={"token": tok, "transferId": "01TRANSFERAAAAAAAAAAAAAAAA"})[1]["code"] == "invalid_state"
    assert ("Ticket.Transferred" in [p[0] for p in env.published])


def test_gift_transfers_per_ticket_are_capped(env, monkeypatch):
    """One ticket can only cause so many gift emails a day (request guard)."""
    guard_table(monkeypatch)
    sold(env)
    tok = token_of(env)
    ticket_id = sorted(t["ticketId"] for t in tickets(env))[0]
    for i in range(et.TRANSFERS_PER_TICKET_DAY):
        transfer_id = f"01TRANSFER{i}AAAAAAAAAAAAAAA"
        status, _ = call(env, "POST", "/public/tickets/{token}/transfers", params={"token": tok},
                         body={"transferId": transfer_id, "ticketId": ticket_id, "kind": "gift",
                               "toEmail": f"v{i}@example.se"})
        assert status == 201
        assert call(env, "DELETE", "/public/tickets/{token}/transfers/{transferId}",
                    params={"token": tok, "transferId": transfer_id})[0] == 204
    event = {"routeKey": "POST /public/tickets/{token}/transfers", "pathParameters": {"token": tok}, "headers": {},
             "requestContext": {"http": {"method": "POST", "sourceIp": "203.0.113.5"}},
             "body": json.dumps({"transferId": "01TRANSFERXAAAAAAAAAAAAAAA", "ticketId": ticket_id, "kind": "gift",
                                 "toEmail": "x@example.se"})}
    response = env.api.handler(event, None)
    assert response["statusCode"] == 429 and int(response["headers"]["Retry-After"]) > 0
    assert json.loads(response["body"])["code"] == "too_many_requests"
    assert env.mail.kinds().count("events-tickets.transfer") == et.TRANSFERS_PER_TICKET_DAY
    assert env.table.get_item(Key=et.transfer_key(TENANT_A, "01TRANSFERXAAAAAAAAAAAAAAA")).get("Item") is None
    assert len(et.transfers_of_ticket(TENANT_A, ticket_id)) == et.TRANSFERS_PER_TICKET_DAY


def test_transfer_cancel_and_closing(env):
    sold(env)
    tok = token_of(env)
    ticket_id = sorted(t["ticketId"] for t in tickets(env))[0]
    body = {"transferId": "01TRANSFERBBBBBBBBBBBBBBBB", "ticketId": ticket_id, "kind": "gift",
            "toEmail": "a@example.se"}
    call(env, "POST", "/public/tickets/{token}/transfers", params={"token": tok}, body=body)
    p = {"token": tok, "transferId": "01TRANSFERBBBBBBBBBBBBBBBB"}
    assert call(env, "DELETE", "/public/tickets/{token}/transfers/{transferId}", params=p)[0] == 204
    assert call(env, "DELETE", "/public/tickets/{token}/transfers/{transferId}", params=p)[0] == 204
    foreign = qr.token("o", TENANT_A, "01ORDERZZZZZZZZZZZZZZZZZZZ")
    assert call(env, "DELETE", "/public/tickets/{token}/transfers/{transferId}",
                params={**p, "token": foreign})[0] == 404
    env.clock.value = datetime(2026, 12, 4, 16, 30, tzinfo=timezone.utc)  # 1.5 h before the start
    assert call(env, "POST", "/public/tickets/{token}/transfers", params={"token": tok},
                body={**body, "transferId": "01TRANSFERCCCCCCCCCCCCCCCC"})[1]["code"] == "transfer_closed"


# --- K10-K11 guest list and check-ins -------------------------------------------------------------

def test_guest_list_and_offline_check_ins(env):
    sold(env)
    p = {"locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1}
    status, glist = call(env, "GET", OCC + "/guest-list", params=p, claims=STAFF_A)
    assert status == 200 and glist["counters"] == {"sold": 2, "checkedIn": 0, "capacity": 4}
    key = glist["scanKey"]
    t1, t2 = [t["ticketId"] for t in glist["tickets"]]
    env.clock.value = datetime(2026, 12, 4, 18, 10, tzinfo=timezone.utc)  # the door, syncing after a dead zone
    batch = [{"ticketId": t1, "sig": qr.sig(key, t1), "at": "2026-12-04T17:55:00Z", "clientId": "c1"},
             {"ticketId": t2, "sig": "0" * 16, "at": "2026-12-04T17:56:00Z", "clientId": "c2"}]
    status, out = call(env, "POST", OCC + "/check-ins", params=p, body={"checkIns": batch}, claims=STAFF_A)
    assert status == 200 and [r["status"] for r in out["results"]] == ["ok", "invalid"]
    assert out["results"][0]["by"] == "Staff"
    # a second device scanned the same ticket offline
    status, out = call(env, "POST", OCC + "/check-ins", params=p, claims=OWNER_A,
                       body={"checkIns": [{"ticketId": t1, "sig": qr.sig(key, t1), "at": "2026-12-04T17:57:00Z",
                                           "clientId": "c9"}]})
    assert out["results"][0] == {"clientId": "c9", "status": "already", "at": "2026-12-04T17:55:00Z", "by": "Staff"}
    assert occ_row(env)["checkedIn"] == 1
    too_many = [{"ticketId": t1, "sig": "x", "at": START, "clientId": f"c{i}"} for i in range(201)]
    assert call(env, "POST", OCC + "/check-ins", params=p, body={"checkIns": too_many}, claims=STAFF_A)[1]["code"] == \
        "too_many"
    assert call(env, "GET", OCC + "/guest-list", params={**p, "locationId": LOC_A2}, claims=STAFF_A)[0] == 404


def test_scan_key_is_tenant_and_occurrence_bound(env):
    assert qr.scan_key(TENANT_A, OCC1) != qr.scan_key(TENANT_B, OCC1)
    assert qr.scan_key(TENANT_A, OCC1) != qr.scan_key(TENANT_A, f"{E1}-20261211T1800Z")
    sold(env)
    t1 = sorted(t["ticketId"] for t in tickets(env))[0]
    foreign_sig = qr.sig(qr.scan_key(TENANT_B, OCC1), t1)
    assert not qr.verify(TENANT_A, OCC1, t1, foreign_sig)
    assert qr.verify(TENANT_A, OCC1, t1, qr.sig(qr.scan_key(TENANT_A, OCC1), t1))


# --- K12-K14 orders, refunds, cancel ------------------------------------------------------------------

def test_orders_and_ticket_refund(env):
    all_tickets = sold(env)
    p = {"locationId": LOC_A, "eventId": E1}
    status, listing = call(env, "GET", L + "/{eventId}/orders", params=p, claims=OWNER_A)
    assert status == 200 and listing["orders"][0]["status"] == "paid" and len(listing["orders"][0]["tickets"]) == 2
    assert call(env, "GET", L + "/{eventId}/orders", params=p, claims=STAFF_A)[0] == 403
    assert call(env, "GET", L + "/{eventId}/orders", params=p, query={"cursor": "!!"}, claims=OWNER_A)[0] == 400
    ticket_id = all_tickets[0]["ticketId"]
    rp = {"locationId": LOC_A, "ticketId": ticket_id}
    body = {"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "Sjuk"}
    assert call(env, "POST", L + "/tickets/{ticketId}/refund", params=rp, body=body, claims=STAFF_A)[0] == 403
    assert call(env, "POST", L + "/tickets/{ticketId}/refund", params={**rp, "locationId": LOC_B}, body=body,
                claims=OWNER_B)[0] == 404
    status, t = call(env, "POST", L + "/tickets/{ticketId}/refund", params=rp, body=body, claims=OWNER_A)
    assert status == 200 and t["status"] == "refunded"
    assert call(env, "POST", L + "/tickets/{ticketId}/refund", params=rp, body=body, claims=OWNER_A)[0] == 200
    refunds = env.stripe.refunds()
    assert len(refunds) == 1 and refunds[0]["key"] == f"ticket-refund-{ticket_id}"
    assert refunds[0]["params"]["amount"] == 89500 and refunds[0]["account"] == ACCT_A
    assert call(env, "POST", L + "/tickets/{ticketId}/refund", params=rp, claims=OWNER_A,
                body={**body, "txId": "01TXREFUNDBBBBBBBBBBBBBBBB"})[1]["code"] == "invalid_state"
    assert occ_row(env)["sold"] == 1 and occ_row(env)["seatsLeft"] == 3
    order = env.table.get_item(Key=et.order_key(TENANT_A, "01ORDERAAAAAAAAAAAAAAAAAAA"))["Item"]
    assert order["status"] == "partly_refunded" and order["refundedOre"] == 89500


def test_refund_stripe_down_then_retry_finishes(env):
    ticket_id = sold(env)[0]["ticketId"]
    rp = {"locationId": LOC_A, "ticketId": ticket_id}
    body = {"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "Sjuk"}
    env.stripe.fail = True
    assert call(env, "POST", L + "/tickets/{ticketId}/refund", params=rp, body=body, claims=OWNER_A)[1]["code"] == \
        "stripe_unavailable"
    env.stripe.fail = False
    assert call(env, "POST", L + "/tickets/{ticketId}/refund", params=rp, body=body, claims=OWNER_A)[0] == 200
    tx = env.table.get_item(Key=et.tx_key(TENANT_A, "01TXREFUNDAAAAAAAAAAAAAAAA"))["Item"]
    assert tx["stripeRefundId"].startswith("re_")
    assert len([r for r in env.stripe.refunds() if r["key"] == f"ticket-refund-{ticket_id}"]) == 2  # same key


def test_refund_retry_with_another_tx_id_then_sweep_refunds_once(env):
    """Stripe down on the owner's refund, the admin retries with a fresh txId
    (the old client did that): still one refund TX, and the sweep sends the
    one Stripe refund the row still owes (REFUNDS index, removed on success)."""
    ticket_id = sold(env)[0]["ticketId"]
    rp = {"locationId": LOC_A, "ticketId": ticket_id}
    env.stripe.fail = True
    assert call(env, "POST", L + "/tickets/{ticketId}/refund", params=rp, claims=OWNER_A,
                body={"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "Sjuk"})[1]["code"] == "stripe_unavailable"
    retry = call(env, "POST", L + "/tickets/{ticketId}/refund", params=rp, claims=OWNER_A,
                 body={"txId": "01TXREFUNDBBBBBBBBBBBBBBBB", "reason": "Sjuk"})
    assert retry[0] == 409 and retry[1]["code"] == "invalid_state"  # already refunded in our books, not twice
    env.stripe.fail = False
    assert et.pending_refunds() and et.pending_refunds()[0]["txId"] == "01TXREFUNDAAAAAAAAAAAAAAAA"
    before = len(env.stripe.refunds())
    assert env.api.handler({"job": "sweep"})["refunds"] == 1
    assert len(env.stripe.refunds()) == before + 1 and len({r["key"] for r in env.stripe.refunds()}) == 1
    rows = env.table.query(KeyConditionExpression="PK = :p AND begins_with(SK, :s)",
                           ExpressionAttributeValues={":p": f"TENANT#{TENANT_A}", ":s": "TX#"})["Items"]
    refund_rows = [r for r in rows if r["kind"] == "refund"]
    assert len(refund_rows) == 1 and refund_rows[0]["stripeRefundId"].startswith("re_")
    assert "GSI1PK" not in refund_rows[0] and et.pending_refunds() == []
    assert env.api.handler({"job": "sweep"})["refunds"] == 0
    ticket = env.table.get_item(Key=et.ticket_key(TENANT_A, ticket_id))["Item"]
    assert ticket["refundTxId"] == "01TXREFUNDAAAAAAAAAAAAAAAA"


def test_refund_older_than_a_day_is_looked_up_in_stripe_first(env):
    ticket_id = sold(env)[0]["ticketId"]
    rp = {"locationId": LOC_A, "ticketId": ticket_id}
    env.stripe.fail = True
    call(env, "POST", L + "/tickets/{ticketId}/refund", params=rp, claims=OWNER_A,
         body={"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "Sjuk"})
    env.stripe.fail = False
    order = env.table.get_item(Key=et.order_key(TENANT_A, "01ORDERAAAAAAAAAAAAAAAAAAA"))["Item"]
    # Stripe did take the refund (the response never reached us); the key may be forgotten after 24 h.
    env.stripe.known_refunds = [{"id": "re_found", "payment_intent": order["paymentIntentId"], "amount": 89500,
                                 "status": "succeeded",
                                 "metadata": {"kind": "ticket", "tenantId": TENANT_A, "ticketId": ticket_id}}]
    env.clock.value = NOW + timedelta(hours=25)
    posted = len(env.stripe.refunds())
    assert env.api.handler({"job": "sweep"})["refunds"] == 1
    assert len(env.stripe.refunds()) == posted  # no second refund
    tx = env.table.get_item(Key=et.tx_key(TENANT_A, "01TXREFUNDAAAAAAAAAAAAAAAA"))["Item"]
    assert tx["stripeRefundId"] == "re_found"
    lookups = [c for c in env.stripe.calls if c["method"] == "GET" and c["path"] == "/v1/refunds"]
    assert lookups and lookups[-1]["account"] == ACCT_A and lookups[-1]["params"]["payment_intent"] == \
        order["paymentIntentId"]


@pytest.mark.parametrize("stale", ["failed", "canceled"])
def test_refund_older_than_a_day_ignores_failed_or_canceled_stripe_refunds(env, stale):
    ticket_id = sold(env)[0]["ticketId"]
    env.stripe.fail = True
    call(env, "POST", L + "/tickets/{ticketId}/refund", params={"locationId": LOC_A, "ticketId": ticket_id},
         claims=OWNER_A, body={"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "Sjuk"})
    env.stripe.fail = False
    order = env.table.get_item(Key=et.order_key(TENANT_A, "01ORDERAAAAAAAAAAAAAAAAAAA"))["Item"]
    env.stripe.known_refunds = [{"id": "re_stale", "payment_intent": order["paymentIntentId"], "amount": 89500,
                                 "status": stale,
                                 "metadata": {"kind": "ticket", "tenantId": TENANT_A, "ticketId": ticket_id}}]
    env.clock.value = NOW + timedelta(hours=25)
    posted = len(env.stripe.refunds())
    assert env.api.handler({"job": "sweep"})["refunds"] == 1
    assert len(env.stripe.refunds()) == posted + 1  # a new refund is sent; the stale one moved no money
    tx = env.table.get_item(Key=et.tx_key(TENANT_A, "01TXREFUNDAAAAAAAAAAAAAAAA"))["Item"]
    assert tx["stripeRefundId"] not in ("re_stale", "unknown")


def test_tickets_of_order_uses_get_item_only(env, monkeypatch):
    """Key lookups (with the gift chain) never use BatchGetItem (not in the IAM policy)."""
    sold(env)
    tok = token_of(env)
    ticket_id = sorted(t["ticketId"] for t in tickets(env))[0]
    body = {"transferId": "01TRANSFERAAAAAAAAAAAAAAAA", "ticketId": ticket_id, "kind": "gift",
            "toEmail": "vän@example.se"}
    assert call(env, "POST", "/public/tickets/{token}/transfers", params={"token": tok}, body=body)[0] == 201
    accept = qr.token("x", TENANT_A, "01TRANSFERAAAAAAAAAAAAAAAA")
    assert call(env, "POST", "/public/ticket-transfers/{transferToken}/accept", params={"transferToken": accept},
                body={"name": "Vän Vänsson"})[0] == 200
    from shared import dynamo
    batch_calls = []
    monkeypatch.setattr(dynamo.client(), "batch_get_item", lambda **kw: batch_calls.append(kw) or {},
                        raising=False)
    order = env.table.get_item(Key=et.order_key(TENANT_A, "01ORDERAAAAAAAAAAAAAAAAAAA"))["Item"]
    rows = et.tickets_of_order(TENANT_A, order)
    assert len(rows) == int(order["qty"]) + 1  # both tickets plus the gifted one
    assert any(r.get("transferredTo") for r in rows)
    assert len(et.tickets_of_order(TENANT_A, order, gifted=False)) == int(order["qty"])
    assert et.batch_get([et.ticket_key(TENANT_A, "01NOSUCHTICKETAAAAAAAAAAAA")]) == []
    assert batch_calls == []


def test_refund_older_than_a_day_unknown_to_stripe_is_sent(env):
    ticket_id = sold(env)[0]["ticketId"]
    env.stripe.fail = True
    call(env, "POST", L + "/tickets/{ticketId}/refund", params={"locationId": LOC_A, "ticketId": ticket_id},
         claims=OWNER_A, body={"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "Sjuk"})
    env.stripe.fail = False
    env.clock.value = NOW + timedelta(hours=25)
    posted = len(env.stripe.refunds())
    assert env.api.handler({"job": "sweep"})["refunds"] == 1
    assert len(env.stripe.refunds()) == posted + 1
    assert any(c["method"] == "GET" and c["path"] == "/v1/refunds" for c in env.stripe.calls)


def test_cancel_refunds_a_few_inline_and_the_sweep_the_rest(env):
    """K9 answers 202 after INLINE_REFUNDS tickets; counts are written per
    ticket; the sweep stops when the Lambda's time is short and continues."""
    published(env, occurrences=[{"startAt": START, "endAt": "2026-12-04T22:00:00Z", "capacity": 20}])
    for i in range(3):
        order_id = f"01ORDER{i}AAAAAAAAAAAAAAAAAA"
        assert buy(env, order_id=order_id, qty=4)[0] == 200 and pay(env, order_id=order_id)["result"] == "issued"
    p = {"locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1}
    status, out = call(env, "POST", OCC + "/cancel", params=p, claims=OWNER_A,
                       body={"jobId": "01JOBAAAAAAAAAAAAAAAAAAAAA", "version": 1})
    assert (status, out["toRefund"]) == (202, 12)
    jp = {"locationId": LOC_A, "jobId": "01JOBAAAAAAAAAAAAAAAAAAAAA"}
    _, job = call(env, "GET", L + "/jobs/{jobId}", params=jp, claims=OWNER_A)
    assert job["done"] is False and job["counts"]["refunded"] == env.api.INLINE_REFUNDS == 10
    assert len(env.stripe.refunds()) == 10 and env.mail.kinds().count("events-tickets.cancelled") == 0
    # the sweep with (almost) no time left does nothing and says so
    out = env.api.handler({"job": "sweep"}, FakeContext(env.api.JOB_MARGIN_MS - 1))
    assert out["stopped"] is True and out["jobs"] == 0
    assert call(env, "GET", L + "/jobs/{jobId}", params=jp, claims=OWNER_A)[1]["counts"]["refunded"] == 10
    out = env.api.handler({"job": "sweep"}, FakeContext(120_000))
    assert out["stopped"] is False and out["jobs"] == 1
    _, job = call(env, "GET", L + "/jobs/{jobId}", params=jp, claims=OWNER_A)
    assert job["done"] is True and job["counts"] == {"refunded": 12, "failed": 0, "notified": 3}
    assert len({r["key"] for r in env.stripe.refunds()}) == 12 and {t["status"] for t in tickets(env)} == {"refunded"}


def test_cancel_occurrence_mass_refund_resumable(env):
    sold(env)
    buy(env, order_id="01ORDERBBBBBBBBBBBBBBBBBBB", qty=1)
    pay(env, order_id="01ORDERBBBBBBBBBBBBBBBBBBB")
    p = {"locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1}
    assert call(env, "POST", OCC + "/cancel", params=p, body={"jobId": "01JOBAAAAAAAAAAAAAAAAAAAAA", "version": 1},
                claims=STAFF_A)[0] == 403
    env.stripe.fail = True  # Stripe is down during the first run
    status, out = call(env, "POST", OCC + "/cancel", params=p, claims=OWNER_A,
                       body={"jobId": "01JOBAAAAAAAAAAAAAAAAAAAAA", "version": 1, "message": "Kocken är sjuk."})
    assert status == 202 and out == {"jobId": "01JOBAAAAAAAAAAAAAAAAAAAAA", "toRefund": 3}
    assert call(env, "POST", OCC + "/cancel", params=p, claims=OWNER_A,
                body={"jobId": "01JOBAAAAAAAAAAAAAAAAAAAAA", "version": 1})[1]["toRefund"] == 3
    jp = {"locationId": LOC_A, "jobId": "01JOBAAAAAAAAAAAAAAAAAAAAA"}
    assert call(env, "GET", L + "/jobs/{jobId}", params=jp, claims=OWNER_A)[1]["done"] is False
    _, job = call(env, "GET", L + "/jobs/{jobId}", params=jp, claims=OWNER_A)
    assert job["counts"] == {"refunded": 3, "failed": 3, "notified": 0}  # in our books; Stripe still owes 3
    env.stripe.fail = False
    env.api.handler({"job": "sweep"})
    _, job = call(env, "GET", L + "/jobs/{jobId}", params=jp, claims=OWNER_A)
    assert job["done"] is True and job["counts"] == {"refunded": 3, "failed": 0, "notified": 2}
    keys = [r["key"] for r in env.stripe.refunds()]
    assert len(set(keys)) == 3  # every ticket refunded once (retries reuse the same key)
    assert len([k for k in keys if k]) == 6  # 3 failed calls, 3 that went through
    assert et.pending_refunds() == []
    assert {t["status"] for t in tickets(env)} == {"refunded"}
    assert env.mail.kinds().count("events-tickets.cancelled") == 2
    env.api.handler({"job": "sweep"})
    assert env.mail.kinds().count("events-tickets.cancelled") == 2
    assert call(env, "POST", OCC + "/cancel", params=p, claims=OWNER_A,
                body={"jobId": "01JOBBBBBBBBBBBBBBBBBBBBBB", "version": 2})[1]["code"] == "invalid_state"
    assert call(env, "GET", L + "/jobs/{jobId}", params={**jp, "locationId": LOC_B}, claims=OWNER_B)[0] == 404


def test_full_refund_in_stripe_voids_and_own_refund_ignored(env):
    sold(env)
    order = env.table.get_item(Key=et.order_key(TENANT_A, "01ORDERAAAAAAAAAAAAAAAAAAA"))["Item"]
    charge = {"id": "ch_1", "amount": 179000, "amount_refunded": 89500,
              "metadata": {"kind": "ticket", "tenantId": TENANT_A, "orderId": order["orderId"]}}
    hook = {"hook": "stripe.event", "tenantId": TENANT_A, "type": "charge.refunded", "account": ACCT_A,
            "object": charge}
    assert env.api.handler(hook)["result"] == "flagged:partial_refund"
    charge["amount_refunded"] = 179000
    assert env.api.handler(hook)["result"] == "voided"
    assert env.api.handler(hook)["result"] == "already_refunded"
    assert {t["status"] for t in tickets(env)} == {"refunded"} and env.stripe.refunds() == []


# --- hooks -----------------------------------------------------------------------------------------

def test_accounting_balanced_with_sale_and_refund(env):
    ticket_id = sold(env)[0]["ticketId"]
    env.clock.value = NOW + timedelta(days=1)
    call(env, "POST", L + "/tickets/{ticketId}/refund", params={"locationId": LOC_A, "ticketId": ticket_id},
         body={"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "x"}, claims=OWNER_A)
    out = env.api.handler({"hook": "accounting.events", "tenantId": TENANT_A, "locationId": LOC_A,
                           "first": "2026-11-05", "last": "2026-11-06"})
    assert out["ok"] and [d["day"] for d in out["events"]] == ["2026-11-05", "2026-11-06"]
    for day in out["events"]:
        assert sum(p["amount"] for p in day["postings"]) == 0
        assert {p["role"] for p in day["postings"]} <= set(accounting.ROLES)
    sale = {p["role"]: p["amount"] for p in out["events"][0]["postings"]}
    assert sale == {"stripe": 179000, "sales12": -159821, "vat12": -19179}
    assert env.api.handler({"hook": "accounting.events", "tenantId": TENANT_B, "locationId": LOC_B,
                            "first": "2026-11-05", "last": "2026-11-06"}) == {"ok": True, "events": []}


def test_insights_hook(env):
    sold(env)
    out = env.api.handler({"hook": "insights.events", "tenantId": TENANT_A, "locationIds": [LOC_A],
                           "first": "2026-11-01", "last": "2026-12-31"})
    assert out["ok"]
    by_day = {d["day"]: d for d in out["days"]}
    assert by_day["2026-11-05"]["ticketsSold"] == 2 and by_day["2026-11-05"]["revenueOre"] == 179000
    assert by_day["2026-12-04"]["occurrences"] == 1
    assert env.api.handler({"hook": "insights.events", "tenantId": TENANT_B, "locationIds": [LOC_B],
                            "first": "2026-11-01", "last": "2026-12-31"}) == {"ok": True, "days": []}


def test_privacy_export_and_erase(env):
    sold(env)
    export = env.api.handler({"hook": "privacy.export", "tenantId": TENANT_A, "email": "sara@example.se"})
    assert export["ok"] and export["items"][0]["kind"] == "ticket_order"
    assert env.api.handler({"hook": "privacy.export", "tenantId": TENANT_B, "email": "sara@example.se"})["items"] == []
    erased = env.api.handler({"hook": "privacy.erase", "tenantId": TENANT_A, "email": "sara@example.se"})
    assert erased["erased"]["ticketOrders"] == 1 and erased["kept"]["ticketTransactions"] == 1
    order = env.table.get_item(Key=et.order_key(TENANT_A, "01ORDERAAAAAAAAAAAAAAAAAAA"))["Item"]
    assert "buyerEmail" not in order and "buyerName" not in order
    assert all("holderName" not in t and t["GSI1SK"].startswith("#") for t in tickets(env))
    assert {t["status"] for t in tickets(env)} == {"valid"}


def test_purge_keeps_tx_rows(env):
    sold(env)
    out = env.api.handler({"hook": "tenant.purge", "tenantId": TENANT_A, "locationIds": [LOC_A], "round": 1})
    assert out["ok"] and out["done"] and out["counts"]["keptUntilBookkeepingEnds"] == 1
    rows = env.table.scan()["Items"]
    assert [r["SK"][:3] for r in rows] == ["TX#"] and rows[0]["ttl"] > NOW.timestamp() + 6 * 365 * 86400


def test_nightly_cleans_contacts_after_24_months(env):
    """Order, its tickets, the ticket a guest was given and the transfer: all
    found by key (deterministic ids and the transfer's per-ticket index row)."""
    sold(env)
    tok = token_of(env)
    ticket_id = sorted(t["ticketId"] for t in tickets(env))[0]
    call(env, "POST", "/public/tickets/{token}/transfers", params={"token": tok},
         body={"transferId": "01TRANSFERAAAAAAAAAAAAAAAA", "ticketId": ticket_id, "kind": "gift",
               "toEmail": "vän@example.se"})
    call(env, "POST", "/public/ticket-transfers/{transferToken}/accept",
         params={"transferToken": qr.token("x", TENANT_A, "01TRANSFERAAAAAAAAAAAAAAAA")}, body={"name": "Vän"})
    env.table.update_item(Key=et.transfer_key(TENANT_A, "01TRANSFERAAAAAAAAAAAAAAAA"), UpdateExpression="REMOVE #t",
                          ExpressionAttributeNames={"#t": "ttl"})  # moto never expires rows; keep it explicit
    env.clock.value = datetime(2028, 12, 10, 3, 0, tzinfo=timezone.utc)
    assert env.api.handler({"job": "nightly"})["cleaned"] == 1
    order = env.table.get_item(Key=et.order_key(TENANT_A, "01ORDERAAAAAAAAAAAAAAAAAAA"))["Item"]
    assert "buyerEmail" not in order and "GSI2PK" not in order
    assert len(tickets(env)) == 3 and all("holderName" not in t and "holderEmailHash" not in t for t in tickets(env))
    transfer = env.table.get_item(Key=et.transfer_key(TENANT_A, "01TRANSFERAAAAAAAAAAAAAAAA"))["Item"]
    assert "toEmail" not in transfer and "toEmailHash" not in transfer
    assert env.api.handler({"job": "nightly"})["cleaned"] == 0


def test_unknown_hook_and_route(env):
    assert env.api.handler({"hook": "nope", "tenantId": TENANT_A})["code"] == "unknown_hook"
    assert env.api.handler({"hook": "stripe.event"})["code"] == "tenant_required"
    assert call(env, "GET", "/nope")[0] == 404


def test_payment_for_a_cancelled_occurrence_is_refunded(env):
    published(env)
    buy(env)
    p = {"locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1}
    assert call(env, "POST", OCC + "/cancel", params=p, claims=OWNER_A,
                body={"jobId": "01JOBAAAAAAAAAAAAAAAAAAAAA", "version": 1})[0] == 202
    assert pay(env)["result"] == "refunded:cancelled"
    assert tickets(env) == [] and env.stripe.refunds()[-1]["key"] == "ticket-late-01ORDERAAAAAAAAAAAAAAAAAAA"
    assert occ_row(env)["held"] == 0
