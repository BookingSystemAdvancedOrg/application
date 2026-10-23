"""Kitchen printing: ticket layout (StarPRNT, cp1252), the auto job from the
order stream (idempotent), the cancellation slip, the CloudPRNT device flow
(auth, MAC, lease, confirm, backoff, FAILED), the admin API and the sweep.
DynamoDB is moto."""

import base64
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

from shared import dynamo as shared_dynamo
from shared import print_ticket as t
from shared import printing as p
from shared import tenant as shared_tenant
from tenant_support import (LOC_A, LOC_B, REGION, STAFF_A_SUB, TENANT_A, TENANT_B, create_tables, location_row,
                            tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
NOW = datetime(2026, 7, 8, 16, 30, tzinfo=timezone.utc)  # 18:30 in Stockholm (summer time)
ORDER_ID = "a" * 32
_ser = TypeSerializer()


def load(name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table(res, name, extra=(), gsis=()):
    res.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", *extra)],
        BillingMode="PAY_PER_REQUEST", **({"GlobalSecondaryIndexes": list(gsis)} if gsis else {}))


def order(**extra):
    item = {"PK": f"LOCATION#{LOC_A}", "SK": f"ORDER#2026-07-08#{ORDER_ID}", "orderId": ORDER_ID,
            "tenantId": TENANT_A, "locationId": LOC_A, "date": "2026-07-08", "number": 12, "mode": "pickup",
            "status": "confirmed", "lines": [{"name": "Pizza Margherita", "quantity": 2, "unitPrice": 12500},
                                             {"name": "Cola", "quantity": 1, "unitPrice": 3550}],
            "totalAmount": 28550, "customerName": "Åsa Öberg", "customerPhone": "0701234567",
            "notes": "Ingen lök\x1b@ tack", "confirmedAt": "2026-07-08T16:30:00Z",
            "readyEstimate": "2026-07-08T16:50:00Z"}
    item.update(extra)
    return item


@pytest.fixture
def env(monkeypatch):
    for key, value in {"ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
                       "AWS_SESSION_TOKEN": "t", "AWS_DEFAULT_REGION": REGION, "ORDER_TABLE_NAME": "test-order",
                       "PRINT_TABLE_NAME": "test-print", "USER_TABLE_NAME": "test-user",
                       "REQUEST_GUARD_TABLE_NAME": "test-guard",
                       "PRINT_SERVER_URL": "https://api.example/print/cloudprnt"}.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        p._device_location.clear()
        res = boto3.resource("dynamodb", region_name=REGION)
        _table(res, "test-order")
        _table(res, "test-print", ("queue", "dueAt", "deviceKey"), [
            {"IndexName": "queue", "Projection": {"ProjectionType": "ALL"},
             "KeySchema": [{"AttributeName": "queue", "KeyType": "HASH"}, {"AttributeName": "dueAt", "KeyType": "RANGE"}]},
            {"IndexName": "byDevice", "Projection": {"ProjectionType": "KEYS_ONLY"},
             "KeySchema": [{"AttributeName": "deviceKey", "KeyType": "HASH"}]}])
        res.create_table(TableName="test-guard", KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
                         BillingMode="PAY_PER_REQUEST")
        create_tables(monkeypatch)
        res.Table("test-tenant").put_item(Item=tenant_row(TENANT_A))
        res.Table("test-tenant").put_item(Item=tenant_row(TENANT_B))
        res.Table("test-location").put_item(Item=location_row(TENANT_A, LOC_A, name="Roma", timezone="Europe/Stockholm"))
        res.Table("test-location").put_item(Item=location_row(TENANT_B, LOC_B))
        res.Table("test-user").put_item(Item={"PK": f"USER#{STAFF_A_SUB}", "SK": "PROFILE", "tenantId": TENANT_A,
                                              "locationId": LOC_A, "status": "active"})
        orders = res.Table("test-order")
        orders.put_item(Item=order())
        orders.put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": f"OID#{ORDER_ID}", "date": "2026-07-08"})
        clock = {"now": NOW}
        monkeypatch.setattr(p, "now_utc", lambda: clock["now"])
        monkeypatch.setattr(p, "metric", lambda name, value=1: metrics.append((name, value)))
        metrics = []
        yield {"res": res, "jobs": load("print-jobs", "print_jobs_app"), "device": load("print-device", "print_device_app"),
               "clock": clock, "metrics": metrics}
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()


def api(env, method, path, body=None, *, role="owner_user", tenant_id=TENANT_A, location=LOC_A):
    proxy = path.strip("/")
    route = "/locations/{locationId}/print" + ("/{proxy+}" if proxy else "")
    event = {"routeKey": f"{method} {route}", "pathParameters": {"locationId": location, **({"proxy": proxy} if proxy else {})},
             "body": json.dumps(body) if body is not None else None}
    resp = env["jobs"].handler(with_tenant_claims(event, tenant_id, role), None)
    return resp["statusCode"], json.loads(resp["body"])


def add_printer(env, **settings):
    status, body = api(env, "POST", "devices", {"name": "Köket"})
    assert status == 201
    if settings:
        current = p.get_settings(LOC_A)
        api(env, "PUT", "settings", {**current, **settings})
    return body["device"]["deviceId"], body["secret"]


def device_call(env, method, device_id, secret, *, query=None, body=None, auth=True, ip="198.51.100.7"):
    headers = {}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{device_id}:{secret}".encode()).decode()
    event = {"routeKey": f"{method} /print/cloudprnt/{{deviceId}}", "pathParameters": {"deviceId": device_id},
             "headers": headers, "queryStringParameters": query, "body": json.dumps(body) if body is not None else None,
             "requestContext": {"http": {"method": method, "sourceIp": ip}}}
    return env["device"].handler(event, None)


def stream(env, old, new):
    record = {"eventName": "MODIFY", "dynamodb": {
        "SequenceNumber": "1", "OldImage": {k: _ser.serialize(v) for k, v in old.items()},
        "NewImage": {k: _ser.serialize(v) for k, v in new.items()}}}
    return env["jobs"].handler({"Records": [record]}, None)


def job(env, sk):
    return p.get_job(LOC_A, sk)


AUTO_SK = f"JOB#{ORDER_ID}#auto"
CANCEL_SK = f"JOB#{ORDER_ID}#cancel"


def print_once(env, device_id, secret, code="200 OK"):
    poll = json.loads(device_call(env, "POST", device_id, secret, body={"printerMAC": "00:11:62:aa:bb:cc",
                                                                       "statusCode": "200%20OK"})["body"])
    assert poll["jobReady"] is True
    got = device_call(env, "GET", device_id, secret, query={"type": t.MEDIA_TYPE, "token": poll["jobToken"],
                                                            "mac": "00:11:62:aa:bb:cc"})
    assert got["statusCode"] == 200
    device_call(env, "DELETE", device_id, secret, query={"code": code, "token": poll["jobToken"],
                                                          "mac": "00:11:62:aa:bb:cc"})
    return base64.b64decode(got["body"])


# --- ticket ----------------------------------------------------------------------------------

def test_ticket_is_a_kitchen_note_in_cp1252_without_prices():
    out = t.order_ticket(order(), {"timezone": "Europe/Stockholm"}, width=80)
    text = out.decode("cp1252")
    assert "Köksnota, ej kvitto" in text and "AVHÄMTNING" in text and "#12" in text
    assert "Beställd 18:30, klar ca 18:50" in text  # Stockholm summer time
    assert "2 x Pizza Margherita" in text and "Åsa Öberg" in text and "Tel: 0701234567" in text
    assert "125" not in text and "kr" not in text
    assert "lök\x1b" not in text and "Ingen lök @ tack" in text  # control characters never reach the printer
    assert b"\x1bENOTERING:" in out
    assert out.startswith(b"\x1b@\x1b\x1dt\x20") and out.endswith(b"\x1bd\x03")


def test_ticket_table_order_hides_phone_and_winter_time():
    text = t.order_ticket(order(mode="table", tableLabel="5", confirmedAt="2026-01-15T11:05:00Z"),
                          {"timezone": "Europe/Stockholm"}).decode("cp1252")
    assert "BORD 5" in text and "Tel:" not in text and "Beställd 12:05" in text


def test_ticket_width_retry_and_copy():
    long = order(lines=[{"name": "En väldigt lång rätt med många ord som inte får plats", "quantity": 1}])
    out = t.order_ticket(long, {}, width=58, retry=True, copy=True).decode("cp1252")
    assert "KAN VARA DUBBLETT" in out and "KOPIA" in out
    rows = [r.split("\x1b")[0] for r in out.split("\n")]
    assert all(len(r) <= 32 for r in rows) and "-" * 32 in out and "-" * 33 not in out
    assert "-" * 48 in t.order_ticket(order(), {}, width=80).decode("cp1252")


def test_cancel_and_test_tickets():
    assert "MAKULERAD #12" in t.cancel_ticket(order(), {}, now=NOW).decode("cp1252")
    text = t.test_ticket({"name": "Roma"}, "Köket", width=58, now=NOW).decode("cp1252")
    assert "TESTUTSKRIFT" in text and "å ä ö" in text and "32 tecken" in text


# --- stream: auto job and cancellation ----------------------------------------------------------

def test_paid_order_creates_exactly_one_auto_job(env):
    add_printer(env, autoPrint=True)
    paid = (order(status="pending_payment"), order())
    assert stream(env, *paid) == {"batchItemFailures": []}
    stream(env, *paid)  # redelivered record
    jobs = p.order_jobs(LOC_A, ORDER_ID)
    assert [j["SK"] for j in jobs] == [AUTO_SK]
    assert jobs[0]["status"] == "PENDING" and "customerName" not in jobs[0] and jobs[0]["ttl"] > 0


def test_no_job_when_auto_print_is_off(env):
    add_printer(env)
    stream(env, order(status="pending_payment"), order())
    assert p.order_jobs(LOC_A, ORDER_ID) == []


def test_cancel_before_printing_drops_the_job_without_slip(env):
    add_printer(env, autoPrint=True)
    stream(env, order(status="pending_payment"), order())
    stream(env, order(), order(status="cancelled"))
    assert job(env, AUTO_SK)["status"] == "CANCELLED" and "queue" not in job(env, AUTO_SK)
    assert job(env, CANCEL_SK) is None


def test_cancel_after_printing_prints_makulerad(env):
    device_id, secret = add_printer(env, autoPrint=True)
    stream(env, order(status="pending_payment"), order())
    print_once(env, device_id, secret)
    assert job(env, AUTO_SK)["status"] == "PRINTED"
    env["res"].Table("test-order").update_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": f"ORDER#2026-07-08#{ORDER_ID}"},
                                               UpdateExpression="SET #s = :c", ExpressionAttributeNames={"#s": "status"},
                                               ExpressionAttributeValues={":c": "cancelled"})
    stream(env, order(), order(status="cancelled"))
    assert "MAKULERAD #12" in print_once(env, device_id, secret).decode("cp1252")
    assert job(env, CANCEL_SK)["status"] == "PRINTED"


