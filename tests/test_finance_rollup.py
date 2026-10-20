"""finance-rollup: pulls cost, counts usage, allocates cost over the
customers active that day and writes the day rows. DynamoDB is moto; Cost
Explorer, Logs Insights, SNS and Lambda are fakes."""

import importlib.util
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo

APP = Path(__file__).parents[1] / "functions" / "finance-rollup" / "app.py"
REGION = "eu-north-1"
NOW = datetime(2026, 10, 20, 4, 30, tzinfo=timezone.utc)
A, B, GONE, NEW = "01aaaaaaaaaaaaaaaaaaaaaaaa", "01bbbbbbbbbbbbbbbbbbbbbbbb", "01gggggggggggggggggggggggg", "01nnnnnnnnnnnnnnnnnnnnnnnn"
LOC_A, LOC_B = "loc-a", "loc-b"
ALLOCATION = {"baseline": ["Amazon Route 53", "Tax"], "email": ["Amazon Simple Email Service"],
              "sms": ["Amazon Simple Notification Service"]}


def _table(res, name, gsis=()):
    attrs = [{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}]
    indexes = []
    for index, hash_key, range_key in gsis:
        attrs += [{"AttributeName": hash_key, "AttributeType": "S"}, {"AttributeName": range_key, "AttributeType": "S"}]
        indexes.append({"IndexName": index, "KeySchema": [{"AttributeName": hash_key, "KeyType": "HASH"},
                                                           {"AttributeName": range_key, "KeyType": "RANGE"}],
                        "Projection": {"ProjectionType": "ALL"}})
    res.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                     AttributeDefinitions=attrs, **({"GlobalSecondaryIndexes": indexes} if indexes else {}))
    return res.Table(name)


class FakeCE:
    def __init__(self, by_day=None, error=None):
        self.by_day, self.error, self.calls = by_day or {}, error, []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        results = []
        for day, services in self.by_day.items():
            results.append({"TimePeriod": {"Start": day}, "Groups": [
                {"Keys": [s], "Metrics": {"UnblendedCost": {"Amount": str(v), "Unit": "USD"}}} for s, v in services.items()]})
        return {"ResultsByTime": results}


class FakeLogs:
    def __init__(self, counts=None, missing=()):
        self.counts, self.missing, self.started = counts or {}, set(missing), []

    def start_query(self, **kwargs):
        groups = kwargs["logGroupNames"]
        self.started.append(groups)
        if any(g in self.missing for g in groups):
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "no group"}}, "StartQuery")
        return {"queryId": "q-" + str(len(self.started))}

    def get_query_results(self, queryId):
        rows = []
        for key, n in self.counts.items():
            tenant_id, loc = key if isinstance(key, tuple) else (key, "-")
            rows.append([{"field": "tenant", "value": tenant_id}, {"field": "loc", "value": loc}, {"field": "n", "value": str(n)}])
        return {"status": "Complete", "results": rows}

    def stop_query(self, queryId):
        return {}


class FakeClient:
    def __init__(self):
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)

    def invoke(self, **kwargs):
        self.calls.append(kwargs)


class Context:
    invoked_function_arn = "arn:aws:lambda:eu-north-1:123456789012:function:dev-finance-rollup:live"

    def __init__(self, remaining=(600_000,)):
        self.remaining = list(remaining)

    def get_remaining_time_in_millis(self):
        return self.remaining.pop(0) if len(self.remaining) > 1 else self.remaining[0]


