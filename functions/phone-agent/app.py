"""phone-agent

TRIGGER:
    API Gateway -- Auth: JWT (tenant pool), feature `phoneAgent`; staff are held to their location
    GET    /tenant/phone-agent                                               L1 owner
    PUT    /tenant/phone-agent/config                                        L2 owner
    GET    /locations/{locationId}/phone-agent                               L3 owner, staff
    PUT    /locations/{locationId}/phone-agent                               L4 owner (first enable provisions)
    POST   /locations/{locationId}/phone-agent/kill-switch                   L5 owner, staff
    POST   /locations/{locationId}/phone-agent/simulate                      L6 owner (dry run)
    GET    /locations/{locationId}/phone-agent/calls                         L7 owner, staff
    GET    /locations/{locationId}/phone-agent/calls/{callId}                L8 owner, staff
    DELETE /locations/{locationId}/phone-agent/calls/{callId}/recording      L9 owner
    GET    /locations/{locationId}/phone-agent/callbacks                     L10 owner, staff
    POST   /locations/{locationId}/phone-agent/callbacks/{callbackId}/close  L11 owner, staff
    GET    /locations/{locationId}/phone-agent/reservations/{reservationId}/call  L12 owner, staff
    POST   /locations/{locationId}/phone-agent/setup                         L19 owner (setup checklist)

    Lambda invoke (no requestContext): {"hook": ...} platform (L15-L18, Booqy Console),
    privacy.export, privacy.erase, tenant.purge, billing.usage

PURPOSE:
    Configuration, numbers, the calls list, transcripts, callbacks and the
    text simulator of the AI phone agent (ADR-L). Calls themselves run in
    phone-agent-call. Off by default (entitlement `phoneAgent`).

ENV_VARS:
    ENVIRONMENT, PHONE_AGENT_TABLE_NAME, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    USER_TABLE_NAME, EVENT_BUS_NAME, RECORDINGS_BUCKET, VOICE_CLIENT, TELEPHONY_CLIENT, AGENT_MODEL_CLIENT,
    PHONE_AGENT_MODEL_ID
"""

import base64
import binascii
import json
import logging
import os
from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import agent_model, dynamo, http, telephony, tenant, voice_provider
from shared.features import phone_agent as pa
from shared.features.phone_agent import calls, copy, session
from shared.features.phone_agent.tools import lambda_invoker

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_clients = {}
MAX_RANGE_DAYS = 31
PAGE = 50


def client(name):
    if name not in _clients:
        _clients[name] = {"voice": voice_provider.client, "telephony": telephony.client,
                          "model": agent_model.client}.get(name, lambda: boto3.client(name))()
    return _clients[name]


def invoker():
    if "invoke" not in _clients:
        _clients["invoke"] = lambda_invoker(client("lambda"))
    return _clients["invoke"]


def _err(status, code, field=None):
    body = {"error": code, "code": code}
    if field:
        body["field"] = field
    return http.respond(status, body)


def _owner(ctx):
    if not ctx.is_owner:
        raise pa.PhoneAgentError(403, "owner_only")


def _version(body):
    value = body.get("version", 0)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value != int(value):
        raise pa.PhoneAgentError(400, "version must be a whole number", "version")
    return int(value)


def _prefix():
    env = os.environ.get("ENVIRONMENT", "dev")
    return "" if env == "prod" else f"{env}-"


# --- L1, L2 ------------------------------------------------------------------------------

def get_tenant_view(event, ctx):
    _owner(ctx)
    config = pa.get_config(ctx.tenant_id)
    end = pa.period_end()
    use = pa.usage(ctx.tenant_id, end)
    per_loc = pa.plain(use.get("loc") or {})
    locations = []
    for loc_row in tenant.list_locations(ctx.tenant_id):
        lid = loc_row.get("locationId")
        loc = pa.get_loc(ctx.tenant_id, lid)
        locations.append({"locationId": lid, "name": loc_row.get("name") or "", "enabled": bool(loc.get("enabled")),
                          "killSwitch": bool(loc.get("killSwitch")), "mode": loc.get("mode") or "after_hours",
                          "number": (loc.get("number") or {}).get("e164"), "minutesThisPeriod": int(per_loc.get(lid)
                                                                                                    or 0)})
    return http.respond(200, {"config": pa.config_view(ctx.tenant, config), "locations": locations,
                              "usage": {"minutes": int(use.get("minutes") or 0),
                                        "cap": pa.tenant_cap(ctx.tenant, config),
                                        "includedMinutes": pa.included_minutes(ctx.tenant), "periodEnd": end}})


