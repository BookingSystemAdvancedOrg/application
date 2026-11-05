"""Call lifecycle on the call row (ADR-L decision 5): in-Lambda state, every
transition a conditional update, so a duplicate webhook is a no-op.

    ringing -> answered -> disclosed -> talking -> {transferring -> talking | ended}
    any     -> ended (hangup, max length, kill switch, provider error)
    ended   -> processed (minutes metered once, outcome, events)

Minutes (decision 10): billedMinutes = ceil((min(endedAt, transferAnsweredAt)
- answeredAt) / 60); refused and unanswered calls are 0. The usage row of the
billing period gets the minutes in the same transaction that marks the call
metered, so two end webhooks meter once.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from shared import dynamo, events
from shared.features import phone_agent as pa
from shared.features.phone_agent import copy

logger = logging.getLogger(__name__)
_ser = TypeSerializer()
OPEN = ("ringing", "answered", "disclosed", "talking", "transferring")


def publish(detail_type, detail):
    try:
        events.publish(pa.FEATURE_ID, detail_type, {k: v for k, v in detail.items() if v is not None})
    except Exception as exc:  # best effort (CONTRACTS.md 5), never fails the call
        logger.warning(json.dumps({"event": detail_type, "publish": "failed", "type": type(exc).__name__}))


def get_call(tenant_id, call_id):
    key = pa.call_key(tenant_id, call_id)
    if not key:
        return None
    item = pa.table().get_item(Key=key, ConsistentRead=True).get("Item")
    return item if item and item.get("tenantId") == tenant_id else None


def start(*, tenant_id, location_id, call_ref, caller, at: datetime):
    """The call row for a provider call (idempotent by call_ref). Returns (row, created)."""
    call_id = pa.call_id_for(call_ref, at)
    key = pa.call_key(tenant_id, call_id)
    item = {**key, "tenantId": tenant_id, "locationId": location_id, "callId": call_id, "callRef": str(call_ref),
            "status": "ringing", "startedAt": pa.iso(at), "durationSec": 0, "billedMinutes": 0, "metered": False,
            "language": "sv", "counterOffer": "none", "turnCount": 0, "version": 1,
            "ttl": int((at + timedelta(days=pa.CALL_TTL_DAYS)).timestamp())}
    if caller:
        item.update({"from": caller, "fromHash": pa.phone_hash(tenant_id, caller)})
    try:
        pa.table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        return item, True
    except ClientError as exc:
        if not pa.conditional_failed(exc):
            raise
        return get_call(tenant_id, call_id), False


def _update(call, expression, values, *, statuses=None, names=None, extra_condition=None):
    cond = ["version = :v"]
    vals = {":v": call["version"], ":nv": call["version"] + 1, **values}
    if statuses:
        cond.append("#st IN (" + ", ".join(f":s{i}" for i in range(len(statuses))) + ")")
        vals.update({f":s{i}": s for i, s in enumerate(statuses)})
    if extra_condition:
        cond.append(extra_condition)
    all_names = {"#st": "status", **(names or {})}
    used = {k: v for k, v in all_names.items() if k in expression or any(k in c for c in cond)}
    try:
        return pa.table().update_item(
            Key={"PK": call["PK"], "SK": call["SK"]}, UpdateExpression=expression + ", version = :nv",
            ConditionExpression=" AND ".join(cond), ExpressionAttributeValues=vals,
            **({"ExpressionAttributeNames": used} if used else {}), ReturnValues="ALL_NEW")["Attributes"]
    except ClientError as exc:
        if pa.conditional_failed(exc):
            return None
        raise


def refuse(call, reason, at):
    """Answered with the refusal sentence and ended: outcome refused, 0 minutes."""
    lang = call.get("language") or "sv"
    done = _update(call, "SET #st = :ended, outcome = :o, refusedReason = :r, endedAt = :at, summary = :sum",
                   {":ended": "ended", ":o": "refused", ":r": reason, ":at": pa.iso(at),
                    ":sum": copy.REFUSED[lang][:200]}, statuses=["ringing"])
    if done:
        publish("PhoneCall.Refused", {"tenantId": call["tenantId"], "locationId": call["locationId"],
                                      "callId": call["callId"], "reason": reason})
    return done or get_call(call["tenantId"], call["callId"])


def append_turn(call, role, text, at, *, tool_name=None, tool_ok=None):
    """A transcript turn (24 months); the counter on the call row orders them."""
    updated = None
    for _ in range(5):
        current = get_call(call["tenantId"], call["callId"]) or call
        seq = int(current.get("turnCount") or 0) + 1
        updated = _update(current, "SET turnCount = :seq", {":seq": seq})
        if updated:
            item = {**pa.turn_key(call["tenantId"], call["callId"], seq), "tenantId": call["tenantId"],
                    "role": role, "text": str(text)[:2000], "at": pa.iso(at),
                    "ttl": int((at + timedelta(days=pa.TURN_TTL_DAYS)).timestamp())}
            if tool_name:
                item.update({"toolName": tool_name, "toolOk": bool(tool_ok)})
            pa.table().put_item(Item=item)
            return updated
    return updated


def answer(call, *, greeting, lang, at, restaurant=""):
    """ringing -> answered; the greeting with the art. 50 disclosure is the
    first agent turn, said by Booqy before the model gets a turn."""
    done = _update(call, "SET #st = :a, answeredAt = :at, #lang = :l, disclosure = :d, restaurant = :rn",
                   {":a": "answered", ":at": pa.iso(at), ":l": lang, ":rn": str(restaurant or "")[:120],
                    ":d": {"text": greeting, "playedAt": None, "repeats": 0, "languages": [lang]}},
                   statuses=["ringing"], names={"#lang": "language"})
    if not done:
        return get_call(call["tenantId"], call["callId"]), False
    append_turn(done, "agent", greeting, at)
    publish("PhoneCall.Answered", {"tenantId": call["tenantId"], "locationId": call["locationId"],
                                   "callId": call["callId"], "language": lang})
    return get_call(call["tenantId"], call["callId"]), True


def disclosed(call, at):
    """The provider's transcript event for the greeting arrived: evidence."""
    return _update(call, "SET #st = :d, disclosure.playedAt = :at", {":d": "disclosed", ":at": pa.iso(at)},
                   statuses=["answered"]) or call


