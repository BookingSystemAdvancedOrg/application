"""guest-crm-sync

TRIGGER:
    DynamoDB Streams (NEW_AND_OLD_IMAGES) of the reservation, order and
    catering-requests tables; event source mappings with filter criteria
    on the status and with ReportBatchItemFailures.

PURPOSE:
    Feeds the guest register (ADR-C 3). A booking counts when it becomes
    `arrived` (a no-show separately), an order when it is paid or
    completed, a catering request when it is accepted or paid. Each source
    row is applied once (history row written with attribute_not_exists), so
    stream replays and arrived -> undo -> arrived count once.

    Writes only for tenants with `guestCrm` on. Emits
    `GuestVisit.Recorded` (no names or contact, CONTRACTS.md 5) for tenants
    with `guestCrm` or `googleBusiness` on, for arrived bookings, counted
    orders and catering, once per newly applied row (every time for a
    googleBusiness-only tenant: its consumer is idempotent).

ENV_VARS:
    ENVIRONMENT, GUEST_CRM_TABLE_NAME, TENANT_TABLE_NAME, LOCATION_TABLE_NAME,
    LOCATION_ID_INDEX_NAME, RESERVATION_TABLE_NAME, ORDER_TABLE_NAME,
    CATERING_TABLE_NAME, EVENT_BUS_NAME, DATA_RETENTION_DAYS (optional)
"""

import json
import logging
import os

from boto3.dynamodb.types import TypeDeserializer

from shared import events, tenant
from shared.features import guest_crm as crm

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_deserializer = TypeDeserializer()
PREFIX = {"reservation": ("booking", "RESERVATION#"), "order": ("order", "ORDER#"),
          "catering": ("catering", "REQUEST#")}


def _logical(source_arn: str):
    """reservation | order | catering from the stream's table ARN."""
    table_name = (source_arn or "").split(":table/", 1)[-1].split("/", 1)[0]
    for logical, env in crm.LOGICAL_TABLES.items():
        if table_name and os.environ.get(env) == table_name:
            return logical
    return None


def _image(record):
    raw = (record.get("dynamodb") or {}).get("NewImage")
    if not raw:
        return None
    return {k: _deserializer.deserialize(v) for k, v in raw.items()}


def _process(record):
    if record.get("eventName") not in ("INSERT", "MODIFY"):
        return
    logical = _logical(record.get("eventSourceARN"))
    item = _image(record)
    if not logical or not item:
        return
    kind, prefix = PREFIX[logical]
    if not str(item.get("SK", "")).startswith(prefix) or not crm.classify(kind, item):
        return
    location_id = item.get("locationId") or str(item.get("PK", "")).removeprefix("LOCATION#")
    tenant_id = tenant.tenant_of_location(location_id) if location_id else None
    if not tenant_id or (item.get("tenantId") and item["tenantId"] != tenant_id):
        return
    tenant_row = tenant.get_tenant(tenant_id) or {}
    features = (tenant_row.get("entitlements") or {}).get("features") or {}
    crm_on, gbp_on = bool(features.get("guestCrm")), bool(features.get("googleBusiness"))
    if tenant_row.get("status") != "active" or not (crm_on or gbp_on):
        return
    ref = {"table": logical, "pk": item["PK"], "sk": item["SK"]}
    result = crm.record_source(tenant_id, kind, item, location_id=location_id, contact_ref=ref, write=crm_on)
    if not result or result["counted"] == "noshow" or not result["new"]:
        return
    email, phone = item.get("customerEmail"), item.get("customerPhone")
    guest_key = crm.identities(tenant_id, email, phone)[0][1]
    fields = crm.source_fields(kind, item)
    detail = {"tenantId": tenant_id, "locationId": location_id, "kind": kind, "sourceId": fields["sourceId"],
              "guestKey": guest_key, "channels": result["channels"], "contactRef": ref,
              "returning": bool(result["returning"]), "at": crm.iso(crm.parse_iso(fields["at"]))}
    try:
        events.publish(crm.FEATURE_ID, "GuestVisit.Recorded", detail)
    except Exception as exc:  # never fail the batch for a lost event
        logger.warning(json.dumps({"event": "GuestVisit.Recorded", "publish": "failed",
                                   "type": type(exc).__name__}))


def handler(event, context=None):
    failures = []
    for record in (event or {}).get("Records") or []:
        try:
            _process(record)
        except Exception as exc:
            logger.error(json.dumps({"record": record.get("eventID"), "error": type(exc).__name__}))
            sequence = (record.get("dynamodb") or {}).get("SequenceNumber")
            if sequence:
                failures.append({"itemIdentifier": sequence})
    return {"batchItemFailures": failures}
