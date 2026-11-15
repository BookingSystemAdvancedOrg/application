"""phone-agent-jobs

TRIGGER:
    EventBridge Scheduler, every 5 minutes      {"job": "sweep"}
    EventBridge Scheduler, nightly 03:00 Stockholm {"job": "nightly"}

PURPOSE:
    ADR-L decision 5 and "Privacy and GDPR". The sweep ends calls that ran
    past their location's max length (+60 s) and processes ended calls
    older than 2 minutes that no end webhook processed (metering is
    idempotent, so a call processed twice is metered once). The nightly job
    closes callbacks open for 30 days, removes caller numbers and names from
    call rows and callbacks after 24 months (the contact rule; turns expire
    by TTL) and releases numbers whose 30-day grace after disable is over.

    Both read the table with a Scan filtered on the row kind: calls are few
    per tenant and the jobs run in the background. (Scale note: a status
    index is the upgrade when the table grows; see docs/features/phone-agent.md.)

ENV_VARS:
    ENVIRONMENT, PHONE_AGENT_TABLE_NAME, TENANT_TABLE_NAME, EVENT_BUS_NAME, TELEPHONY_CLIENT, VOICE_CLIENT
"""

import json
import logging
from datetime import timedelta

from boto3.dynamodb.conditions import Attr

from shared import telephony, tenant, voice_provider
from shared.features import phone_agent as pa
from shared.features.phone_agent import calls

logger = logging.getLogger()
logger.setLevel(logging.INFO)
_clients = {}
CONTACT_DAYS = 730
CALLBACK_AUTO_CLOSE_DAYS = 30
STUCK_ENDED = timedelta(minutes=2)
MAX_SLACK = timedelta(seconds=60)


def client(name):
    if name not in _clients:
        _clients[name] = {"voice": voice_provider.client, "telephony": telephony.client}[name]()
    return _clients[name]


def _scan(filter_expression):
    items, args = [], {"FilterExpression": filter_expression}
    while True:
        page = pa.table().scan(**args)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        args["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def sweep(at=None):
    at = at or pa.now()
    rows = _scan(Attr("SK").begins_with("CALL#") & Attr("status").is_in(list(calls.OPEN) + ["ended"]))
    ended = processed = 0
    for call in rows:
        if "#T#" in call["SK"]:
            continue
        if call["status"] in calls.OPEN:
            loc = pa.get_loc(call["tenantId"], call["locationId"])
            started = pa.parse_iso(call.get("answeredAt") or call.get("startedAt")) or at
            limit = timedelta(seconds=int(loc.get("maxCallSeconds") or pa.MAX_CALL_DEFAULT)) + MAX_SLACK
            if at - started <= limit:
                continue
            if call.get("callRef"):
                try:
                    client("voice").end(call["callRef"], "max_length")
                except voice_provider.VoiceProviderError as exc:
                    logger.warning(json.dumps({"voice": "end_failed", "code": exc.code}))
            call = calls.end(call, at, "max_length")
            ended += 1
        elif at - (pa.parse_iso(call.get("endedAt")) or at) < STUCK_ENDED:
            continue
        state = json.loads(call.get("toolState") or "{}")
        calls.process(call, state=state, at=at)
        processed += 1
    return {"ended": ended, "processed": processed}


def nightly(at=None):
    at = at or pa.now()
    closed = scrubbed = released = 0
    for cb in _scan(Attr("SK").begins_with("CALLBACK#") & Attr("status").eq("open")):
        created = pa.parse_iso(cb.get("createdAt")) or at
        if at - created >= timedelta(days=CALLBACK_AUTO_CLOSE_DAYS):
            pa.table().update_item(Key={"PK": cb["PK"], "SK": cb["SK"]},
                                   UpdateExpression="SET #s = :c, closedAt = :at, closedBy = :by, #t = :ttl",
                                   ConditionExpression="#s = :o",
                                   ExpressionAttributeNames={"#s": "status", "#t": "ttl"},
                                   ExpressionAttributeValues={":c": "closed", ":o": "open", ":at": pa.iso(at),
                                                              ":by": "system", ":ttl": int((at + timedelta(
                                                                  days=pa.CALLBACK_TTL_DAYS)).timestamp())})
            closed += 1
    cutoff = pa.iso(at - timedelta(days=CONTACT_DAYS))
    for row in _scan((Attr("SK").begins_with("CALL#") | Attr("SK").begins_with("CALLBACK#"))
                     & (Attr("from").exists() | Attr("phone").exists())):
        if "#T#" in row["SK"] or (row.get("startedAt") or row.get("createdAt") or "") > cutoff:
            continue
        pa.table().update_item(Key={"PK": row["PK"], "SK": row["SK"]}, UpdateExpression="REMOVE #f, phone, #n",
                               ExpressionAttributeNames={"#f": "from", "#n": "name"})
        scrubbed += 1
    for loc in _scan(Attr("SK").begins_with("LOC#") & Attr("number.releaseAfter").exists()):
        number = pa.plain(loc.get("number") or {})
        due = pa.parse_iso(number.get("releaseAfter"))
        if not due or due > at or loc.get("enabled"):
            continue
        try:
            client("telephony").release_number(number["providerId"])
            if loc.get("providerAgentId"):
                client("voice").delete_agent(loc["providerAgentId"])
        except (telephony.TelephonyError, voice_provider.VoiceProviderError) as exc:
            logger.warning(json.dumps({"release": "failed", "code": exc.code}))
            continue
        pa.table().delete_item(Key=pa.number_key(loc["tenantId"], number["e164"]))
        pa.table().update_item(Key={"PK": loc["PK"], "SK": loc["SK"]}, UpdateExpression="REMOVE #n, providerAgentId",
                               ExpressionAttributeNames={"#n": "number"})
        calls.publish("PhoneNumber.Released", {"tenantId": loc["tenantId"], "locationId": loc["locationId"]})
        released += 1
    tenant.reset_caches()
    return {"callbacksClosed": closed, "contactsRemoved": scrubbed, "numbersReleased": released}


def handler(event, context=None):
    job = (event or {}).get("job")
    if job == "sweep":
        return sweep()
    if job == "nightly":
        return nightly()
    return {"ok": False, "code": "unknown_job"}