def repeat_disclosure(call, lang, at, *, new_language=False):
    """Interrupted before the disclosure finished, or a language switch: the
    short form again (counted), in `lang`."""
    disclosure = pa.plain(call.get("disclosure") or {})
    if new_language and lang in (disclosure.get("languages") or []):
        return call, None
    text = copy.disclosure_short(lang, call.get("restaurant") or "")
    if new_language:
        done = _update(call, "SET disclosure.languages = list_append(disclosure.languages, :l)", {":l": [lang]},
                       statuses=list(OPEN))
    else:
        done = _update(call, "SET disclosure.repeats = disclosure.repeats + :one", {":one": 1},
                       statuses=["answered", "disclosed", "talking"])
    if not done:
        return call, None
    append_turn(done, "agent", text, at)
    return get_call(call["tenantId"], call["callId"]), text


def set_status(call, status, from_statuses, **fields):
    expr = ["#st = :status"] + [f"#f_{k} = :{k}" for k in fields]
    return _update(call, "SET " + ", ".join(expr), {":status": status, **{f":{k}": v for k, v in fields.items()}},
                   statuses=from_statuses, names={f"#f_{k}": k for k in fields})


def end(call, at, reason="hangup"):
    started = pa.parse_iso(call.get("answeredAt") or call.get("startedAt")) or at
    return _update(call, "SET #st = :ended, endedAt = :at, durationSec = :d, endReason = :r",
                   {":ended": "ended", ":at": pa.iso(at), ":d": max(0, int((at - started).total_seconds())),
                    ":r": reason}, statuses=list(OPEN)) or get_call(call["tenantId"], call["callId"])


def billed_minutes(call) -> int:
    answered = pa.parse_iso(call.get("answeredAt"))
    ended = pa.parse_iso(call.get("endedAt"))
    if not answered or not ended or call.get("outcome") == "refused":
        return 0
    transfer = pa.plain(call.get("transfer") or {})
    if transfer.get("answered") and transfer.get("at"):
        ended = min(ended, pa.parse_iso(transfer["at"]) or ended)
    return max(0, math.ceil((ended - answered).total_seconds() / 60))


def _typed(item):
    return {k: _ser.serialize(v) for k, v in item.items()}


