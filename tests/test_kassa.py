"""Kassa (functions/kassa, shared/kassa.py): a full sale on the simulated
reader and the simulated control system, gap-free serial numbers, no
receipt without a control code, the append-only journal and its hash
chain, returns, the one copy, training mode, X and Z reports, discounts,
the feature flag and tenant isolation. DynamoDB is moto; Stripe is the
recording fake from test_card_payments with partial refunds."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import card_payments as cp
from shared import control_system as cs
from shared import dynamo as shared_dynamo
from shared import kassa, kassa_receipt, print_ticket, printing, stripe_client
from shared import orders as o
from shared import tenant as shared_tenant
from tenant_support import (LOC_A, LOC_B, REGION, STAFF_A_SUB, TENANT_A, TENANT_B, TENANT_TABLE, create_tables,
                            tenant_row, with_tenant_claims)
from test_card_payments import FakeStripe, load_fn, location_item

ACCOUNT = "acct_roma"
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)  # 14:00 in Stockholm
PRINTER = "d" * 32
FEATURES = {"reservations": True, "ordering": True, "catering": True, "terminal": True, "kassa": True}
APP = Path(__file__).parents[1]


class KassaStripe(FakeStripe):
    """Partial refunds: each refund takes `amount` (or the rest)."""

    def _handle(self, method, path, params, account):
        if path == "/v1/refunds":
            intent = self.intents[params["payment_intent"]]
            charge = self.charges[intent["latest_charge"]]
            amount = int(params.get("amount") or intent["amount"] - charge["amount_refunded"])
            charge["amount_refunded"] += amount
            return {"id": f"re_{self.count('/v1/refunds')}", "amount": amount, "status": "succeeded"}
        return super()._handle(method, path, params, account)


def _table(res, name, extra=(), gsis=()):
    res.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", *extra)],
        BillingMode="PAY_PER_REQUEST", **({"GlobalSecondaryIndexes": list(gsis)} if gsis else {}))


def _menu(res, item_id, name, price, category="mains", **extra):
    res.Table("test-menu").put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": f"MENU#{item_id}", "menuItemId": item_id,
                                          "name": name, "price": Decimal(price), "category": category,
                                          "active": True, **extra})


@pytest.fixture
def env(monkeypatch):
    for key, value in {
        "ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_SESSION_TOKEN": "t",
        "AWS_DEFAULT_REGION": REGION, "ORDER_TABLE_NAME": "test-order", "USER_TABLE_NAME": "test-user",
        "KASSA_TABLE_NAME": "test-kassa", "KASSA_JOURNAL_TABLE_NAME": "test-kassa-journal",
        "MENU_TABLE_NAME": "test-menu", "PRINT_TABLE_NAME": "test-print",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("KASSA_CERTIFIED", raising=False)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        res = boto3.resource("dynamodb", region_name=REGION)
        for name in ("test-order", "test-kassa", "test-menu"):
            _table(res, name)
        _table(res, "test-kassa-journal", ("regDay",), [
            {"IndexName": "byDay", "Projection": {"ProjectionType": "ALL"},
             "KeySchema": [{"AttributeName": "regDay", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}]}])
        _table(res, "test-print", ("queue", "dueAt"), [
            {"IndexName": "queue", "Projection": {"ProjectionType": "ALL"},
             "KeySchema": [{"AttributeName": "queue", "KeyType": "HASH"}, {"AttributeName": "dueAt", "KeyType": "RANGE"}]}])
        create_tables(monkeypatch)
        res.Table(TENANT_TABLE).put_item(Item=tenant_row(
            TENANT_A, name="Roma", features=FEATURES, stripe={"accountId": ACCOUNT}, legalName="Roma AB",
            orgNumber="556677-8899"))
        res.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_B, features=FEATURES, orgNumber="556000-0001"))
        res.Table("test-location").put_item(Item=location_item(TENANT_A, LOC_A, {"accountId": ACCOUNT,
                                                                                  "locationId": "tml_a1"}))
        res.Table("test-location").put_item(Item=location_item(TENANT_B, LOC_B, {"accountId": "acct_other",
                                                                                  "locationId": "tml_b1"}))
        res.Table("test-user").put_item(Item={"PK": f"USER#{STAFF_A_SUB}", "SK": "PROFILE",
                                              "tenantId": TENANT_A, "locationId": LOC_A, "status": "active"})
        res.Table("test-print").put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": "SETTINGS", "printerId": PRINTER,
                                               "autoPrint": True, "paperWidth": 80})
        _menu(res, "pasta", "Pasta carbonara", "149")
        _menu(res, "soup", "Dagens soppa", "89.50", category="starters")
        _menu(res, "wine", "Husets rött, glas", "95", category="drinks", alcohol=True)
        fake = KassaStripe()
        monkeypatch.setattr(stripe_client, "request", fake)
        clock = {"now": NOW}
        monkeypatch.setattr(o, "now_utc", lambda: clock["now"])
        fn = load_fn("kassa", "kassa_fn")
        yield {"fn": fn, "res": res, "stripe": fake, "clock": clock}
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()


# --- helpers ---------------------------------------------------------------------------

def call(env, method, suffix="", *, body=None, query=None, role="owner_user", tenant_id=TENANT_A, location_id=LOC_A,
         sub=None):
    if suffix:
        event = {"routeKey": f"{method} /locations/{{locationId}}/kassa/{{proxy+}}",
                 "pathParameters": {"locationId": location_id, "proxy": suffix}}
    else:
        event = {"routeKey": f"{method} /locations/{{locationId}}/kassa", "pathParameters": {"locationId": location_id}}
    if body is not None:
        event["body"] = json.dumps(body)
    if query is not None:
        event["queryStringParameters"] = query
    event = with_tenant_claims(event, tenant_id, role, sub)
    event["requestContext"]["authorizer"]["jwt"]["claims"]["email"] = "anna@roma.se"
    response = env["fn"].handler(event, None)
    if response.get("headers", {}).get("Content-Type", "").startswith("application/xml"):
        return response["statusCode"], response["body"]
    return response["statusCode"], json.loads(response["body"])


def register(env, label="Kassa 1"):
    status, body = call(env, "POST", "registers", body={"label": label})
    assert status == 201, body
    return body["register"]["registerId"]


def bill(env, register_id, kind="table", table="5", lines=None, discount=None):
    status, created = call(env, "POST", "bills", body={"registerId": register_id, "kind": kind, "tableLabel": table})
    assert status == 201, created
    if lines is None:
        lines = [{"menuItemId": "pasta", "quantity": 2, "note": "utan lök"}, {"menuItemId": "soup", "quantity": 1}]
    status, updated = call(env, "PUT", f"bills/{created['billId']}",
                           body={"version": created["version"], "lines": lines, "discount": discount})
    assert status == 200, updated
    return updated


def pay(env, bill_id, amount, checkout, *, approve=True):
    status, payment = call(env, "POST", f"bills/{bill_id}/payments",
                           body={"checkoutId": checkout, "readerId": "tmr_a1", "amount": amount})
    assert status == 201, payment
    intent = env["stripe"].intents[env["stripe"].keys[f"card-pi-{checkout}"]["id"]]
    if approve:
        env["stripe"].pay(intent["id"])
        cp.succeed(cp.load(LOC_A, checkout), env["stripe"]._intent(intent["id"], True), env["clock"]["now"],
                   actor="stripe")
    return payment


def sale(env, register_id, *, checkout="a" * 32, delivery="print", **kw):
    b = bill(env, register_id, **kw)
    pay(env, b["billId"], b["total"], checkout)
    status, body = call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": delivery})
    assert status == 201, body
    return b, body


def journal(env, register_id):
    return sorted(env["res"].Table("test-kassa-journal").query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(f"REG#{register_id}"))["Items"],
        key=lambda e: int(e["jseq"]))


def reg_row(env, register_id):
    return env["res"].Table("test-kassa").get_item(Key=kassa.register_key(LOC_A, register_id))["Item"]


def print_jobs(env):
    items = env["res"].Table("test-print").scan()["Items"]
    return [i for i in items if i["SK"].startswith("JOB#kassa#")]


def receipts_of(env, register_id, kind=kassa.SALE):
    return [e for e in journal(env, register_id) if e["type"] == kind]


# --- the full sale ------------------------------------------------------------------------

def test_full_sale_end_to_end(env):
    rid = register(env)
    assert call(env, "POST", f"registers/{rid}/open-day", body={"float": 0})[0] == 200
    b = bill(env, rid)
    # 2 x 149 + 89,50 = 387,50 kr at the table: 12 % VAT
    assert b["total"] == 38750 and [v["rate"] for v in b["vat"]] == [12]
    status, sent = call(env, "POST", f"bills/{b['billId']}/send", body={})
    assert status == 200 and all(line["sent"] for line in sent["lines"])
    pay(env, b["billId"], 38750, "a" * 32)
    status, done = call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "print"})
    assert status == 201 and done["problems"] == []
    doc = done["document"]
    texts = [r.get("text") or " ".join(r.get("pair") or []) for r in doc["rows"]]
    assert doc["title"] == "Kvitto" and doc["seq"] == 1
    assert kassa_receipt.TEST_NOTICE in texts
    assert any("Org.nr 556677-8899" in t for t in texts) and any("Götgatan 1" in t for t in texts)
    assert any(t.startswith("Kassabeteckning Kassa 1") for t in texts)
    assert any("Kort Visa **** 4242" in t for t in texts)
    assert any(t == "Kontrollsystem: BOOQYSIMULATOR001" for t in texts)
    sale_entry = receipts_of(env, rid)[0]
    data = sale_entry["data"]
    assert len(data["controlCode"]) == cs.CODE_LENGTH and data["total"] == 38750
    assert data["vat"] == [{"rate": 12, "gross": 38750, "net": 34598, "vat": 4152}]
    assert sale_entry["user"] == "anna@roma.se" and sale_entry["local"] == "2026-10-24T14:00:00"
    row = reg_row(env, rid)
    assert row["receiptSeq"] == 1 and row["gtSales"] == 38750 and "lockId" not in row
    assert call(env, "GET", f"bills/{b['billId']}")[1]["status"] == "closed"
    # kitchen ticket (send) + the receipt; finalize sends nothing new to the kitchen
    kinds = sorted(j["ticket"]["kind"] for j in print_jobs(env))
    assert kinds == ["kassakvitto", "kitchen"]
    ticket = print_ticket.document_ticket(next(j["ticket"] for j in print_jobs(env)
                                                if j["ticket"]["kind"] == "kassakvitto"))
    assert "Testläge".encode("cp1252") in ticket and b"BOOQYSIMULATOR001" in ticket


def test_receipt_cannot_be_made_without_full_payment(env):
    rid = register(env)
    b = bill(env, rid)
    assert call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "print"}) == (
        409, {"error": "not_fully_paid"})
    pay(env, b["billId"], 10000, "a" * 32, approve=False)
    assert call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "print"})[1] == {
        "error": "payment_in_progress"}
    assert reg_row(env, rid)["receiptSeq"] == 0


def test_split_payment_two_cards(env):
    rid = register(env)
    b = bill(env, rid)
    pay(env, b["billId"], 20000, "a" * 32)
    assert call(env, "POST", f"bills/{b['billId']}/payments",
                body={"checkoutId": "c" * 32, "readerId": "tmr_a1", "amount": 20000}) == (
        409, {"error": "amount_too_large"})
    pay(env, b["billId"], 18750, "b" * 32)
    status, done = call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "screen"})
    assert status == 201
    assert [p["amount"] for p in receipts_of(env, rid)[0]["data"]["payments"]] == [20000, 18750]


def test_bill_is_locked_once_a_card_payment_runs(env):
    rid = register(env)
    b = bill(env, rid)
    pay(env, b["billId"], 1000, "a" * 32, approve=False)
    current = call(env, "GET", f"bills/{b['billId']}")[1]
    assert current["locked"] is True
    assert call(env, "PUT", f"bills/{b['billId']}", body={"version": current["version"], "lines": []})[1] == {
        "error": "bill_has_payments"}


# --- control system ------------------------------------------------------------------------

def test_control_system_down_stops_card_payment_before_money_is_taken(env):
    rid = register(env)
    b = bill(env, rid)
    call(env, "POST", f"registers/{rid}/simulate", body={"down": True})
    assert call(env, "POST", f"bills/{b['billId']}/payments",
                body={"checkoutId": "a" * 32, "readerId": "tmr_a1", "amount": 38750}) == (
        503, {"error": "control_system_unreachable"})
    assert env["stripe"].count("/v1/payment_intents") == 0


def test_no_receipt_without_control_code_and_no_number_burned(env):
    rid = register(env)
    sale(env, rid)
    b = bill(env, rid)
    pay(env, b["billId"], b["total"], "b" * 32)
    call(env, "POST", f"registers/{rid}/simulate", body={"down": True})
    assert call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "print"}) == (
        503, {"error": "control_system_unreachable"})
    assert reg_row(env, rid)["receiptSeq"] == 1
    assert journal(env, rid)[-1]["type"] == kassa.STATUS
    assert call(env, "GET", f"bills/{b['billId']}")[1]["status"] == "open"
    call(env, "POST", f"registers/{rid}/simulate", body={"down": False})
    status, done = call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "print"})
    assert status == 201 and done["document"]["seq"] == 2


def test_serial_numbers_are_gap_free_and_unique(env):
    rid = register(env)
    for i in range(3):
        sale(env, rid, checkout=f"{i + 1:032x}")
    assert [e["data"]["seq"] for e in receipts_of(env, rid)] == [1, 2, 3]
    entries = journal(env, rid)
    assert [int(e["jseq"]) for e in entries] == list(range(1, len(entries) + 1))
    assert kassa.verify_chain(entries) is None


def test_kvittodata_follows_skvfs_2020_9(env):
    data = cs.ReceiptData(kind=cs.NORMAL, at=datetime(2026, 10, 24, 14, 3, 7), org_number="556677-8899",
                          register_label="Kassa 1", address="Götgatan 1", serial=17,
                          sales_by_rate={25: 10095, 6: 9000}, vat_by_rate={25: 2019, 6: 509})
    out = data.kvittodata()
    assert out["N1"] == "20261024140307" and out["N2"] == "5566778899" and out["N5"] == "17"
    assert out["N6"] == "F060%90,00F250%100,95" and out["N7"] == "190,95"
    assert out["N10"] == "M060%5,09M250%20,19" and out["N9"] == "0,00"


# --- journal ---------------------------------------------------------------------------

def test_journal_tampering_is_detected(env):
    rid = register(env)
    sale(env, rid)
    entries = journal(env, rid)
    assert kassa.verify_chain(entries) is None
    entries[1]["data"]["total"] = 1
    assert kassa.verify_chain(entries) == 2


def test_journal_is_written_append_only():
    """No code path updates or deletes a journal item, and the retention
    jobs never name the journal table."""
    source = (APP / "shared" / "kassa.py").read_text()
    journal_writes = [line for line in source.splitlines() if "_journal()." in line]
    assert journal_writes and all("_journal().query" in line or "_journal().get_item" in line
                                  for line in journal_writes)
    for name in ("data-retention", "tenant-purge", "guest-privacy"):
        text = (APP / "functions" / name / "app.py").read_text()
        assert "KASSA_JOURNAL" not in text and "kassa-journal" not in text and "KASSA_TABLE" not in text
        if name != "tenant-purge":   # tenant-purge deletes only Kassa's fiskaly key secret
            assert "kassa" not in text.lower()
    assert "KASSA" not in (APP / "shared" / "retention.py").read_text().upper()


def test_removing_a_sent_line_is_journalled(env):
    rid = register(env)
    b = bill(env, rid)
    sent = call(env, "POST", f"bills/{b['billId']}/send", body={})[1]
    pasta = next(line for line in sent["lines"] if line["menuItemId"] == "pasta")
    status, after = call(env, "PUT", f"bills/{b['billId']}",
                         body={"version": sent["version"], "lines": [{"lineId": pasta["lineId"], "quantity": 1,
                                                                      "note": "utan lök"}]})
    assert status == 200 and after["total"] == 14900
    removed = [e for e in journal(env, rid) if e["type"] == kassa.DELETED][0]["data"]
    assert sorted((line["name"], line["quantity"]) for line in removed["lines"]) == [
        ("Dagens soppa", 1), ("Pasta carbonara", 1)]
    # a sent line can't be changed upwards or re-noted
    assert call(env, "PUT", f"bills/{b['billId']}", body={"version": after["version"], "lines": [
        {"lineId": pasta["lineId"], "quantity": 3, "note": "utan lök"}]})[1] == {"error": "line_sent"}


def test_prices_are_fixed_when_the_line_is_added(env):
    rid = register(env)
    b = bill(env, rid)
    _menu(env["res"], "pasta", "Pasta carbonara", "999")
    lines = [{"lineId": line["lineId"], "quantity": line["quantity"], "note": line.get("note")} for line in b["lines"]]
    status, same = call(env, "PUT", f"bills/{b['billId']}", body={"version": b["version"], "lines": lines})
    assert status == 200 and same["total"] == 38750


def test_takeaway_vat_and_no_alcohol(env):
    rid = register(env)
    b = bill(env, rid, kind="takeaway", table=None, lines=[{"menuItemId": "pasta", "quantity": 1}])
    assert b["vat"][0]["rate"] == 6  # reduced food VAT 2026-04-01 to 2027-12-31 (shared/orders.py)
    assert call(env, "PUT", f"bills/{b['billId']}", body={"version": b["version"], "lines": [
        {"menuItemId": "wine", "quantity": 1}]})[1] == {"error": "alcohol_not_for_takeaway"}
    wine = bill(env, rid, lines=[{"menuItemId": "wine", "quantity": 2}])
    assert wine["vat"][0]["rate"] == 25


def test_discount_is_spread_over_lines_and_vat(env):
    rid = register(env)
    b = bill(env, rid, lines=[{"menuItemId": "pasta", "quantity": 1}, {"menuItemId": "wine", "quantity": 1}],
             discount={"kind": "percent", "value": 10, "label": "Personal"})
    assert b["gross"] == 24400 and b["discountAmount"] == 2440 and b["total"] == 21960
    assert sum(line["discount"] for line in b["lines"]) == 2440
    by_rate = {v["rate"]: v for v in b["vat"]}
    assert by_rate[12]["gross"] == 13410 and by_rate[25]["gross"] == 8550
    assert sum(line["vat"] for line in b["lines"]) == sum(v["vat"] for v in b["vat"])


# --- returns, copies, training ---------------------------------------------------------------

def test_partial_return_refunds_the_card_and_continues_the_series(env):
    rid = register(env)
    b, _ = sale(env, rid)
    pasta = next(line for line in b["lines"] if line["menuItemId"] == "pasta")
    status, info = call(env, "GET", f"registers/{rid}/receipts/1")
    assert status == 200 and info["copyTaken"] is False
    status, done = call(env, "POST", f"registers/{rid}/receipts/1/return", body={
        "lines": [{"lineId": pasta["lineId"], "quantity": 1}], "reason": "Fel rätt", "delivery": "print"})
    assert status == 201, done
    assert done["document"]["title"] == "Returkvitto" and done["document"]["seq"] == 2
    refund = [c for c in env["stripe"].calls if c["path"] == "/v1/refunds"][0]
    assert refund["params"]["amount"] == 14900
    row = reg_row(env, rid)
    assert row["receiptSeq"] == 2 and row["gtReturn"] == 14900
    assert call(env, "POST", f"registers/{rid}/receipts/1/return", body={
        "lines": [{"lineId": pasta["lineId"], "quantity": 2}], "reason": "x", "delivery": "print"})[1] == {
        "error": "return_too_large"}
    status, again = call(env, "POST", f"registers/{rid}/receipts/1/return", body={
        "lines": [{"lineId": pasta["lineId"], "quantity": 1}], "reason": "Kall", "delivery": "screen"})
    assert status == 201 and again["document"]["seq"] == 3
    assert cp.load(LOC_A, "a" * 32)["refund"]["amount"] == 29800


def test_card_payments_of_kassa_are_refunded_only_in_kassa(env):
    rid = register(env)
    sale(env, rid)
    with pytest.raises(cp.InvalidState) as err:
        cp.refund(cp.load(LOC_A, "a" * 32), actor="x", now=NOW)
    assert err.value.code == "refund_via_kassa"


def test_only_one_copy_per_receipt(env):
    rid = register(env)
    sale(env, rid)
    status, copy = call(env, "POST", f"registers/{rid}/receipts/1/copy", body={"delivery": "print"})
    assert status == 201
    rows = [r.get("text") for r in copy["document"]["rows"]]
    assert rows.count("KOPIA") == 2 and any(r and r.startswith("Kopia nr 1") for r in rows)
    assert next(r for r in copy["document"]["rows"] if r.get("text") == "KOPIA")["big"] is True
    assert call(env, "POST", f"registers/{rid}/receipts/1/copy", body={"delivery": "print"}) == (
        409, {"error": "copy_exists"})
    assert reg_row(env, rid)["copySeq"] == 1


def test_training_mode(env):
    rid = register(env)
    call(env, "POST", f"registers/{rid}/training", body={"on": True})
    b = bill(env, rid)
    assert b["training"] is True
    assert call(env, "POST", f"bills/{b['billId']}/payments",
                body={"checkoutId": "a" * 32, "readerId": "tmr_a1", "amount": 1000})[1] == {
        "error": "training_no_payment"}
    status, done = call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "screen"})
    assert status == 201 and done["document"]["title"] == "Övningskvitto"
    assert [r.get("text") for r in done["document"]["rows"] if r.get("big")].count("ÖVNING") == 2
    row = reg_row(env, rid)
    assert row["trainingSeq"] == 1 and row["receiptSeq"] == 0 and row["gtSales"] == 0
    call(env, "POST", f"registers/{rid}/training", body={"on": False})
    assert [e["type"] for e in journal(env, rid)] == [kassa.START, kassa.TRAINING_ON, kassa.TRAINING, kassa.TRAINING_OFF]


# --- reports ---------------------------------------------------------------------------

def test_x_and_z_reports(env):
    rid = register(env)
    call(env, "POST", f"registers/{rid}/open-day", body={"float": 50000})
    b, _ = sale(env, rid)
    call(env, "POST", f"registers/{rid}/receipts/1/copy", body={"delivery": "print"})
    bill(env, rid, lines=[{"menuItemId": "soup", "quantity": 1}])  # left open
    status, x = call(env, "POST", f"registers/{rid}/x", body={"print": True})
    assert status == 200 and x["document"]["title"] == "X-dagrapport"
    data = journal(env, rid)[-1]["data"]
    assert data["totalSales"] == 38750 and data["receipts"] == 1 and data["copies"] == 1
    assert data["openBills"] == 1 and data["openBillsAmount"] == 8950 and data["float"] == 50000
    assert data["payments"] == [{"method": "Kort", "amount": 38750}]
    assert {g["name"] for g in data["groups"]} == {"Förrätter", "Varmrätter"}
    status, z = call(env, "POST", f"registers/{rid}/z", body={})
    assert status == 200 and z["document"]["seq"] == 1
    assert call(env, "POST", f"registers/{rid}/z", body={})[1]["document"]["seq"] == 2
    call(env, "POST", f"registers/{rid}/x", body={})
    after = journal(env, rid)[-1]["data"]
    assert after["totalSales"] == 0 and after["grandTotalSales"] == 38750
    sale(env, rid, checkout="b" * 32)
    assert receipts_of(env, rid)[-1]["data"]["zSeq"] == 3


def test_z_report_counts_sales_and_returns_separately(env):
    """Dev admin, 2026-10-27: one sale, all of it returned. KRAV 7 kap. 3 §:
    the sales total, VAT per rate and sales per payment means follow the
    sales; returns are their own item, with their VAT and refunds."""
    rid = register(env)
    b, done = sale(env, rid)
    sold = receipts_of(env, rid)[-1]["data"]
    lines = [{"lineId": line["lineId"], "quantity": line["quantity"]} for line in b["lines"]]
    status, _ = call(env, "POST", f"registers/{rid}/receipts/1/return",
                     body={"lines": lines, "reason": "Fel nota", "delivery": "print"})
    assert status == 201
    status, z = call(env, "POST", f"registers/{rid}/z", body={})
    data = journal(env, rid)[-1]["data"]
    total = sold["total"]
    sales_vat = [{"rate": v["rate"], "amount": v["vat"]} for v in sold["vat"]]
    assert data["totalSales"] == total and data["vat"] == sales_vat
    assert data["payments"] == [{"method": "Kort", "amount": total}]
    assert data["returns"] == 1 and data["returnsAmount"] == total
    assert data["returnVat"] == sales_vat and data["refunds"] == [{"method": "Kort", "amount": total}]
    assert data["grandTotalSales"] == total and data["grandTotalReturn"] == total and data["grandTotalNet"] == 0
    texts = [" ".join(r.get("pair") or [r.get("text") or ""]) for r in z["document"]["rows"]]
    amount = kassa_receipt.kr(total)
    assert f"Total försäljning {amount}" in texts and f"Betalt med kort {amount}" in texts
    assert f"Returer, 1 st {amount}" in texts and f"  återbetalt med kort {amount}" in texts
    for v in sold["vat"]:
        assert f"Moms {v['rate']} % {kassa_receipt.kr(v['vat'])}" in texts
        assert f"  varav moms {v['rate']} % {kassa_receipt.kr(v['vat'])}" in texts


def test_day_status_flags_an_unclosed_day(env):
    rid = register(env)
    sale(env, rid)
    env["clock"]["now"] = NOW + timedelta(days=1)
    status, body = call(env, "GET", f"registers/{rid}")
    assert status == 200 and body["day"]["unclosedDays"] == ["2026-10-24"]


# --- delivery ----------------------------------------------------------------------------

def test_email_receipt_is_queued_for_notification(env):
    rid = register(env)
    b = bill(env, rid)
    pay(env, b["billId"], b["total"], "a" * 32)
    status, done = call(env, "POST", f"bills/{b['billId']}/finalize",
                        body={"delivery": "email", "email": "Gast@Example.se"})
    assert status == 201
    mails = [i for i in env["res"].Table("test-order").scan()["Items"] if i["SK"].startswith("KASSAMAIL#")]
    assert len(mails) == 1 and mails[0]["receiptEmail"] == "gast@example.se"
    assert mails[0]["notice"]["type"] == kassa.NOTICE_RECEIPT and int(mails[0]["ttl"]) > NOW.timestamp()
    assert any(r.get("text") == "Elektroniskt kassakvitto" for r in done["document"]["rows"])
    subject, text, html_body = kassa_receipt.email(mails[0]["document"], {"name": "Roma"}, {"name": "Södermalm"})
    assert subject.startswith("Kvitto 387,50 kr") and "Kontrollkod" in text and "<table" in html_body
    # The address is never in the journal.
    assert "gast@example.se" not in json.dumps(kassa._plain(journal(env, rid)), ensure_ascii=False)


def test_screen_only_in_test_mode_and_printer_needed(env, monkeypatch):
    rid = register(env)
    b = bill(env, rid)
    pay(env, b["billId"], b["total"], "a" * 32)
    env["res"].Table("test-print").delete_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": "SETTINGS"})
    assert call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "print"})[1] == {"error": "no_printer"}
    monkeypatch.setattr(cs, "certified", lambda system: True)
    assert call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "screen"})[1] == {
        "error": "receipt_must_be_printed"}


def test_kassa_print_job_reaches_the_printer(env):
    job = printing.kassa_job(TENANT_A, LOC_A, {"rows": [{"text": "KOPIA", "big": True}]}, NOW)
    token = printing.job_token(job["SK"])
    assert printing.job_sk(token) == job["SK"]


# --- access ---------------------------------------------------------------------------

def test_feature_flag_is_off_by_default(env):
    env["res"].Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, name="Roma", orgNumber="556677-8899"))
    shared_tenant.reset_caches()
    assert call(env, "GET") == (403, {"error": "feature_not_in_plan"})


def test_state_says_test_mode(env):
    register(env)
    status, state = call(env, "GET")
    assert status == 200 and state["testMode"] is True and state["testNotice"] == kassa_receipt.TEST_NOTICE
    assert state["registers"][0]["controlSystem"]["simulated"] is True and state["canSimulate"] is True


def test_other_tenants_register_is_not_found(env):
    rid = register(env)
    assert call(env, "GET", f"registers/{rid}", tenant_id=TENANT_B, location_id=LOC_B)[0] == 404
    assert call(env, "POST", "bills", body={"registerId": rid, "kind": "takeaway"}, tenant_id=TENANT_B,
                location_id=LOC_B)[0] == 404


def test_staff_can_sell_but_not_create_registers_or_export(env):
    rid = register(env)
    assert call(env, "POST", "registers", body={"label": "Baren"}, role="staff_user", sub=STAFF_A_SUB)[1] == {
        "error": "owner_only"}
    assert call(env, "GET", f"registers/{rid}/export", query={"from": "2026-10-24", "to": "2026-10-24"},
                role="staff_user", sub=STAFF_A_SUB)[0] == 403
    status, _ = call(env, "POST", "bills", body={"registerId": rid, "kind": "takeaway"}, role="staff_user",
                     sub=STAFF_A_SUB)
    assert status == 201


def test_register_labels_are_unique_and_limited(env):
    register(env, "Kassa 1")
    assert call(env, "POST", "registers", body={"label": "kassa 1"})[1] == {"error": "label_taken"}
    assert call(env, "POST", "registers", body={"label": "x" * 17})[0] == 400


def test_org_number_is_required(env):
    env["res"].Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, name="Roma", features=FEATURES,
                                                            stripe={"accountId": ACCOUNT}))
    shared_tenant.reset_caches()
    rid = register(env)
    b = bill(env, rid)
    pay(env, b["billId"], b["total"], "a" * 32)
    assert call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "print"})[1] == {
        "error": "org_number_missing"}


def test_org_digits():
    assert kassa.org_digits("556677-8899") == "5566778899"
    assert kassa.org_digits("800101-1234", datetime(2026, 1, 1)) == "198001011234"
    assert kassa.org_digits("abc") is None


def test_partial_refund_is_idempotent(env):
    rid = register(env)
    sale(env, rid)
    item = cp.load(LOC_A, "a" * 32)
    once = cp.refund_part(item, 1000, key="k1", actor="x", now=NOW)
    again = cp.refund_part(once, 1000, key="k1", actor="x", now=NOW)
    assert again["refund"]["amount"] == 1000 and env["stripe"].count("/v1/refunds") == 1
    with pytest.raises(cp.InvalidState):
        cp.refund_part(again, 38750, key="k2", actor="x", now=NOW)


def test_print_device_renders_a_kassa_job(env, monkeypatch):
    monkeypatch.setenv("REQUEST_GUARD_TABLE_NAME", "test-guard")
    device = load_fn("print-device", "print_device_kassa")
    job = printing.kassa_job(TENANT_A, LOC_A, {"rows": [{"text": "KOPIA", "big": True},
                                                        {"pair": ["Totalt", "387,50"], "bold": True}]}, NOW)
    body = device._render({**job, "attempts": 1}, {"name": "Kassan"}, None, {"paperWidth": 58}, NOW)
    assert b"KOPIA" in body and b"387,50" in body and b"KAN VARA DUBBLETT" not in body


# --- outages, felanmälan, checklist, manual -------------------------------------------------

def test_outage_start_and_end_are_recorded_once(env):
    rid = register(env)
    call(env, "POST", f"registers/{rid}/simulate", body={"down": True})
    call(env, "GET")
    call(env, "GET")
    assert reg_row(env, rid)["csDownSince"] == "2026-10-24T12:00:00Z"
    statuses = [e["data"]["code"] for e in journal(env, rid) if e["type"] == kassa.STATUS]
    assert statuses == ["EJ_KONTAKT_MED_KS"]
    call(env, "POST", f"registers/{rid}/simulate", body={"down": False})
    call(env, "GET")
    assert "csDownSince" not in reg_row(env, rid)
    assert [e["data"]["code"] for e in journal(env, rid) if e["type"] == kassa.STATUS] == [
        "EJ_KONTAKT_MED_KS", "KONTAKT_MED_KS"]


def test_felanmal_banner_after_thirty_minutes(env):
    rid = register(env)
    call(env, "POST", f"registers/{rid}/simulate", body={"down": True})
    assert call(env, "GET")[1]["registers"][0]["felanmal"] is False
    env["clock"]["now"] = NOW + timedelta(minutes=29)
    assert call(env, "GET")[1]["registers"][0]["felanmal"] is False
    env["clock"]["now"] = NOW + timedelta(minutes=30)
    state = call(env, "GET")[1]
    assert state["registers"][0]["felanmal"] is True and state["felanmalAfterMinutes"] == 30


def test_provider_status_asks_for_felanmalan_at_once(env):
    reg = {"csDownSince": "2026-10-24T12:00:00Z"}
    status = cs.Status(ok=False, code=cs.STATUS_REPORT_24H, provider="x", serial="s")
    assert kassa.felanmal_due(reg, status, NOW) is True
    assert kassa.felanmal_due(reg, cs.Status(ok=False, code=cs.STATUS_NO_CONTACT, provider="x", serial="s"), NOW) is False


def test_outage_notice_is_audited(env):
    rid = register(env)
    assert call(env, "POST", f"registers/{rid}/outage-notice", body={"action": "shown"})[1] == {
        "error": "control_system_up"}
    call(env, "POST", f"registers/{rid}/simulate", body={"down": True})
    call(env, "GET")
    for action in ("shown", "shown", "dismissed", "dismissed"):
        assert call(env, "POST", f"registers/{rid}/outage-notice", body={"action": action})[0] == 200
    audit = sorted(i["action"] for i in env["res"].Table("test-kassa").scan()["Items"] if i["SK"].startswith("AUDIT#"))
    assert audit == ["dismissed", "dismissed", "shown"]
    assert reg_row(env, rid)["outageNoticeShown"] == "2026-10-24T12:00:00Z"


def test_failed_payment_check_marks_the_outage(env):
    rid = register(env)
    b = bill(env, rid)
    call(env, "POST", f"registers/{rid}/simulate", body={"down": True})
    call(env, "POST", f"bills/{b['billId']}/payments", body={"checkoutId": "a" * 32, "readerId": "tmr_a1",
                                                             "amount": 1000})
    assert reg_row(env, rid)["csDownCode"] == "EJ_KONTAKT_MED_KS"


def test_setup_checklist_per_location(env):
    status, body = call(env, "GET", "setup")
    assert status == 200 and body["items"]["anmalan"] == {"done": False}
    status, body = call(env, "PUT", "setup", body={"item": "anmalan", "done": True})
    assert status == 200 and body["items"]["anmalan"]["done"] is True and body["items"]["anmalan"]["by"] == "anna@roma.se"
    assert call(env, "PUT", "setup", body={"item": "x", "done": True})[0] == 400
    assert call(env, "PUT", "setup", body={"item": "skylt", "done": True}, role="staff_user", sub=STAFF_A_SUB)[1] == {
        "error": "owner_only"}
    assert call(env, "GET", "setup", tenant_id=TENANT_B, location_id=LOC_B)[1]["items"]["anmalan"] == {"done": False}


def test_manual_is_served_from_the_docs(env):
    status, body = call(env, "GET", "manual")
    assert status == 200 and body["markdown"].startswith("# Bruksanvisning för Booqy Kassa")


def test_state_carries_the_company_for_the_manual_sheet(env):
    register(env)
    assert call(env, "GET")[1]["company"]["org"] == "5566778899"


def test_identity_and_version_can_be_read(env, monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_VERSION", "42")
    rid = register(env)
    reg = call(env, "GET", f"registers/{rid}")[1]["register"]
    assert reg["model"] == "Booqy Kassa SE" and "SE" in reg["model"].split()
    assert reg["manufacturer"] == "Booqy" and reg["version"] == f"{kassa.KASSA_VERSION}+42"
    assert len(reg["serialNumber"]) == 17 and reg["serialNumber"].isalnum()
    assert reg["controlSystem"]["serial"] == cs.SIMULATOR_SERIAL