def put_config(event, ctx):
    _owner(ctx)
    body = http.body(event, allowed=("agentName", "tone", "languages", "recording", "minutesCapPerMonth", "version"))
    version = _version(body)
    clean = pa.validate_config(body, ceiling=pa.platform_settings()["maxMinutesCap"])
    current = pa.get_config(ctx.tenant_id)
    if all(pa.plain(current.get(k)) == v for k, v in clean.items()) and current:
        return http.respond(200, pa.config_view(ctx.tenant, current))
    if int(current.get("version") or 0) != version:
        raise pa.PhoneAgentError(409, "changed_retry")
    item = {**current, **pa.config_key(ctx.tenant_id), "tenantId": ctx.tenant_id, **clean, "version": version + 1,
            "updatedAt": pa.iso(pa.now()), "updatedBy": ctx.sub}
    cond = {"ConditionExpression": "attribute_not_exists(PK)"} if not current else {
        "ConditionExpression": "version = :v", "ExpressionAttributeValues": {":v": version}}
    try:
        pa.table().put_item(Item=item, **cond)
    except ClientError as exc:
        if pa.conditional_failed(exc):
            raise pa.PhoneAgentError(409, "changed_retry")
        raise
    return http.respond(200, pa.config_view(ctx.tenant, item))


# --- L3, L4, L5 ----------------------------------------------------------------------------

def _test_number(ctx):
    """The dev test line of the location (never looked up in prod)."""
    if os.environ.get("ENVIRONMENT") == "prod":
        return None
    return pa.test_line(ctx.tenant_id, ctx.location_id)


def _full_view(ctx, loc, *, owner=True):
    test_number = _test_number(ctx)
    return {**pa.loc_view(loc, owner=owner, test_number=test_number), "forwarding": pa.forwarding(loc, test_number)}


def get_location_view(event, ctx):
    loc = {**pa.get_loc(ctx.tenant_id, ctx.location_id), "locationId": ctx.location_id}
    return http.respond(200, _full_view(ctx, loc, owner=ctx.is_owner))


def post_setup(event, ctx):
    """L19: the setup checklist's progress (operator, forwarded, tested). Never
    provisions or calls a provider: recording a step can not fail on them."""
    _owner(ctx)
    body = http.body(event, allowed=("operator", "forwarded", "tested", "version"))
    version = _version(body)
    changes = pa.validate_setup(body)
    loc = pa.get_loc(ctx.tenant_id, ctx.location_id)
    if int(loc.get("version") or 0) != version:
        raise pa.PhoneAgentError(409, "changed_retry")
    setup = {**pa.setup_view(loc), **changes}
    if "operator" in changes and "forwarded" not in body and \
            pa.forwarding({**loc, "setup": setup})["codes"] != pa.forwarding(loc)["codes"]:
        # Another operator dials other codes: the checklist asks to forward again.
        setup = {**setup, "forwardedAt": None, "testedAt": None}
    setup = {k: v for k, v in setup.items() if v is not None}
    try:
        new = pa.table().update_item(
            Key=pa.loc_key(ctx.tenant_id, ctx.location_id),
            UpdateExpression="SET #s = :s, version = :nv, tenantId = :t, locationId = :l, updatedAt = :at",
            ConditionExpression="attribute_not_exists(version) OR version = :v",
            ExpressionAttributeNames={"#s": "setup"},
            ExpressionAttributeValues={":s": setup, ":nv": version + 1, ":v": version, ":t": ctx.tenant_id,
                                       ":l": ctx.location_id, ":at": pa.iso(pa.now())},
            ReturnValues="ALL_NEW")["Attributes"]
    except ClientError as exc:
        if pa.conditional_failed(exc):
            raise pa.PhoneAgentError(409, "changed_retry")
        raise
    return http.respond(200, _full_view(ctx, new))


def _agent_user(tenant_id, location_id, *, present):
    table_name = os.environ.get("USER_TABLE_NAME")
    if not table_name:
        return
    key = {"PK": f"USER#{pa.AGENT_USER_PREFIX}{location_id}", "SK": "PROFILE"}
    users = dynamo.table(table_name)
    if present:
        users.put_item(Item={**key, "tenantId": tenant_id, "role": "staff", "locationId": location_id,
                             "name": "Telefonassistent", "status": pa.AGENT_USER_STATUS})
    else:
        users.delete_item(Key=key)


def _share(ctx, clean):
    """A second location answering on another location's number (one number,
    several locations: the agent asks which location first). The number row
    lists every location on it; nothing is provisioned."""
    other_id = clean["shareNumberWith"]
    if other_id == ctx.location_id:
        raise pa.PhoneAgentError(400, "shareNumberWith must be another location", "shareNumberWith")
    other = pa.get_loc(ctx.tenant_id, other_id)  # same tenant only: the key carries the tenant
    number = pa.plain(other.get("number") or {})
    if not other.get("enabled") or not number.get("e164") or number.get("shared"):
        raise pa.PhoneAgentError(409, "share_unavailable", "shareNumberWith")
    key = pa.number_key(ctx.tenant_id, number["e164"])
    row = pa.table().get_item(Key=key, ConsistentRead=True).get("Item") or {}
    ids = list(row.get("locationIds") or [other_id])
    if ctx.location_id not in ids:
        pa.table().update_item(Key=key, UpdateExpression="SET locationIds = :ids",
                               ExpressionAttributeValues={":ids": ids + [ctx.location_id]})
    return {"e164": number["e164"], "providerId": number.get("providerId"), "shared": True,
            "ownerLocationId": other_id, "provisionedAt": pa.iso(pa.now())}, other.get("providerAgentId")