# --- device flow ---------------------------------------------------------------------------------

def test_device_prints_and_confirms(env):
    device_id, secret = add_printer(env, autoPrint=True)
    stream(env, order(status="pending_payment"), order())
    data = print_once(env, device_id, secret).decode("cp1252")
    assert "#12" in data and "KAN VARA DUBBLETT" not in data
    done = job(env, AUTO_SK)
    assert done["status"] == "PRINTED" and done["attempts"] == 1 and "queue" not in done
    device = p.get_device(LOC_A, device_id)
    assert device["mac"] == "00:11:62:aa:bb:cc" and device["lastSeenAt"] == p.iso(NOW)
    assert json.loads(device_call(env, "POST", device_id, secret, body={})["body"]) == {"jobReady": False}


def test_wrong_secret_mac_and_rate_limit(env):
    device_id, secret = add_printer(env, autoPrint=True)
    assert device_call(env, "POST", device_id, "nope", body={})["statusCode"] == 401
    assert device_call(env, "POST", "f" * 32, secret, body={})["statusCode"] == 401
    device_call(env, "POST", device_id, secret, body={"printerMAC": "00:11:62:aa:bb:cc"})
    assert device_call(env, "POST", device_id, secret, body={"printerMAC": "00:11:62:00:00:01"})["statusCode"] == 401
    codes = {device_call(env, "POST", device_id, "nope", body={}, ip="192.0.2.9")["statusCode"] for _ in range(21)}
    assert 429 in codes
    assert device_call(env, "POST", device_id, secret, body={"printerMAC": "00:11:62:aa:bb:cc"})["statusCode"] == 200


