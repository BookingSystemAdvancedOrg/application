"""Fixtures for the phone agent tests (feature L): moto tables as Terraform
creates them, two tenants, the three simulators and a fake Lambda invoker
that answers the existing functions' routes with the shapes those Lambdas
return (get-availability guest and staff views, the active layout, the manual
reservation route, the reservation list, mark-arrived, get-menu,
tenant-site-config). Not a test module itself (no tests here)."""

import importlib.util
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo
from shared import events
from shared import tenant as shared_tenant
from shared.agent_model import SimulatedAgentModel
from shared.features import phone_agent as pa
from shared.features.phone_agent import session
from shared.telephony import SIM_BASIC_AUTH, SimulatedTelephony
from shared.voice_provider import SimulatedVoiceProvider
from tenant_support import (LOC_A, LOC_A2, LOC_B, REGION, STAFF_A_SUB, TENANT_A, TENANT_B, TENANT_TABLE,
                            USER_TABLE_DEFAULT, features_on, seed_tenancy, tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
TABLE, BUCKET = "test-phone-agent", "test-phone-agent-recordings"
TODAY = date(2026, 11, 2)                                   # a Monday
NOW = datetime(2026, 11, 2, 19, 0, tzinfo=timezone.utc)    # 20:00 in Stockholm
CALLER = "+46701112233"
OWNER_A, OWNER_B, STAFF_A = (TENANT_A, "owner_user"), (TENANT_B, "owner_user"), (TENANT_A, "staff_user")
ALWAYS = {"enabled": True, "mode": "always", "agentHours": {}, "transferTo": "+46812345678",
          "maxCallSeconds": 480, "version": 0}


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def load(name):
    spec = importlib.util.spec_from_file_location(f"pa_{name.replace('-', '_')}", ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def table_el(table_id, seats, x, z, *, width=1.0, depth=1.0, floor="f1"):
    return {"elementId": table_id, "type": "table", "x": x, "z": z, "width": width, "depth": depth, "rotationY": 0,
            "seats": seats, "floorId": floor}


# Floor plan: t2a and t2b (2 seats each) 1.0 m apart edge to edge; t2c 1.5 m from t2b; t6 is a
# 6-seat table online only for 5-6; t4p is a phone-only 4-seat table.
LAYOUT = {"elements": [table_el("t2a", 2, 0.0, 0.0), table_el("t2b", 2, 2.0, 0.0), table_el("t2c", 2, 4.5, 0.0),
                       table_el("t6", 6, 0.0, 5.0, width=2.0), table_el("t4p", 4, 3.0, 5.0)]}


class Availability:
    """Per day: the free start times for a party of four (online options)
    and the free tables per time (staff view)."""

    def __init__(self):
        self.days = {}

    def set(self, day, times, *, staff=None, unavailable=None):
        self.days[day] = {"times": list(times), "staff": staff or {}, "unavailable": unavailable or {}}

    def guest(self, day, party):
        d = self.days.get(day) or {"times": [], "staff": {}, "unavailable": {}}
        slots = [{"startTime": t, "tables": [{"tableId": "t4", "seats": 4}],
                  "options": [{"tableIds": ["t4"], "seats": 4}]} for t in d["times"]]
        for t, why in d["unavailable"].items():
            slot = next((s for s in slots if s["startTime"] == t), None)
            if slot is None:
                slot = {"startTime": t, "tables": [], "options": []}
                slots.append(slot)
            slot["unavailable"] = [{"tableId": k, "reason": v} for k, v in why.items()]
        return {"date": day, "slots": [s for s in slots if s["options"] or s.get("unavailable")]}

    def staff(self, day):
        d = self.days.get(day) or {"staff": {}}
        return {"date": day, "slots": [{"startTime": t, "tables": [{"tableId": k, "seats": v} for k, v in tabs.items()]}
                                       for t, tabs in d["staff"].items()]}


class FakeInvoker:
    """The existing Lambdas as the agent sees them (no real handler)."""

    def __init__(self, availability):
        self.availability = availability
        self.layout = LAYOUT
        self.calls = []
        self.reservations = {}           # reservationId -> booking (made through the manual route)
        self.taken = set()               # (date, time) the manual route refuses (taken since the proposal)
        self.listed = {}                 # date -> reservations list (get-reservation)
        self.menu = {"items": [{"name": "Vegetarisk lasagne", "price": 189}, {"name": "Entrecôte", "price": 295}]}
        self.menus = {}                  # locationId -> menu (multi-location tests), else `menu`
        self.site = {"locations": [{"locationId": LOC_A, "name": "Bistro A", "address": "Storgatan 1"}]}

    def __call__(self, fn, event):
        self.calls.append((fn, event["routeKey"], event))
        route, q = event["routeKey"], event.get("queryStringParameters") or {}
        claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims") or {}
        if fn == "get-availability":
            day = q["date"]
            return 200, (self.availability.guest(day, int(q["partySize"])) if "partySize" in q
                         else self.availability.staff(day))
        if fn == "list-layout-version":
            return (200, self.layout) if self.layout else (404, {"error": "not found"})
        if fn == "create-pending-reservation":
            assert route == "POST /locations/{locationId}/reservations/manual"
            assert claims["role"] == "staff_user" and claims["sub"].startswith("agent:")
            body = json.loads(event["body"])
            if (body["date"], body["startTime"]) in self.taken:
                return 409, {"error": "table taken"}
            rid = f"r{len(self.reservations) + 1}"
            self.reservations[rid] = body
            return 201, {"reservationId": rid}
        if fn == "get-reservation":
            return 200, {"reservations": self.listed.get(q["date"], [])}
        if fn == "mark-arrived":
            return 200, {"ok": True}
        if fn == "get-menu":
            return 200, self.menus.get((event.get("pathParameters") or {}).get("locationId"), self.menu)
        if fn == "tenant-site-config":
            return 200, self.site
        return 404, {"error": "not found"}


def phone_table(ddb):
    ddb.create_table(
        TableName=TABLE, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                              for n in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")],
        GlobalSecondaryIndexes=[
            {"IndexName": "byNumber", "Projection": {"ProjectionType": "ALL"},
             "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                           {"AttributeName": "GSI1SK", "KeyType": "RANGE"}]},
            {"IndexName": "byReservation", "Projection": {"ProjectionType": "ALL"},
             "KeySchema": [{"AttributeName": "GSI2PK", "KeyType": "HASH"},
                           {"AttributeName": "GSI2SK", "KeyType": "RANGE"}]}])


@pytest.fixture
def env(monkeypatch):
    for key, value in {"ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                       "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION, "PHONE_AGENT_TABLE_NAME": TABLE,
                       "USER_TABLE_NAME": USER_TABLE_DEFAULT, "RECORDINGS_BUCKET": BUCKET,
                       "NOTIFICATION_FUNCTION_NAME": "dev-notification:live", "NUMBER_INDEX_NAME": "byNumber",
                       "RESERVATION_INDEX_NAME": "byReservation"}.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        on = features_on("phoneAgent")
        seed_tenancy(monkeypatch, tenant_a=tenant_row(TENANT_A, features=on, limits={pa.LIMIT_KEY: 300}),
                     tenant_b=tenant_row(TENANT_B, features=on, limits={pa.LIMIT_KEY: 300}))
        phone_table(boto3.client("dynamodb", region_name=REGION))
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        clock = Clock(NOW)
        monkeypatch.setattr(pa, "now", clock)
        monkeypatch.setattr(session, "local_today", lambda location: TODAY)
        published = []
        monkeypatch.setattr(events, "publish", lambda f, t, d: published.append((t, d)) or "id")
        api, live = load("phone-agent"), load("phone-agent-call")
        availability = Availability()
        invoke = FakeInvoker(availability)
        voice, tel = SimulatedVoiceProvider(), SimulatedTelephony()
        model = SimulatedAgentModel(today=TODAY)
        lam = FakeLambda()
        for module in (api, live):
            module._clients.update({"voice": voice, "telephony": tel, "model": model, "invoke": invoke,
                                    "lambda": lam})
        yield SimpleNamespace(api=api, live=live, clock=clock, published=published, voice=voice, tel=tel,
                              model=model, invoke=invoke, availability=availability, lam=lam,
                              res=boto3.resource("dynamodb", region_name=REGION))
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(json.loads(kwargs["Payload"]))
        return {"StatusCode": 202}


def call(e, method, template, *, as_=OWNER_A, location=LOC_A, body=None, query=None, **params):
    path = {"locationId": location, **params}
    event = {"routeKey": f"{method} {template}", "pathParameters": path, "queryStringParameters": query,
             "requestContext": {"http": {"method": method}}, "body": json.dumps(body) if body is not None else None}
    tenant_id, role = as_
    with_tenant_claims(event, tenant_id, role, STAFF_A_SUB if role == "staff_user" else None)
    response = e.api.handler(event, None)
    return response["statusCode"], (json.loads(response["body"]) if response.get("body") else None)


L = "/locations/{locationId}/phone-agent"


def enable(e, location=LOC_A, as_=OWNER_A, **over):
    status, body = call(e, "PUT", L, as_=as_, location=location, body={**ALWAYS, **over})
    assert status == 200, body
    return body


def set_feature(e, tenant_id, on):
    e.res.Table(TENANT_TABLE).update_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"},
                                          UpdateExpression="SET entitlements.features.phoneAgent = :v",
                                          ExpressionAttributeValues={":v": on})
    shared_tenant.reset_caches()


def webhook(e, kind, events_, *, auth=True):
    if kind == "voice":
        headers = {"X-Sim-Signature": "ok"} if auth else {}
        route = "POST /public/phone-agent/voice/webhook"
    else:
        headers = {"Authorization": SIM_BASIC_AUTH} if auth else {}
        route = "POST /public/phone-agent/telephony/webhook"
    event = {"routeKey": route, "headers": headers, "requestContext": {"http": {"method": "POST"}},
             "body": json.dumps({"events": events_})}
    response = e.live.handler(event, None)
    return response["statusCode"], json.loads(response["body"] or "{}")


__all__ = ["ALWAYS", "CALLER", "L", "LAYOUT", "LOC_A", "LOC_A2", "LOC_B", "NOW", "OWNER_A", "OWNER_B", "STAFF_A",
           "TENANT_A", "TENANT_B", "TODAY", "call", "enable", "env", "set_feature", "webhook"]