def _unshare(tenant_id, location_id, number):
    key = pa.number_key(tenant_id, number["e164"])
    row = pa.table().get_item(Key=key, ConsistentRead=True).get("Item") or {}
    ids = [x for x in row.get("locationIds") or [] if x != location_id]
    if row:
        pa.table().update_item(Key=key, UpdateExpression="SET locationIds = :ids",
                               ExpressionAttributeValues={":ids": ids})


def voice_config(ctx, config, clean):
    """What the voice provider's agent for the location is made of (ADR-L 3):
    the greeting with the disclosure, the languages, the tone, the length."""
    return {"name": f"{_prefix()}phone-agent-{ctx.location_id}",
            "greetingSv": copy.greeting("sv", config.get("agentName"), ctx.location.get("name"),
                                        clean.get("greetingExtraSv")),
            "greetingEn": copy.greeting("en", config.get("agentName"), ctx.location.get("name"),
                                        clean.get("greetingExtraEn")),
            "languages": list(config.get("languages") or pa.LANGUAGES),
            "tone": config.get("tone") or "friendly",
            "transferTo": clean.get("transferTo"),
            "maxDurationSeconds": clean["maxCallSeconds"]}


def _provision(ctx, loc, clean):
    """First enable: one Booqy number and one provider agent for the location
    (idempotent: an existing number is reused, the agent is idempotent by name)."""
    if clean.get("shareNumberWith"):
        return _share(ctx, clean)
    number = pa.plain(loc.get("number") or {})
    if number.get("shared"):  # stops sharing: the location gets a number of its own
        _unshare(ctx.tenant_id, ctx.location_id, number)
        number = {}
    if not number:
        try:
            got = client("telephony").provision_number("SE", f"{_prefix()}phone-agent-{ctx.location_id}")
        except telephony.TelephonyError as exc:
            raise pa.PhoneAgentError(409 if exc.code == "number_unavailable" else 502,
                                     "number_unavailable" if exc.code == "number_unavailable"
                                     else "telephony_unavailable")
        number = {"e164": got.e164, "providerId": got.provider_id, "provisionedAt": pa.iso(pa.now())}
        pa.table().put_item(Item={**pa.number_key(ctx.tenant_id, got.e164), "tenantId": ctx.tenant_id,
                                  "locationId": ctx.location_id, "locationIds": [ctx.location_id],
                                  "GSI1PK": f"NUMBER#{got.e164}", "GSI1SK": f"{ctx.tenant_id}#{ctx.location_id}"})
        pa.table().update_item(Key=pa.loc_key(ctx.tenant_id, ctx.location_id), UpdateExpression="SET #n = :n",
                               ExpressionAttributeNames={"#n": "number"}, ExpressionAttributeValues={":n": number})
        calls.publish("PhoneNumber.Provisioned", {"tenantId": ctx.tenant_id, "locationId": ctx.location_id})
    config = pa.get_config(ctx.tenant_id)
    wanted = voice_config(ctx, config, clean)
    own_agent = loc.get("providerAgentId") if not pa.plain(loc.get("number") or {}).get("shared") else None
    try:
        agent_id = own_agent or client("voice").create_agent(wanted)
        if own_agent:
            client("voice").update_agent(agent_id, wanted)
        sip = client("voice").sip_target(agent_id, number["e164"])
    except voice_provider.VoiceProviderError:
        raise pa.PhoneAgentError(502, "voice_provider_unavailable")
    # Never the restaurant's own number: it is forwarded to Booqy, so a call
    # would go round in a loop. No transfer number: the provider hangs up.
    transfer = clean.get("transferTo")
    fallback = transfer if transfer and not pa.same_phone(transfer, ctx.location.get("phoneNumber")) else None
    try:
        client("telephony").set_inbound(number["providerId"], sip, fallback)
    except telephony.TelephonyError:
        raise pa.PhoneAgentError(502, "telephony_unavailable")
    return number, agent_id


