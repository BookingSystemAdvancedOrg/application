"""Publishing feature events to the custom bus (CONTRACTS.md section 5).

    from shared import events
    events.publish("gift-cards", "GiftCard.Sold", {"tenantId": t, "cardId": c, ...})

The detail is checked against the contract (shared/contracts.EVENTS): an
unknown source or detail-type, a missing required field or a field the
contract doesn't list raises ValueError, so nothing undocumented (names,
emails, codes) can leave on the bus. `occurredAt` and `version` are filled
in. The bus name comes from EVENT_BUS_NAME (Terraform sets it).

Callers publish after their own write succeeded; a failed publish is
logged by the caller and never fails the request.
"""

import json
import os
from datetime import datetime, timezone

import boto3

from shared import contracts

_client = None


def client():
    global _client
    if _client is None:
        _client = boto3.client("events")
    return _client


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def publish(feature_id: str, detail_type: str, detail: dict) -> str:
    """Puts one event on the bus and returns its event id."""
    source = f"booqy.{feature_id}"
    spec = contracts.EVENTS.get(source, {}).get(detail_type)
    if spec is None:
        raise ValueError(f"unknown event {source} {detail_type}")
    if not isinstance(detail, dict):
        raise ValueError("detail must be an object")
    common = set(contracts.EVENT_COMMON_FIELDS)
    allowed = common | set(spec["required"]) | set(spec["optional"])
    extra = sorted(set(detail) - allowed)
    if extra:
        raise ValueError(f"fields not in the contract: {', '.join(extra)}")
    body = {**detail, "occurredAt": detail.get("occurredAt") or _now(), "version": 1}
    missing = sorted(k for k in common | set(spec["required"]) if body.get(k) is None)
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    bus = os.environ["EVENT_BUS_NAME"]
    answer = client().put_events(Entries=[{
        "Source": source, "DetailType": detail_type, "Detail": json.dumps(body, sort_keys=True),
        "EventBusName": bus,
    }])
    entry = (answer.get("Entries") or [{}])[0]
    if answer.get("FailedEntryCount") or not entry.get("EventId"):
        raise RuntimeError(f"event not accepted: {entry.get('ErrorCode') or 'unknown'}")
    return entry["EventId"]
