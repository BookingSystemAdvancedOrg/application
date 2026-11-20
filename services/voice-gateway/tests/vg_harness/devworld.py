"""The real dev restaurant for the live replay (VG_WORLD=dev). Owner: builder 'harness'.

Same namespace as world.fake_world, but nothing is faked except the three things that must not reach a person:
tables are the real dev tables (profile booqy-dev), the tool Lambdas are the real dev functions (get-availability,
create-pending-reservation, ...), the number +46100000001 resolves the real "Restaurant 1" tenant through the byNumber
index exactly as the phone-agent-call Lambda does. Not real: the notification Lambda (SMS notices are recorded, never
sent), the event bus (recorded) and the telephony and voice providers (the gateway is the line).

Everything this writes in dev is listed by the cleanup script (a before/after snapshot of the small tables) and deleted
right after a run. Credentials stay in this process (frozen environment variables), never printed.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

DIALED = "+46100000001"
REGION = "eu-north-1"
# the non-secret configuration of the deployed dev phone-agent-call Lambda
DEV_ENV = {
    "ENVIRONMENT": "dev", "AWS_DEFAULT_REGION": REGION, "AWS_REGION": REGION,
    "PHONE_AGENT_TABLE_NAME": "dev-phone-agent", "USER_TABLE_NAME": "dev-user", "LOCATION_TABLE_NAME": "dev-location",
    "TENANT_TABLE_NAME": "dev-tenant", "NUMBER_INDEX_NAME": "byNumber", "RESERVATION_INDEX_NAME": "byReservation",
    "USER_TENANT_INDEX_NAME": "byTenant", "LOCATION_ID_INDEX_NAME": "byLocationId", "EVENT_BUS_NAME": "dev-booqy-events",
    "NOTIFICATION_FUNCTION_NAME": "arn:aws:lambda:eu-north-1:877653167825:function:dev-notification:live",
    "RECORDINGS_BUCKET": "dev-phone-agent-recordings",
}


class _RecordingInvoker:
    """The real Lambda invoker, remembering the reservations the manual route made (what TimedInvoker reads)."""

    def __init__(self, inner):
        self.inner = inner
        self.reservations: dict = {}

    def __call__(self, fn, event):
        status, body = self.inner(fn, event)
        if fn == "create-pending-reservation" and status == 201 and isinstance(body, dict):
            try:
                request = json.loads(event.get("body") or "{}")
            except ValueError:
                request = {}
            self.reservations[body.get("reservationId") or f"r{len(self.reservations) + 1}"] = request
        return status, body


class _GuardedLambda:
    """The notification Lambda is never invoked (no SMS leaves dev); every other Lambda is the real one."""

    def __init__(self, real):
        self.real, self.calls = real, []

    def invoke(self, **kwargs):
        if "notification" in str(kwargs.get("FunctionName", "")):
            self.calls.append(json.loads(kwargs["Payload"]))
            return {"StatusCode": 202}
        return self.real.invoke(**kwargs)


@contextmanager
def dev_world(tenant: dict | None = None, *, now_ms=lambda: 0, glosses=None, languages=None, real_credentials=None):
    import boto3
    from _pytest.monkeypatch import MonkeyPatch

    import test_phone_agent_support as sup
    from shared import dynamo as shared_dynamo
    from shared import events
    from shared import tenant as shared_tenant
    from shared.features.phone_agent.tools import lambda_invoker
    from vg_harness.world import TimedInvoker
    from voice_gateway.brain.voice_port import GatewayVoice

    assert real_credentials, "the dev world needs the booqy-dev credentials (run it in live mode)"
    with MonkeyPatch.context() as mp:
        for key, value in {**DEV_ENV, **real_credentials}.items():
            mp.setenv(key, value)
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()
        session = boto3.Session(region_name=REGION, aws_access_key_id=real_credentials["AWS_ACCESS_KEY_ID"],
                                aws_secret_access_key=real_credentials["AWS_SECRET_ACCESS_KEY"],
                                aws_session_token=real_credentials["AWS_SESSION_TOKEN"] or None)
        published = []
        mp.setattr(events, "publish", lambda f, t, d: published.append((t, d)))
        live = sup.load("phone-agent-call")
        lam = _GuardedLambda(session.client("lambda"))
        invoker = _RecordingInvoker(lambda_invoker(lam))
        invoke = TimedInvoker(invoker, now_ms)
        live._clients.update({"voice": GatewayVoice(), "telephony": sup.SimulatedTelephony(), "invoke": invoke,
                              "lambda": lam})
        mp.setattr(live, "_endpoint_key", lambda: "dev-world-key")
        res = session.resource("dynamodb")
        table = res.Table(DEV_ENV["PHONE_AGENT_TABLE_NAME"])
        tenant_id = (live.by_number(DIALED) or (None,))[0]
        assert tenant_id, f"{DIALED} does not resolve a tenant in dev"

        def _callbacks():
            from boto3.dynamodb.conditions import Key
            items = table.query(KeyConditionExpression=Key("PK").eq(f"TENANT#{tenant_id}")
                                & Key("SK").begins_with("CALLBACK#")).get("Items") or []
            return [dict(i) for i in items]

        before = {c["SK"] for c in _callbacks()}

        def callbacks():
            return [c for c in _callbacks() if c["SK"] not in before]

        yield SimpleNamespace(api=None, live=live, invoke=invoke, lam=lam, model=None, res=res, number=DIALED,
                              published=published, callbacks=callbacks, availability=None, clock=None,
                              caller="+46700000000", tenant_id=tenant_id)
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()