def put_location(event, ctx):
    _owner(ctx)
    body = http.body(event, allowed=("enabled", "mode", "agentHours", "greetingExtraSv", "greetingExtraEn",
                                     "transferTo", "maxCallSeconds", "counterOffers", "shareNumberWith",
                                     "version"))
    version = _version(body)
    clean = pa.validate_location(body, restaurant_phone=ctx.location.get("phoneNumber"))
    loc = pa.get_loc(ctx.tenant_id, ctx.location_id)
    if int(loc.get("version") or 0) != version:
        raise pa.PhoneAgentError(409, "changed_retry")
    extra = {}
    if clean["enabled"]:
        number, agent_id = _provision(ctx, loc, clean)
        extra = {"number": {**number, "releaseAfter": None}, "providerAgentId": agent_id}
        _agent_user(ctx.tenant_id, ctx.location_id, present=True)
    elif loc.get("enabled") and pa.plain(loc.get("number") or {}).get("shared"):
        _unshare(ctx.tenant_id, ctx.location_id, pa.plain(loc["number"]))  # the owner's number stays
        extra = {"number": None}
        _agent_user(ctx.tenant_id, ctx.location_id, present=False)
    elif loc.get("enabled") and loc.get("number"):
        number = pa.plain(loc["number"])
        sharing = pa.table().get_item(Key=pa.number_key(ctx.tenant_id, number["e164"]),
                                      ConsistentRead=True).get("Item") or {}
        others = [x for x in sharing.get("locationIds") or [] if x != ctx.location_id]
        # A number other locations answer on is kept for them, never released.
        extra = {"number": number if others else
                 {**number, "releaseAfter": pa.iso(pa.now() + timedelta(days=pa.NUMBER_GRACE_DAYS))}}
        _agent_user(ctx.tenant_id, ctx.location_id, present=False)
    before = pa.forwarding(loc)["codes"]
    loc = pa.get_loc(ctx.tenant_id, ctx.location_id)  # the number written during provisioning
    item = {**loc, **pa.loc_key(ctx.tenant_id, ctx.location_id), "tenantId": ctx.tenant_id,
            "locationId": ctx.location_id, **{k: v for k, v in clean.items() if k != "shareNumberWith"}, **extra,
            "killSwitch": bool(loc.get("killSwitch")),
            "version": version + 1, "updatedAt": pa.iso(pa.now())}
    if item.get("number") and item["number"].get("releaseAfter") is None:
        item["number"] = {k: v for k, v in item["number"].items() if k != "releaseAfter"}
    setup = pa.plain(item.get("setup") or {})
    if setup.get("forwardedAt") and before and pa.forwarding(item)["codes"] != before:
        # Another mode or number needs another code: the checklist asks again.
        item["setup"] = {k: v for k, v in setup.items() if k not in ("forwardedAt", "testedAt")}
    cond = {"ConditionExpression": "attribute_not_exists(version) OR version = :v",
            "ExpressionAttributeValues": {":v": version}}
    try:
        pa.table().put_item(Item={k: v for k, v in item.items() if v is not None}, **cond)
    except ClientError as exc:
        if pa.conditional_failed(exc):
            raise pa.PhoneAgentError(409, "changed_retry")
        raise
    return http.respond(200, _full_view(ctx, item))


def kill_switch(event, ctx):
    body = http.body(event, allowed=("on", "version"))
    if not isinstance(body.get("on"), bool):
        raise pa.PhoneAgentError(400, "on must be true or false", "on")
    version = _version(body)
    loc = pa.get_loc(ctx.tenant_id, ctx.location_id)
    if loc and bool(loc.get("killSwitch")) == body["on"]:
        return http.respond(200, pa.loc_view({**loc, "locationId": ctx.location_id}, owner=ctx.is_owner))
    if int(loc.get("version") or 0) != version:
        raise pa.PhoneAgentError(409, "changed_retry")
    try:
        new = pa.table().update_item(
            Key=pa.loc_key(ctx.tenant_id, ctx.location_id),
            UpdateExpression="SET killSwitch = :on, version = :nv, tenantId = :t, locationId = :l, "
                             "killSwitchBy = :by, killSwitchAt = :at",
            ConditionExpression="attribute_not_exists(version) OR version = :v",
            ExpressionAttributeValues={":on": body["on"], ":nv": version + 1, ":v": version, ":t": ctx.tenant_id,
                                       ":l": ctx.location_id, ":by": ctx.sub, ":at": pa.iso(pa.now())},
            ReturnValues="ALL_NEW")["Attributes"]
    except ClientError as exc:
        if pa.conditional_failed(exc):
            raise pa.PhoneAgentError(409, "changed_retry")
        raise
    return http.respond(200, pa.loc_view(new, owner=ctx.is_owner))


# --- L6 ------------------------------------------------------------------------------------

def simulate(event, ctx):
    _owner(ctx)
    body = http.body(event, allowed=("sessionId", "text", "language"))
    session_id = body.get("sessionId")
    if not pa.valid_id(session_id):
        raise pa.PhoneAgentError(400, "sessionId must be 6-64 letters or digits", "sessionId")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise pa.PhoneAgentError(400, "text is required", "text")
    if len(text) > pa.SIM_TEXT_MAX:
        raise pa.PhoneAgentError(400, "text_too_long", "text")
    language = body.get("language")
    if language is not None and language not in pa.LANGUAGES:
        raise pa.PhoneAgentError(400, "language must be one of " + ", ".join(pa.LANGUAGES), "language")
    loc = pa.get_loc(ctx.tenant_id, ctx.location_id)
    try:
        out = session.sim_turn(tenant_id=ctx.tenant_id, tenant_row=ctx.tenant,
                               location={**ctx.location, "locationId": ctx.location_id}, loc=loc,
                               config=pa.get_config(ctx.tenant_id), session_id=session_id, text=" ".join(text.split()),
                               language_=language, model=client("model"), invoke=invoker())
    except agent_model.AgentModelError:
        raise pa.PhoneAgentError(502, "ai_unavailable")
    return http.respond(200, out)