def test_query_secret_fallback(env):
    device_id, secret = add_printer(env)
    resp = device_call(env, "POST", device_id, None, auth=False, query={"k": secret}, body={})
    assert resp["statusCode"] == 200


def test_error_backs_off_then_fails_after_five_attempts(env):
    device_id, secret = add_printer(env, autoPrint=True)
    stream(env, order(status="pending_payment"), order())
    clock = env["clock"]
    print_once(env, device_id, secret, code="520 Error")
    first = job(env, AUTO_SK)
    assert first["status"] == "PENDING" and first["dueAt"] == p.iso(NOW + timedelta(seconds=15))
    assert json.loads(device_call(env, "POST", device_id, secret, body={})["body"])["jobReady"] is False
    for wait in (15, 30, 60, 120):
        clock["now"] += timedelta(seconds=wait)
        data = print_once(env, device_id, secret, code="520 Error")
        assert "KAN VARA DUBBLETT" in data.decode("cp1252")
    failed = job(env, AUTO_SK)
    assert failed["status"] == "FAILED" and failed["attempts"] == 5 and failed["dueAt"] == p.FAILED_DUE
    assert ("PrintJobFailed", 1) in env["metrics"]
    status, body = api(env, "GET", "status", role="staff_user", tenant_id=TENANT_A)
    assert status == 200 and body["failed"] == 1 and body["orders"] == {ORDER_ID: "FAILED"}


