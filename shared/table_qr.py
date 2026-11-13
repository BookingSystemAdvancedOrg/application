"""Per-table QR codes for ordering at the table.

Every table in a location's ACTIVE published floor plan gets its own code: a
random 128-bit key (22 url-safe characters), printed in the QR code as
``/bestall?bord=<key>``. The key says nothing about the table; the server
looks it up. So a code:

    * can't be guessed or edited into another table (no table id in it),
    * belongs to one location (the key lives in that location's partition,
      so a code from another restaurant or location is simply unknown),
    * survives renaming and moving the table: the name is read from the
      active floor plan when the guest scans and when the order is placed,
    * dies with the table: a deleted table gets no new number reused by
      accident, because a new table is a new element id with its own key,
    * can be regenerated (sticker stolen): the old key is kept, marked
      revoked, so the guest gets a clear message instead of "unknown".

Records (order table, location partition):
    SK TABLEQR#TABLE#<tableId>  {key, legacyOk, createdAt/By, rotatedAt/By}
    SK TABLEQR#KEY#<key>        {tableId, createdAt, revokedAt/By}

Codes printed before this module (``payload.signature``, shared/orders.py
v1, name inside) keep working until the table's code is regenerated once
(``legacyOk`` false) or the table leaves the floor plan; their name is also
taken from the floor plan now.

The draft floor plan never counts: a table exists for ordering once its
layout is published and active (what guests book against).

ENV_VARS: ORDER_TABLE_NAME, PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
RESERVATION_LINK_KEY_SECRET_ARN (old codes).
"""

import os
import re
import secrets

from botocore.exceptions import ClientError

from shared import dynamo
from shared import orders as o

_KEY = re.compile(r"[A-Za-z0-9_-]{22}")
_TABLE_PREFIX = "TABLEQR#TABLE#"
_KEY_PREFIX = "TABLEQR#KEY#"
_ACTIVATION_SK = "LAYOUT#ACTIVATION"

INVALID = "table code is invalid"
REVOKED = "table code revoked"
REMOVED = "table removed"


class CodeError(ValueError):
    """A scanned code can't be used. ``str(exc)`` is the API error code."""


# --- the active floor plan ------------------------------------------------------------

def _snapshot_table():
    return dynamo.table(os.environ["PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"])


def tables_from_layout(elements):
    """[{tableId, label, seats}] numbered exactly like the admin and the guest
    site (admin shared/tables.ts): floors by level, tables in drawing order,
    1, 2, 3 ... across floors; a table's own label wins."""
    elements = [e for e in elements or [] if isinstance(e, dict)]
    floors = sorted((e for e in elements if e.get("type") == "floor"),
                    key=lambda f: int(f.get("level") or 0))
    others = [e for e in elements if e.get("type") != "floor"]
    groups = [[e for e in others if e.get("floorId") == f.get("elementId")] for f in floors] if floors else [others]
    out = []
    for element in (e for group in groups for e in group if e.get("type") == "table"):
        label = element.get("label")
        label = " ".join(label.split()) if isinstance(label, str) and label.strip() else str(len(out) + 1)
        out.append({"tableId": element["elementId"], "label": label[:40], "seats": int(element.get("seats") or 0)})
    return out


def active_tables(location_id):
    """The tables of the location's active published floor plan; [] when
    nothing is published."""
    snapshots = _snapshot_table()
    pk = f"LOCATION#{location_id}"
    state = snapshots.get_item(Key={"PK": pk, "SK": _ACTIVATION_SK}, ConsistentRead=True).get("Item")
    if not state or state.get("currentVersion") is None:
        return []
    snapshot = snapshots.get_item(Key={"PK": pk, "SK": f"LAYOUT#v{int(state['currentVersion'])}"},
                                  ConsistentRead=True).get("Item")
    if not snapshot:
        return []
    return tables_from_layout(snapshot.get("elements"))


def table_title(label):
    """"Bord 7" for a number ("7", "12b"); a name ("Bar", "Bord 7",
    "Fönsterbordet") is shown as it is."""
    return f"Bord {label}" if re.match(r"\d", label or "") else label


# --- codes ----------------------------------------------------------------------------

def _orders():
    return dynamo.table(o.order_table())


def _table_key(location_id, table_id):
    return {"PK": o.location_pk(location_id), "SK": f"{_TABLE_PREFIX}{table_id}"}


def _code_key(location_id, key):
    return {"PK": o.location_pk(location_id), "SK": f"{_KEY_PREFIX}{key}"}


def _new_key():
    return secrets.token_urlsafe(16)


def _ser(value):
    return {"S": value} if isinstance(value, str) else {"BOOL": value}