# --- L7-L9, L12 calls ------------------------------------------------------------------------

def _cursor(value):
    if not value:
        return None
    try:
        out = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise pa.PhoneAgentError(400, "invalid_range", "cursor")
    return out if isinstance(out, dict) else None


def list_calls(event, ctx):
    query = http.query(event)
    # Stockholm's day from the feature clock (UTC on a Lambda, frozen in tests).
    today = pa.now().astimezone(ZoneInfo("Europe/Stockholm")).date()
    try:
        first = date.fromisoformat(query.get("from") or (today - timedelta(days=6)).isoformat())
        last = date.fromisoformat(query.get("to") or today.isoformat())
    except ValueError:
        raise pa.PhoneAgentError(400, "invalid_range", "from")
    if first > last or (last - first).days >= MAX_RANGE_DAYS:
        raise pa.PhoneAgentError(400, "invalid_range", "from")
    args = {"KeyConditionExpression": Key("PK").eq(pa.tenant_pk(ctx.tenant_id)) &
            Key("SK").between(f"CALL#{first.isoformat()}", f"CALL#{last.isoformat()}~"),
            "ScanIndexForward": False, "Limit": 200}
    start = _cursor(query.get("cursor"))
    if start:
        args["ExclusiveStartKey"] = start
    out, next_key = [], None
    while True:
        page = pa.table().query(**args)
        for item in page.get("Items") or []:
            if "#T#" in item["SK"] or item.get("locationId") != ctx.location_id:
                continue
            out.append(pa.call_view(item, owner=ctx.is_owner))
            if len(out) >= PAGE:
                next_key = {"PK": item["PK"], "SK": item["SK"]}
                break
        if next_key or "LastEvaluatedKey" not in page:
            break
        args["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    cursor = base64.urlsafe_b64encode(json.dumps(next_key).encode()).decode() if next_key else None
    return http.respond(200, {"calls": [{k: c[k] for k in ("callId", "startedAt", "durationSec", "language",
                                                             "outcome", "counterOffer", "reservationId",
                                                             "callbackId", "summary", "refusedReason")}
                                        | ({"from": c.get("from")} if ctx.is_owner else {}) for c in out],
                              "cursor": cursor})


def _own_call(event, ctx):
    call = calls.get_call(ctx.tenant_id, http.path_id(event, "callId"))
    if not call or call.get("locationId") != ctx.location_id:
        raise pa.PhoneAgentError(404, "not found")
    return call


def get_call(event, ctx):
    call = _own_call(event, ctx)
    turns = pa.query_all(pa.tenant_pk(ctx.tenant_id), call["SK"] + "#T#", ConsistentRead=True)
    out = {**pa.call_view(call, owner=ctx.is_owner),
           "turns": [{"role": t.get("role"), "text": t.get("text") or "", "at": t.get("at"),
                      **({"toolName": t["toolName"], "toolOk": bool(t.get("toolOk"))} if t.get("toolName") else {})}
                     for t in turns]}
    recording = pa.plain(call.get("recording") or {})
    if ctx.is_owner and recording.get("key") and os.environ.get("RECORDINGS_BUCKET"):
        out["recordingUrl"] = client("s3").generate_presigned_url(
            "get_object", Params={"Bucket": os.environ["RECORDINGS_BUCKET"], "Key": recording["key"]}, ExpiresIn=300)
    return http.respond(200, out)


def delete_recording(event, ctx):
    _owner(ctx)
    call = _own_call(event, ctx)
    recording = pa.plain(call.get("recording") or {})
    if recording.get("key") and os.environ.get("RECORDINGS_BUCKET"):
        client("s3").delete_object(Bucket=os.environ["RECORDINGS_BUCKET"], Key=recording["key"])
    if call.get("recording") is not None:
        pa.table().update_item(Key={"PK": call["PK"], "SK": call["SK"]}, UpdateExpression="REMOVE recording")
    return {"statusCode": 204, "headers": {"Cache-Control": "no-store"}, "body": ""}


def reservation_call(event, ctx):
    reservation_id = http.path_id(event, "reservationId")
    items = pa.table().query(IndexName=os.environ.get("RESERVATION_INDEX_NAME", "byReservation"),
                             KeyConditionExpression=Key("GSI2PK").eq(
                                 f"{pa.tenant_pk(ctx.tenant_id)}#RES#{reservation_id}")).get("Items") or []
    items = [i for i in items if i.get("tenantId") == ctx.tenant_id and i.get("locationId") == ctx.location_id]
    if not items:
        raise pa.PhoneAgentError(404, "not found")
    call = sorted(items, key=lambda i: i.get("GSI2SK") or "")[-1]
    return http.respond(200, {"callId": call["callId"], "summary": call.get("summary") or "",
                              "disclosure": pa.plain(call.get("disclosure") or {}), "startedAt": call.get("startedAt")})


# --- L10, L11 callbacks ----------------------------------------------------------------------

def list_callbacks(event, ctx):
    status = http.query(event).get("status") or "open"
    if status not in ("open", "closed", "all"):
        raise pa.PhoneAgentError(400, "status must be open, closed or all", "status")
    items = [c for c in pa.query_all(pa.tenant_pk(ctx.tenant_id), "CALLBACK#", ConsistentRead=True)
             if c.get("locationId") == ctx.location_id and (status == "all" or c.get("status") == status)]
    return http.respond(200, {"callbacks": [pa.callback_view(c) for c in sorted(items, key=lambda c: c.get(
        "createdAt") or "", reverse=True)]})


def close_callback(event, ctx):
    body = http.body(event, allowed=("version", "note"))
    version = _version(body)
    note = body.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > pa.TEXT_MAX):
        raise pa.PhoneAgentError(400, "text_too_long", "note")
    callback_id = http.path_id(event, "callbackId")
    key = pa.callback_key(ctx.tenant_id, callback_id)
    current = pa.table().get_item(Key=key, ConsistentRead=True).get("Item")
    if not current or current.get("tenantId") != ctx.tenant_id or current.get("locationId") != ctx.location_id:
        raise pa.PhoneAgentError(404, "not found")
    if current.get("status") == "closed":
        return http.respond(200, pa.callback_view(current))
    if int(current.get("version") or 0) != version:
        raise pa.PhoneAgentError(409, "invalid_state")
    at = pa.now()
    try:
        new = pa.table().update_item(
            Key=key,
            UpdateExpression="SET #s = :c, closedAt = :at, closedBy = :by, note = :n, version = :nv, #t = :ttl",
            ConditionExpression="version = :v AND #s = :o", ExpressionAttributeNames={"#s": "status", "#t": "ttl"},
            ExpressionAttributeValues={":c": "closed", ":at": pa.iso(at), ":by": ctx.sub, ":n": " ".join(
                (note or "").split()), ":nv": version + 1, ":v": version, ":o": "open",
                ":ttl": int((at + timedelta(days=pa.CALLBACK_TTL_DAYS)).timestamp())},
            ReturnValues="ALL_NEW")["Attributes"]
    except ClientError as exc:
        if pa.conditional_failed(exc):
            raise pa.PhoneAgentError(409, "invalid_state")
        raise
    calls.publish("PhoneCallback.Closed", {"tenantId": ctx.tenant_id, "locationId": ctx.location_id,
                                           "callbackId": callback_id})
    return http.respond(200, pa.callback_view(new))