@pytest.fixture
def env(monkeypatch):
    values = {"ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_SESSION_TOKEN": "t",
              "AWS_DEFAULT_REGION": REGION, "FINANCE_TABLE_NAME": "test-finance", "TENANT_TABLE_NAME": "test-tenant",
              "TENANT_LIST_INDEX_NAME": "GSI1", "LOCATION_TABLE_NAME": "test-location",
              "RESERVATION_TABLE_NAME": "test-reservation", "ORDER_TABLE_NAME": "test-order",
              "LAMBDA_LOG_GROUPS": json.dumps(["/aws/lambda/dev-a", "/aws/lambda/dev-b"]),
              "COST_DAYS": "3", "USAGE_DAYS": "2", "USD_TO_SEK": "10", "COST_ALLOCATION": json.dumps(ALLOCATION),
              "ALERT_TOPIC_ARN": "arn:aws:sns:eu-north-1:123456789012:alerts"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        res = boto3.resource("dynamodb", region_name=REGION)
        tables = {"finance": _table(res, "test-finance"), "tenant": _table(res, "test-tenant", [("GSI1", "GSI1PK", "GSI1SK")]),
                  "location": _table(res, "test-location"), "reservation": _table(res, "test-reservation"),
                  "order": _table(res, "test-order")}
        for tenant_id, extra in ((A, {"createdAt": "2026-09-01T10:00:00Z", "planId": "growth",
                                      "billing": {"status": "active", "mrrSek": Decimal("1990")}}),
                                 (B, {"createdAt": "2026-10-01T10:00:00Z", "planId": "starter", "status": "suspended",
                                      "billing": {"status": "past_due", "mrrSek": Decimal("990")}}),
                                 (GONE, {"createdAt": "2026-01-01T10:00:00Z", "status": "offboarded",
                                         "offboardedAt": "2026-10-18T08:00:00Z"}),
                                 (NEW, {"createdAt": "2026-10-19T12:00:00Z", "status": "provisioning"})):
            tables["tenant"].put_item(Item={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE", "GSI1PK": "TENANT",
                                            "GSI1SK": tenant_id, "tenantId": tenant_id, "status": "active",
                                            "name": tenant_id[:4], **extra})
        for tenant_id, loc in ((A, LOC_A), (B, LOC_B)):
            tables["location"].put_item(Item={"PK": f"TENANT#{tenant_id}", "SK": f"LOCATION#{loc}", "locationId": loc})
        # A: 3 bookings on the 18th (one hold excluded), 2 orders (one unpaid excluded)
        for i, status in enumerate(("reserved", "arrived", "held", "cancelled_no_charge")):
            tables["reservation"].put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": f"RESERVATION#2026-10-18#r{i}", "status": status})
        for i, status in enumerate(("confirmed", "completed", "pending_payment")):
            tables["order"].put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": f"ORDER#2026-10-18#o{i}", "status": status})
        tables["reservation"].put_item(Item={"PK": f"LOCATION#{LOC_B}", "SK": "RESERVATION#2026-10-19#r9", "status": "reserved"})
        # counters the senders wrote
        tables["finance"].put_item(Item={"PK": f"TENANT#{A}", "SK": "DAY#2026-10-18", "emails": 30, "sms": 0,
                                         "invoicedSek": Decimal("1990")})
        tables["finance"].put_item(Item={"PK": f"TENANT#{B}", "SK": "DAY#2026-10-18", "emails": 10, "sms": 5})
        tables["finance"].put_item(Item={"PK": f"TENANT#{A}", "SK": f"LOC#{LOC_A}#DAY#2026-10-18", "emails": 30})

        spec = importlib.util.spec_from_file_location("finance_rollup_app", APP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "now_utc", lambda: NOW)
        fakes = {"ce": FakeCE({"2026-10-18": {"AWS Lambda": "6", "Amazon Route 53": "2", "Amazon Simple Email Service": "1",
                                              "Amazon Simple Notification Service": "0.5"},
                               "2026-10-19": {"AWS Lambda": "1"}}),
                 "logs": FakeLogs({(A, LOC_A): 240, (A, "-"): 60, (B, LOC_B): 100, ("stray", "-"): 7}),
                 "sns": FakeClient(), "lambda": FakeClient()}
        module._clients[("ce", "us-east-1")] = fakes["ce"]
        module._clients[("logs", None)] = fakes["logs"]
        module._clients[("sns", None)] = fakes["sns"]
        module._clients[("lambda", None)] = fakes["lambda"]
        monkeypatch.setattr(module.time, "sleep", lambda s: None)
        yield {"app": module, "t": tables, **fakes}
        shared_dynamo._resource = None
        shared_dynamo._client = None


def _row(env, pk, sk):
    return env["t"]["finance"].get_item(Key={"PK": pk, "SK": sk}).get("Item")


def test_nightly_run_allocates_cost_and_counts_usage(env):
    out = env["app"].handler({}, Context())
    assert out["days"] == ["2026-10-18", "2026-10-19"] and out["warnings"] == []
    # Cost Explorer: last 3 days, daily, by service, credits and refunds out
    call = env["ce"].calls[0]
    assert call["TimePeriod"] == {"Start": "2026-10-17", "End": "2026-10-20"} and call["Granularity"] == "DAILY"
    cost = _row(env, "PLATFORM", "COST#2026-10-18")
    assert cost["usd"] == Decimal("9.5") and cost["sek"] == Decimal("95.00") and cost["byServiceUsd"]["AWS Lambda"] == Decimal("6")
    assert _row(env, "PLATFORM", "COST#2026-10-17")["usd"] == Decimal("0")

    a = _row(env, f"TENANT#{A}", "DAY#2026-10-18")
    b = _row(env, f"TENANT#{B}", "DAY#2026-10-18")
    # usage
    assert a["requests"] == 300 and a["bookings"] == 3 and a["orders"] == 2 and a["emails"] == 30
    assert b["requests"] == 100 and b["bookings"] == 0 and b["orders"] == 0 and b["sms"] == 5
    # allocation in SEK: Lambda 60 by requests (45 / 15), Route 53 20 equally (10 / 10),
    # SES 10 by emails (7.5 / 2.5), SNS 5 by sms (0 / 5)
    assert a["costSplitSek"] == {"requests": Decimal("45"), "baseline": Decimal("10"), "email": Decimal("7.5"), "sms": Decimal("0")}
    assert b["costSplitSek"] == {"requests": Decimal("15"), "baseline": Decimal("10"), "email": Decimal("2.5"), "sms": Decimal("5")}
    assert a["costSek"] == Decimal("62.50") and b["costSek"] == Decimal("32.50")
    assert a["mrrSek"] == Decimal("1990") and b["mrrSek"] == Decimal("990") and a["planId"] == "growth"
    # per location: A has one location that took 240 of its 300 requests and all its bookings; the
    # whole tenant cost lands on it (only location); the senders' counter on the row is kept
    loc = _row(env, f"TENANT#{A}", f"LOC#{LOC_A}#DAY#2026-10-18")
    assert loc["requests"] == 240 and loc["bookings"] == 3 and loc["orders"] == 2 and loc["emails"] == 30
    assert loc["costSek"] == Decimal("62.50") and loc["locationId"] == LOC_A
    assert a["invoicedSek"] == Decimal("1990")  # untouched revenue field
    # the offboarded and the provisioning tenants are not customers on the 18th
    assert _row(env, f"TENANT#{GONE}", "DAY#2026-10-18") is None
    assert _row(env, f"TENANT#{NEW}", "DAY#2026-10-18") is None

    platform = _row(env, "PLATFORM", "DAY#2026-10-18")
    assert platform["activeTenants"] == 2 and platform["costSek"] == Decimal("95.00")
    assert platform["mrrSek"] == Decimal("2980") and platform["requests"] == 400 and platform["bookings"] == 3
    assert platform["invoicedSek"] == Decimal("1990") and platform["emails"] == 40
    job = _row(env, "PLATFORM", "JOB#latest")
    assert job["done"] == ["2026-10-18", "2026-10-19"] and job["warnings"] == []
    assert env["sns"].calls == []


def test_offboarded_tenant_counts_until_its_last_day(env):
    # GONE was offboarded on the 18th: a customer on the 17th, gone on the 18th
    env["app"].handler({"from": "2026-10-17", "to": "2026-10-19"}, Context())
    gone = _row(env, f"TENANT#{GONE}", "DAY#2026-10-17")
    assert gone is not None and gone["active"] is True and gone["mrrSek"] == Decimal("0")
    assert _row(env, f"TENANT#{GONE}", "DAY#2026-10-18") is None
    assert _row(env, "PLATFORM", "DAY#2026-10-17")["activeTenants"] == 3
    assert _row(env, "PLATFORM", "DAY#2026-10-18")["churnedTenants"] == 1
    # NEW was created on the 19th but is still provisioning: counted as new, not as a customer
    platform = _row(env, "PLATFORM", "DAY#2026-10-19")
    assert platform["newTenants"] == 1 and platform["activeTenants"] == 2 and platform["churnedTenants"] == 0


def test_cost_explorer_failure_is_a_warning_and_uses_stored_cost(env):
    env["t"]["finance"].put_item(Item={"PK": "PLATFORM", "SK": "COST#2026-10-19", "usdToSek": Decimal("9"),
                                       "byServiceUsd": {"AWS Lambda": Decimal("2")}})
    from botocore.exceptions import ClientError
    env["ce"].error = ClientError({"Error": {"Code": "AccessDeniedException",
                                             "Message": "User not enabled for cost explorer access"}}, "GetCostAndUsage")
    out = env["app"].handler({"from": "2026-10-19", "to": "2026-10-19"}, Context())
    assert any("Cost Explorer" in w for w in out["warnings"])
    platform = _row(env, "PLATFORM", "DAY#2026-10-19")
    assert platform["costSek"] == Decimal("18.00")  # stored 2 USD at the stored rate 9
    assert env["sns"].calls and "warning" in env["sns"].calls[0]["Subject"]


def test_missing_log_group_is_skipped(env):
    env["logs"].missing = {"/aws/lambda/dev-b"}
    out = env["app"].handler({"from": "2026-10-18", "to": "2026-10-18"}, Context())
    assert out["warnings"] == []
    assert _row(env, f"TENANT#{A}", "DAY#2026-10-18")["requests"] == 300
    assert env["logs"].started[-2:] == [["/aws/lambda/dev-a"], ["/aws/lambda/dev-b"]]


def test_range_is_capped_at_yesterday_and_continues_near_timeout(env):
    out = env["app"].handler({"from": "2026-10-17", "to": "2026-10-25"}, Context([10_000]))
    assert out["days"] == ["2026-10-17"] and out["handedOver"] == ["2026-10-18", "2026-10-19"]
    payload = json.loads(env["lambda"].calls[0]["Payload"])
    assert payload == {"from": "2026-10-18", "to": "2026-10-19", "round": 1}
    assert env["lambda"].calls[0]["InvocationType"] == "Event"
    assert env["sns"].calls == []  # the continuation reports, not this run


def test_no_customers_leaves_cost_unallocated(env):
    for tenant_id in (A, B, GONE, NEW):
        env["t"]["tenant"].delete_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"})
    env["app"].handler({"from": "2026-10-18", "to": "2026-10-18"}, Context())
    platform = _row(env, "PLATFORM", "DAY#2026-10-18")
    assert platform["activeTenants"] == 0 and platform["unallocatedSek"] == Decimal("95.00")


def test_cost_split_by_location(env):
    app = env["app"]
    split = app.split_by_location(Decimal("100"), {"x": {"requests": 30}, "y": {"requests": 10}})
    assert split == {"x": Decimal("75.0000"), "y": Decimal("25.0000")}
    split = app.split_by_location(Decimal("100"), {"x": {"requests": 0, "bookings": 1, "orders": 1}, "y": {"requests": 0, "bookings": 2}})
    assert split == {"x": Decimal("50.0000"), "y": Decimal("50.0000")}
    split = app.split_by_location(Decimal("90"), {"x": {}, "y": {}, "z": {}})
    assert split["x"] == Decimal("30.0000")
    assert app.split_by_location(Decimal("90"), {}) == {}


def test_allocation_falls_back_to_equal_split_without_usage(env):
    app = env["app"]
    totals = app.bucket_totals({"AWS Lambda": Decimal("1"), "Tax": Decimal("1")}, Decimal(10), app._allocation())
    assert totals == {"baseline": Decimal("10"), "requests": Decimal("10"), "email": Decimal("0"), "sms": Decimal("0")}
    split = app.allocate(totals, {A: {"requests": 0}, B: {"requests": 0}})
    assert split[A]["total"] == Decimal("10.00") and split[B]["requests"] == Decimal("5")