def _update(key, values, condition, condition_values=None):
    """A TransactWriteItems Update (UpdateItem only: the role has no PutItem)."""
    names = {f"#{k}": k for k in values}
    if "#key" in condition:
        names["#key"] = "key"
    expression_values = {f":{k}": _ser(v) for k, v in values.items()}
    expression_values.update({k: _ser(v) for k, v in (condition_values or {}).items()})
    return {"Update": {
        "TableName": o.order_table(),
        "Key": {k: {"S": v} for k, v in key.items()},
        "UpdateExpression": "SET " + ", ".join(f"#{k} = :{k}" for k in values),
        "ConditionExpression": condition,
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": expression_values,
    }}


def current_codes(location_id):
    """{tableId: key} for every table that has a code."""
    found = {}
    request = {"KeyConditionExpression": "PK = :pk AND begins_with(SK, :p)",
               "ExpressionAttributeValues": {":pk": o.location_pk(location_id), ":p": _TABLE_PREFIX},
               "ConsistentRead": True}
    table = _orders()
    while True:
        page = table.query(**request)
        for item in page.get("Items") or []:
            if isinstance(item.get("key"), str):
                found[item["tableId"]] = item["key"]
        if "LastEvaluatedKey" not in page:
            return found
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _create(tenant_id, location_id, table_id, by, now, *, legacy_ok):
    key = _new_key()
    stamp = o.iso(now)
    try:
        dynamo.client().transact_write_items(TransactItems=[
            _update(_table_key(location_id, table_id),
                    {"recordType": "tableQr", "tenantId": tenant_id, "locationId": location_id,
                     "tableId": table_id, "key": key, "legacyOk": legacy_ok,
                     "createdAt": stamp, "createdBy": by},
                    "attribute_not_exists(PK)"),
            _update(_code_key(location_id, key),
                    {"recordType": "tableQrKey", "tenantId": tenant_id, "locationId": location_id,
                     "tableId": table_id, "createdAt": stamp, "createdBy": by},
                    "attribute_not_exists(PK)"),
        ])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        # Someone else made it a moment ago: use theirs.
        item = _orders().get_item(Key=_table_key(location_id, table_id), ConsistentRead=True).get("Item")
        if not item:
            raise
        return item["key"]
    return key


def codes_for(tenant_id, location_id, by, now):
    """[{tableId, label, seats, code}] for every table in the active floor
    plan, creating the missing codes."""
    tables = active_tables(location_id)
    existing = current_codes(location_id)
    for table in tables:
        table["code"] = existing.get(table["tableId"]) or _create(
            tenant_id, location_id, table["tableId"], by, now, legacy_ok=True)
    return tables


def regenerate(tenant_id, location_id, table_id, by, now):
    """A new code for one table; the old one (and any pre-2026-11 printed
    code) stops working. NotFound when the table isn't in the active plan."""
    table = next((t for t in active_tables(location_id) if t["tableId"] == table_id), None)
    if table is None:
        raise o.NotFound
    current = _orders().get_item(Key=_table_key(location_id, table_id), ConsistentRead=True).get("Item")
    if not current:
        table["code"] = _create(tenant_id, location_id, table_id, by, now, legacy_ok=False)
        return table
    old, key, stamp = current["key"], _new_key(), o.iso(now)
    try:
        dynamo.client().transact_write_items(TransactItems=[
            _update(_table_key(location_id, table_id),
                    {"key": key, "legacyOk": False, "rotatedAt": stamp, "rotatedBy": by},
                    "#key = :old", {":old": old}),
            _update(_code_key(location_id, old), {"revokedAt": stamp, "revokedBy": by},
                    "attribute_exists(PK)"),
            _update(_code_key(location_id, key),
                    {"recordType": "tableQrKey", "tenantId": tenant_id, "locationId": location_id,
                     "tableId": table_id, "createdAt": stamp, "createdBy": by},
                    "attribute_not_exists(PK)"),
        ])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise o.InvalidState("changed_retry") from None
        raise
    table["code"] = key
    return table


def resolve(tenant_id, location_id, code):
    """(tableId, label) for a scanned code of this location, else CodeError
    (INVALID, REVOKED or REMOVED)."""
    if not isinstance(code, str) or not code:
        raise CodeError(INVALID)
    if "." in code:
        try:
            table_id, _ = o.read_table_code(tenant_id, location_id, code)
        except ValueError:
            raise CodeError(INVALID) from None
        record = _orders().get_item(Key=_table_key(location_id, table_id), ConsistentRead=True).get("Item")
        if record and record.get("legacyOk") is False:
            raise CodeError(REVOKED)
    else:
        if not _KEY.fullmatch(code):
            raise CodeError(INVALID)
        record = _orders().get_item(Key=_code_key(location_id, code), ConsistentRead=True).get("Item")
        if not record or record.get("tenantId") != tenant_id:
            raise CodeError(INVALID)
        if record.get("revokedAt"):
            raise CodeError(REVOKED)
        table_id = record["tableId"]
    table = next((t for t in active_tables(location_id) if t["tableId"] == table_id), None)
    if table is None:
        raise CodeError(REMOVED)
    return table_id, table["label"]