# --- hooks ---------------------------------------------------------------------------------

def _platform(event):
    op, tenant_id, body = event.get("op"), event.get("tenantId"), event.get("body") or {}
    if op == "phone-agent.settings.get":
        return {"ok": True, "status": 200, "body": pa.platform_settings()}
    if op == "phone-agent.settings.put":
        kill, cap = body.get("killSwitch"), body.get("maxMinutesCap")
        if not isinstance(kill, bool):
            return {"ok": False, "status": 400, "code": "invalid_kill_switch", "field": "killSwitch"}
        if isinstance(cap, bool) or not isinstance(cap, int) or not 0 <= cap <= pa.CAP_MAX:
            return {"ok": False, "status": 400, "code": "invalid_cap", "field": "maxMinutesCap"}
        pa.tenant_table().put_item(Item={**pa.PLATFORM_KEY, "killSwitch": kill, "maxMinutesCap": cap,
                                         "updatedAt": pa.iso(pa.now()), "updatedBy": (event.get("operator") or {}).get(
                                             "sub")})
        return {"ok": True, "status": 200, "body": {"killSwitch": kill, "maxMinutesCap": cap}}
    if op not in ("phone-agent.tenant.get", "phone-agent.tenant.pause"):
        return {"ok": False, "status": 400, "code": "unknown_op"}
    row = tenant.get_tenant(tenant_id, fresh=True) if isinstance(tenant_id, str) and tenant_id else None
    if not row or not tenant.ever_on(row, pa.FEATURE_KEY):
        return {"ok": False, "status": 404, "code": "not_found"}
    if op == "phone-agent.tenant.pause":
        paused = body.get("paused")
        if not isinstance(paused, bool):
            return {"ok": False, "status": 400, "code": "invalid_paused", "field": "paused"}
        pa.table().update_item(Key=pa.config_key(tenant_id), UpdateExpression="SET paused = :p, tenantId = :t",
                               ExpressionAttributeValues={":p": paused, ":t": tenant_id})
        return {"ok": True, "status": 200, "body": {"paused": paused}}
    end = pa.period_end()
    use = pa.usage(tenant_id, end)
    per_loc = pa.plain(use.get("loc") or {})
    # The locations set up for the agent, from its own table (LOC#<l>#CONFIG): this
    # role has no Query on the location table, and a tenant that never set up the
    # agent gets an empty list (not configured), not an error.
    configs = {row["SK"].removeprefix("LOC#").removesuffix("#CONFIG"): row
               for row in pa.query_all(pa.tenant_pk(tenant_id), "LOC#", ConsistentRead=True)
               if row["SK"].endswith("#CONFIG")}
    locations = []
    for lid in sorted(set(configs) | set(per_loc)):
        loc = configs.get(lid) or {}
        locations.append({"locationId": lid, "enabled": bool(loc.get("enabled")),
                          "number": (loc.get("number") or {}).get("e164"), "minutesThisPeriod": int(per_loc.get(lid)
                                                                                                    or 0)})
    history = [{"periodEnd": u["SK"].removeprefix("USAGE#"), "minutes": int(u.get("minutes") or 0)}
               for u in pa.query_all(pa.tenant_pk(tenant_id), "USAGE#", ConsistentRead=True)]
    return {"ok": True, "status": 200, "body": {"paused": bool(pa.get_config(tenant_id).get("paused")),
                                                "locations": locations, "usage": sorted(
                                                    history, key=lambda u: u["periodEnd"], reverse=True)[:12]}}