def process(call, *, state=None, at=None, notify_cap=None):
    """ended -> processed, once: minutes metered into the period's usage row
    in one transaction with the call's `metered` flag; outcome, summary and
    counterOffer from the tool state; PhoneCall.Ended (and the cap events)."""
    if call.get("status") != "ended":
        return call
    state = state or {}
    minutes = billed_minutes(call)
    end = pa.period_end(pa.parse_iso(call.get("endedAt")) or at)
    outcome = call.get("outcome") or state.get("outcome") or ("abandoned" if minutes == 0 else "info")
    if state.get("needsCallback") and outcome not in ("callback", "booked"):
        outcome = "callback" if state.get("callbackId") else outcome
    counter = state.get("counterOffer") or call.get("counterOffer") or "none"
    table_name = os.environ["PHONE_AGENT_TABLE_NAME"]
    usage_key = pa.usage_key(call["tenantId"], end)
    pa.table().update_item(Key=usage_key, UpdateExpression="SET loc = if_not_exists(loc, :empty), tenantId = :t",
                           ExpressionAttributeValues={":empty": {}, ":t": call["tenantId"]})
    sets = {":p": "processed", ":m": minutes, ":o": outcome, ":c": counter, ":true": True, ":f": False,
            ":v": call["version"], ":nv": call["version"] + 1, ":ended": "ended"}
    expr = "SET #st = :p, billedMinutes = :m, outcome = :o, counterOffer = :c, metered = :true, version = :nv"
    for key in ("summary", "reservationId", "callbackId"):
        if state.get(key):
            expr += f", {key} = :{key}"
            sets[f":{key}"] = state[key]
    if state.get("reservationId"):  # GSI byReservation: the Bokningar badge (L12)
        expr += ", GSI2PK = :g2pk, GSI2SK = :g2sk"
        sets.update({":g2pk": f"{pa.tenant_pk(call['tenantId'])}#RES#{state['reservationId']}",
                     ":g2sk": call.get("startedAt") or ""})
    try:
        dynamo.client().transact_write_items(TransactItems=[
            {"Update": {"TableName": table_name, "Key": _typed({"PK": call["PK"], "SK": call["SK"]}),
                        "UpdateExpression": expr,
                        "ConditionExpression": "#st = :ended AND metered = :f AND version = :v",
                        "ExpressionAttributeNames": {"#st": "status"}, "ExpressionAttributeValues": _typed(sets)}},
            {"Update": {"TableName": table_name, "Key": _typed(usage_key),
                        "UpdateExpression": "ADD minutes :m, calls :one, loc.#l :m",
                        "ExpressionAttributeNames": {"#l": call["locationId"]},
                        "ExpressionAttributeValues": _typed({":m": minutes, ":one": 1})}},
        ])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            return get_call(call["tenantId"], call["callId"])  # processed already (second end webhook)
        raise
    done = get_call(call["tenantId"], call["callId"])
    publish("PhoneCall.Ended", {"tenantId": call["tenantId"], "locationId": call["locationId"],
                                "callId": call["callId"], "durationSec": int(call.get("durationSec") or 0),
                                "billedMinutes": minutes, "outcome": outcome,
                                "reservationId": state.get("reservationId"), "callbackId": state.get("callbackId"),
                                "counterOffer": counter})
    if notify_cap:
        notify_cap(call["tenantId"], end)
    return done


def check_cap(tenant_row, config, end, *, warn):
    """After metering: 80 % warns the owner once, 100 % publishes CapReached once."""
    tenant_id = tenant_row["tenantId"]
    row = pa.usage(tenant_id, end)
    minutes, cap = int(row.get("minutes") or 0), pa.tenant_cap(tenant_row, config)
    if cap <= 0:
        return
    for flag, threshold in (("warned80", 0.8), ("capReached", 1.0)):
        if minutes >= cap * threshold and not row.get(flag):
            try:
                pa.table().update_item(Key=pa.usage_key(tenant_id, end), UpdateExpression=f"SET {flag} = :t",
                                       ConditionExpression=f"attribute_not_exists({flag})",
                                       ExpressionAttributeValues={":t": True})
            except ClientError as exc:
                if pa.conditional_failed(exc):
                    continue
                raise
            if flag == "warned80":
                warn(tenant_id, minutes, cap)
            else:
                publish("PhoneMinutes.CapReached", {"tenantId": tenant_id, "periodEnd": end, "minutes": minutes,
                                                    "capMinutes": cap})