def test_lease_expiry_reprints_as_possible_duplicate(env):
    device_id, secret = add_printer(env, autoPrint=True)
    stream(env, order(status="pending_payment"), order())
    token = json.loads(device_call(env, "POST", device_id, secret, body={})["body"])["jobToken"]
    device_call(env, "GET", device_id, secret, query={"token": token})  # never confirmed
    assert json.loads(device_call(env, "POST", device_id, secret, body={})["body"])["jobReady"] is False
    env["clock"]["now"] += timedelta(seconds=61)
    assert "KAN VARA DUBBLETT" in print_once(env, device_id, secret).decode("cp1252")


def test_auto_job_of_completed_order_is_dropped(env):
    device_id, secret = add_printer(env, autoPrint=True)
    stream(env, order(status="pending_payment"), order())
    env["res"].Table("test-order").put_item(Item=order(status="completed"))
    assert json.loads(device_call(env, "POST", device_id, secret, body={})["body"]) == {"jobReady": False}
    assert job(env, AUTO_SK)["status"] == "CANCELLED"


# --- admin API -------------------------------------------------------------------------------------

def test_owner_manages_printers_and_secret_is_shown_once(env):
    device_id, secret = add_printer(env)
    status, body = api(env, "GET", "")
    assert status == 200 and body["settings"]["printerId"] == device_id  # first printer becomes the order printer
    assert secret not in json.dumps(body) and "secretHash" not in json.dumps(body)
    assert body["devices"][0]["serverUrl"] == f"https://api.example/print/cloudprnt/{device_id}"
    status, rotated = api(env, "POST", f"devices/{device_id}/rotate")
    assert status == 200 and rotated["secret"] != secret
    assert device_call(env, "POST", device_id, secret, body={})["statusCode"] == 401
    assert device_call(env, "POST", device_id, rotated["secret"], body={})["statusCode"] == 200
    status, body = api(env, "DELETE", f"devices/{device_id}")
    assert status == 200 and body["devices"] == [] and body["settings"]["printerId"] is None
    assert device_call(env, "POST", device_id, rotated["secret"], body={})["statusCode"] == 401


def test_settings_validation_version_and_roles(env):
    device_id, _ = add_printer(env)
    current = p.get_settings(LOC_A)
    assert api(env, "PUT", "settings", {**current, "paperWidth": 70})[0] == 400
    assert api(env, "PUT", "settings", {**current, "printerId": "b" * 32})[0] == 400
    assert api(env, "PUT", "settings", {**current, "autoPrint": True}, role="staff_user")[0] == 403
    status, saved = api(env, "PUT", "settings", {**current, "autoPrint": True, "paperWidth": 58})
    assert status == 200 and saved["version"] == current["version"] + 1
    assert api(env, "PUT", "settings", {**current, "autoPrint": False})[0] == 409
    assert api(env, "GET", "", tenant_id=TENANT_B)[0] == 404


def test_reprint_and_test_print(env):
    device_id, secret = add_printer(env)
    status, body = api(env, "POST", "jobs", {"orderId": ORDER_ID}, role="staff_user")
    assert status == 202
    assert "KOPIA" in print_once(env, device_id, secret).decode("cp1252")
    assert api(env, "POST", "jobs", {"orderId": "c" * 32})[0] == 404
    assert api(env, "POST", f"devices/{device_id}/test")[0] == 202
    assert "TESTUTSKRIFT" in print_once(env, device_id, secret).decode("cp1252")


def test_sweep_counts_stuck_jobs(env):
    add_printer(env, autoPrint=True)
    stream(env, order(status="pending_payment"), order())
    assert env["jobs"].handler({"source": "sweep"}, None)["stuck"] == 0
    env["clock"]["now"] += timedelta(minutes=6)
    assert env["jobs"].handler({"source": "sweep"}, None) == {"open": 1, "stuck": 1, "failed": 0}
    assert ("PrintStuckJobs", 1) in env["metrics"]