def _phone(event):
    raw = event.get("phone")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return pa.normalize_e164(raw)
    except pa.PhoneAgentError:
        return None


def _guest_rows(tenant_id, phone):
    wanted = pa.phone_hash(tenant_id, phone)
    # One query: the "CALL" prefix already covers CALL#... and CALLBACK#...
    # (a second CALLBACK# query would list every callback twice).
    rows = pa.query_all(pa.tenant_pk(tenant_id), "CALL", ConsistentRead=True)
    found_calls = [r for r in rows if r["SK"].startswith("CALL#") and "#T#" not in r["SK"]
                   and r.get("fromHash") == wanted]
    found_callbacks = [r for r in rows if r["SK"].startswith("CALLBACK#") and r.get("phoneHash") == wanted]
    return found_calls, found_callbacks


def privacy_export(tenant_id, phone):
    found_calls, found_callbacks = _guest_rows(tenant_id, phone)
    items = [{"kind": "phone_call", "locationId": c.get("locationId"), "startedAt": c.get("startedAt"),
              "outcome": c.get("outcome"), "summary": c.get("summary") or "",
              "turns": [t.get("text") for t in pa.query_all(pa.tenant_pk(tenant_id), c["SK"] + "#T#")]}
             for c in found_calls]
    items += [{"kind": "phone_callback", "locationId": c.get("locationId"), "topic": c.get("topic"),
               "createdAt": c.get("createdAt"), "status": c.get("status")} for c in found_callbacks]
    return {"items": items}


def privacy_erase(tenant_id, phone):
    """Removes the caller's number, name, summary, every turn's text (kept as
    "[raderad]") and the recording; keeps minutes and the disclosure evidence."""
    found_calls, found_callbacks = _guest_rows(tenant_id, phone)
    turns = 0
    for c in found_calls:
        recording = pa.plain(c.get("recording") or {})
        if recording.get("key") and os.environ.get("RECORDINGS_BUCKET"):
            client("s3").delete_object(Bucket=os.environ["RECORDINGS_BUCKET"], Key=recording["key"])
        pa.table().update_item(Key={"PK": c["PK"], "SK": c["SK"]},
                               UpdateExpression="REMOVE #f, fromHash, summary, recording, agentMessages, toolState",
                               ExpressionAttributeNames={"#f": "from"})
        for t in pa.query_all(pa.tenant_pk(tenant_id), c["SK"] + "#T#"):
            pa.table().update_item(Key={"PK": t["PK"], "SK": t["SK"]}, UpdateExpression="SET #x = :r",
                                   ExpressionAttributeNames={"#x": "text"},
                                   ExpressionAttributeValues={":r": "[raderad]"})
            turns += 1
    for cb in found_callbacks:
        pa.table().update_item(Key={"PK": cb["PK"], "SK": cb["SK"]},
                               UpdateExpression="REMOVE #n, phone, phoneHash, topic",
                               ExpressionAttributeNames={"#n": "name"})
    return {"erased": {"calls": len(found_calls), "turns": turns, "callbacks": len(found_callbacks)},
            "kept": {"callMinutes": len(found_calls)}}


def purge(tenant_id, location_ids):
    """Offboarding: numbers released, provider agents deleted, recordings
    deleted, the agent's system users removed, every row deleted."""
    rows = pa.query_all(pa.tenant_pk(tenant_id), "")
    for row in rows:
        if row["SK"].startswith("LOC#") and row["SK"].endswith("#CONFIG"):
            number = pa.plain(row.get("number") or {})
            if number.get("providerId"):
                try:
                    client("telephony").release_number(number["providerId"])
                except telephony.TelephonyError:
                    return {"done": False, "counts": {}}
            if row.get("providerAgentId"):
                try:
                    client("voice").delete_agent(row["providerAgentId"])
                except voice_provider.VoiceProviderError:
                    return {"done": False, "counts": {}}
    for location_id in {r.get("locationId") for r in rows if r.get("locationId")} | {
            x for x in location_ids if isinstance(x, str) and x}:
        _agent_user(tenant_id, location_id, present=False)
    bucket = os.environ.get("RECORDINGS_BUCKET")
    if bucket:
        s3 = client("s3")
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"recordings/{tenant_id}/"):
            keys = [{"Key": o["Key"]} for o in page.get("Contents") or []]
            if keys:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
    with pa.table().batch_writer() as batch:
        for row in rows:
            batch.delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
    return {"done": True, "counts": {"items": len(rows)}}


def billing_usage(tenant_id, event):
    end = event.get("periodEnd")
    try:
        date.fromisoformat(end)
    except (TypeError, ValueError):
        return {"ok": False, "status": 400, "code": "invalid_period", "field": "periodEnd"}
    use = pa.usage(tenant_id, end)
    row = tenant.get_tenant(tenant_id, fresh=True) or {}
    lines = [{"locationId": lid, "minutes": int(m)} for lid, m in sorted(pa.plain(use.get("loc") or {}).items())]
    return {"ok": True, "lines": lines, "includedMinutes": pa.included_minutes(row) if row else 0}


def _hook(event):
    hook, tenant_id = event.get("hook"), event.get("tenantId")
    try:
        if hook == "platform":
            return _platform(event)
        if not isinstance(tenant_id, str) or not tenant_id:
            return {"ok": False, "status": 400, "code": "tenant_required"}
        if hook == "tenant.purge":
            return {"ok": True, **purge(tenant_id, event.get("locationIds") or [])}
        if hook == "billing.usage":
            return billing_usage(tenant_id, event)
        if hook in ("privacy.export", "privacy.erase"):
            phone = _phone(event)
            if not phone:
                return {"ok": True, "items": []} if hook == "privacy.export" else {"ok": True, "erased": {},
                                                                                    "kept": {}}
            return {"ok": True, **(privacy_export(tenant_id, phone) if hook == "privacy.export"
                                   else privacy_erase(tenant_id, phone))}
    except pa.PhoneAgentError as exc:
        return {"ok": False, "status": exc.status, "code": exc.code.replace(" ", "_")}
    return {"ok": False, "status": 400, "code": "unknown_hook"}


# --- dispatch ------------------------------------------------------------------------------

ROUTES = {
    "GET /tenant/phone-agent": get_tenant_view,
    "PUT /tenant/phone-agent/config": put_config,
    "GET /locations/{locationId}/phone-agent": get_location_view,
    "PUT /locations/{locationId}/phone-agent": put_location,
    "POST /locations/{locationId}/phone-agent/kill-switch": kill_switch,
    "POST /locations/{locationId}/phone-agent/simulate": simulate,
    "GET /locations/{locationId}/phone-agent/calls": list_calls,
    "GET /locations/{locationId}/phone-agent/calls/{callId}": get_call,
    "DELETE /locations/{locationId}/phone-agent/calls/{callId}/recording": delete_recording,
    "GET /locations/{locationId}/phone-agent/callbacks": list_callbacks,
    "POST /locations/{locationId}/phone-agent/callbacks/{callbackId}/close": close_callback,
    "GET /locations/{locationId}/phone-agent/reservations/{reservationId}/call": reservation_call,
    "POST /locations/{locationId}/phone-agent/setup": post_setup,
}


def handler(event, context=None):
    if isinstance(event, dict) and "requestContext" not in event:
        if "hook" in event:
            return _hook(event)
        return {"ok": False, "status": 400, "code": "unknown_event"}
    method, path = http.route(event)
    route = f"{method} {path}"
    fn = ROUTES.get(route)
    if fn is None:
        return _err(404, "not found")
    try:
        if path.startswith("/tenant/"):
            ctx = tenant.for_jwt(event, feature=pa.FEATURE_KEY, owner_only=True)
        else:
            try:
                location_id = http.path_id(event, "locationId")
            except ValueError:
                return _err(404, "not found")
            ctx = tenant.for_jwt(event, location_id=location_id, feature=pa.FEATURE_KEY)
        return fn(event, ctx)
    except tenant.TenantError as exc:
        return exc.response()
    except pa.PhoneAgentError as exc:
        if " " in exc.code:
            return http.respond(exc.status, {"error": exc.code, **({"field": exc.field} if exc.field else {})})
        return _err(exc.status, exc.code, exc.field)
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"route": route, "aws": type(exc).__name__}))
        return _err(503, "service_unavailable")
