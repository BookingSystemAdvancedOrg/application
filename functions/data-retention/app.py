"""data-retention

TRIGGER:
    EventBridge Scheduler (data_retention_schedule: nightly). Input {} = a
    sweep. The function invokes itself asynchronously to carry on when a run
    gets close to its timeout: {"after": "<last tenant id done>", "round": n}.

PURPOSE:
    Deletes or anonymises guest data whose retention period is over, for
    every restaurant (shared/retention.py has the rules, docs/privacy.md the
    inventory). Offboarded restaurants are purged as a whole by tenant-purge;
    this job also runs for them until then, which is harmless.

      * bookings: deleted after the booking period (normally DynamoDB TTL has
        done it already); a booking with a card charge is a receipt: its
        expiry is moved to the end of the accounting period ("protect") and
        its guest fields are removed after the booking period
      * orders: paid or refunded = receipt, guest fields removed after the
        guest period, deleted when the accounting period ends; never paid =
        deleted after the cancelled period
      * catering requests (+ their history): like orders when paid or
        invoiced; declined, rejected, expired or cancelled ones deleted after
        the cancelled period; others after the guest period
      * consent records: after a withdrawal, the full records are replaced by
        one suppression record without the guest's address or number; the
        suppression record is deleted when its period is over (and the
        guest's marketing subscriber row with it, unless it is bounced or
        complained: those follow their own rule below)
      * marketing subscriber rows: unconfirmed sign-ups (pending) deleted
        after RETENTION_PENDING_DAYS; bounced/complained rows lose the
        address after the consent history period and are deleted after the
        suppression period (the hashed key keeps the block until then)

    Never touched: Booqy's own invoicing (finance table), Stripe, catering
    PDFs (S3 Object Lock), tenant profiles, statistics without personal data.

    DRY_RUN=true (the default until the owner switches it off) only counts:
    nothing is written or deleted. Logs and the summary hold counts and
    tenant ids only, never personal data. Every step is idempotent: an item
    that is already gone or anonymised is skipped, so a run that stops
    halfway is finished by the next one. Stream consumers ignore these
    writes (no new notice), so no guest gets a message.

ENV_VARS:
    ENVIRONMENT, DRY_RUN, TENANT_TABLE_NAME, TENANT_LIST_INDEX_NAME,
    LOCATION_TABLE_NAME, RESERVATION_TABLE_NAME, ORDER_TABLE_NAME,
    CATERING_REQUESTS_TABLE_NAME, CATERING_REQUEST_HISTORY_TABLE_NAME,
    CONSENT_TABLE_NAME, CONSENT_TENANT_INDEX_NAME, MARKETING_TABLE_NAME,
    ALERT_TOPIC_ARN, RETENTION_* (shared/retention.py)

AWS RESOURCE ACCESS:
    Query on the tenant table (list index) and the location table;
    Query/UpdateItem/DeleteItem/BatchWriteItem on the reservation, order,
    catering request and history tables; Query/PutItem/DeleteItem/
    BatchWriteItem on the consent table and its index; Query/UpdateItem/
    DeleteItem on the marketing table; sns:Publish on the alert topic; lambda:InvokeFunction
    on itself (continuation).
"""

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import consent, dynamo, marketing, retention

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STOP_MARGIN_MS = 60_000
MAX_ROUNDS = 10

_clients = {}


class OutOfTime(Exception):
    pass


class Budget:
    def __init__(self, context):
        self._getter = getattr(context, "get_remaining_time_in_millis", None)

    def check(self):
        if self._getter is not None and self._getter() < STOP_MARGIN_MS:
            raise OutOfTime()


def _client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def now_utc():
    return datetime.now(timezone.utc)


def dry_run():
    return (os.environ.get("DRY_RUN") or "true").strip().lower() != "false"


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _query(table_name, condition, *, index=None, filter_expression=None):
    table = dynamo.table(table_name)
    request = {"KeyConditionExpression": condition}
    if index:
        request["IndexName"] = index
    if filter_expression is not None:
        request["FilterExpression"] = filter_expression
    while True:
        page = table.query(**request)
        yield from page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _missing(exc):
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == \
        "ConditionalCheckFailedException"


def _delete(table_name, key, budget):
    """Deletes one row; already gone is fine."""
    budget.check()
    dynamo.table(table_name).delete_item(Key=key)


def _anonymise(table_name, item, kind, now, budget, ttl=None):
    """Strips the guest fields, only if the row still exists and is not
    anonymised yet (idempotent)."""
    budget.check()
    expr, names, values = retention.anonymise_update(kind, item, _iso(now), ttl=ttl)
    names["#pk"] = "PK"
    try:
        dynamo.table(table_name).update_item(
            Key={"PK": item["PK"], "SK": item["SK"]}, UpdateExpression=expr,
            ConditionExpression="attribute_exists(#pk) AND attribute_not_exists(#a)",
            ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        return True
    except ClientError as exc:
        if _missing(exc):
            return False
        raise


def _set_ttl(table_name, item, ttl, budget):
    budget.check()
    try:
        dynamo.table(table_name).update_item(
            Key={"PK": item["PK"], "SK": item["SK"]}, UpdateExpression="SET #t = :t",
            ConditionExpression="attribute_exists(#pk)", ExpressionAttributeNames={"#t": "ttl", "#pk": "PK"},
            ExpressionAttributeValues={":t": ttl})
    except ClientError as exc:
        if not _missing(exc):
            raise


# ----------------------------------------------------------------- per type
def _bookings(location_id, policy, today, now, counts, budget):
    name = os.environ["RESERVATION_TABLE_NAME"]
    pk = f"LOCATION#{location_id}"
    for item in _query(name, Key("PK").eq(pk) & Key("SK").begins_with("RESERVATION#")):
        action = retention.booking_action(item, today, policy)
        if action is None:
            continue
        if isinstance(action, tuple):
            counts["bookingsProtected"] += 1
            if not dry_run():
                _set_ttl(name, item, action[1], budget)
                pointer = {"PK": pk, "SK": f"RID#{item.get('reservationId')}"}
                _set_ttl(name, pointer, action[1], budget)
            continue
        if action == "anonymise":
            counts["bookingsAnonymised"] += 1
            if not dry_run():
                # Only charged bookings are anonymised: they keep the receipt until the period ends.
                ttl = retention.accounting_ttl(retention.parse_day(item["date"]), policy.accounting_years)
                _anonymise(name, item, "booking", now, budget, ttl=ttl)
                _set_ttl(name, {"PK": pk, "SK": f"RID#{item.get('reservationId')}"}, ttl, budget)
            continue
        counts["bookingsDeleted"] += 1
        if not dry_run():
            _delete(name, {"PK": pk, "SK": item["SK"]}, budget)
            if item.get("reservationId"):
                _delete(name, {"PK": pk, "SK": f"RID#{item['reservationId']}"}, budget)


def _orders(location_id, policy, today, now, counts, budget):
    name = os.environ["ORDER_TABLE_NAME"]
    pk = f"LOCATION#{location_id}"
    for item in _query(name, Key("PK").eq(pk) & Key("SK").begins_with("ORDER#")):
        action = retention.order_action(item, today, policy)
        if action == "anonymise":
            counts["ordersAnonymised"] += 1
            if not dry_run():
                _anonymise(name, item, "order", now, budget)
        elif action == "delete":
            counts["ordersDeleted"] += 1
            if not dry_run():
                _delete(name, {"PK": pk, "SK": item["SK"]}, budget)
                if item.get("orderId"):
                    _delete(name, {"PK": pk, "SK": f"OID#{item['orderId']}"}, budget)


def _catering(location_id, policy, today, now, counts, budget):
    name = os.environ["CATERING_REQUESTS_TABLE_NAME"]
    history = os.environ["CATERING_REQUEST_HISTORY_TABLE_NAME"]
    pk = f"LOCATION#{location_id}"
    for item in _query(name, Key("PK").eq(pk) & Key("SK").begins_with("REQUEST#")):
        action = retention.catering_action(item, today, policy)
        if action == "anonymise":
            counts["cateringAnonymised"] += 1
            if not dry_run():
                _anonymise(name, item, "catering", now, budget)
        elif action == "delete":
            counts["cateringDeleted"] += 1
            if dry_run():
                continue
            request_id = item.get("requestId") or str(item["SK"]).removeprefix("REQUEST#")
            # History first: a run that stops here finds the request again.
            for row in _query(history, Key("PK").eq(f"REQUEST#{request_id}")):
                _delete(history, {"PK": row["PK"], "SK": row["SK"]}, budget)
                counts["cateringHistoryDeleted"] += 1
            _delete(name, {"PK": pk, "SK": item["SK"]}, budget)


def _consent(tenant_id, policy, now, counts, budget):
    name = consent.table_name()
    if not name:
        return
    subjects = defaultdict(list)
    for item in _query(name, Key("tenantId").eq(tenant_id),
                       index=os.environ.get("CONSENT_TENANT_INDEX_NAME", consent.TENANT_INDEX)):
        subjects[item["PK"]].append(item)
    for pk, records in subjects.items():
        records.sort(key=lambda r: r["SK"], reverse=True)
        action = retention.consent_action(records, now, policy)
        if action is None:
            continue
        if action == "delete":
            counts["consentSuppressionsDeleted"] += 1
            if not dry_run():
                for r in records:
                    _delete(name, {"PK": r["PK"], "SK": r["SK"]}, budget)
            continue
        latest = action[1]
        counts["consentSuppressed"] += 1
        if dry_run():
            continue
        # One minimal record first, then the full ones go: a run that stops
        # between the two leaves the guest suppressed, never unknown.
        sk = f"{latest['at']}#S"
        budget.check()
        try:
            dynamo.table(name).put_item(
                Item={"PK": pk, "SK": sk, "tenantId": tenant_id, "at": latest["at"], "subject": latest["subject"],
                      "channel": latest.get("channel"), "action": consent.SUPPRESSED,
                      "ttl": retention.suppression_ttl(latest["at"], policy)},
                ConditionExpression="attribute_not_exists(SK)")
        except ClientError as exc:
            if not _missing(exc):
                raise
        email = next((r.get("identifier") for r in records
                      if r.get("channel") == consent.EMAIL and r.get("identifier")), None)
        for r in records:
            if r["SK"] != sk:
                _delete(name, {"PK": r["PK"], "SK": r["SK"]}, budget)
        if email and os.environ.get("MARKETING_TABLE_NAME"):
            sub_key = marketing.sub_key(tenant_id, marketing.subscriber_id(tenant_id, email))
            try:
                # Bounced/complained rows stay: _subscribers strips them and keeps the block.
                dynamo.table(os.environ["MARKETING_TABLE_NAME"]).delete_item(
                    Key=sub_key, ConditionExpression="attribute_not_exists(#s) OR NOT #s IN (:sub, :b, :c)",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":sub": "subscribed", ":b": "bounced", ":c": "complained"})
                counts["subscribersDeleted"] += 1
            except ClientError as exc:
                if not _missing(exc):
                    raise


def _subscribers(tenant_id, policy, now, counts, budget):
    name = os.environ.get("MARKETING_TABLE_NAME")
    if not name:
        return
    table = dynamo.table(name)
    for item in _query(name, Key("PK").eq(marketing.sub_pk(tenant_id)) & Key("SK").begins_with("SUB#")):
        action = retention.subscriber_action(item, now, policy)
        if action is None:
            continue
        status = item.get("status")
        if status == "pending":
            counts["subscribersPendingDeleted"] += 1
        else:
            counts["subscribersSuppressedDeleted" if action == "delete" else "subscribersSuppressedStripped"] += 1
        if dry_run():
            continue
        budget.check()
        key = {"PK": item["PK"], "SK": item["SK"]}
        names, values = {"#s": "status"}, {":s": status}
        try:
            # Conditional on the status: a sign-up confirmed meanwhile stays.
            if action == "delete":
                table.delete_item(Key=key, ConditionExpression="#s = :s", ExpressionAttributeNames=names,
                                  ExpressionAttributeValues=values)
                continue
            removes = [k for k in item if k not in retention.SUBSCRIBER_KEEP]
            names.update({f"#r{i}": k for i, k in enumerate(removes)}, **{"#a": "anonymisedAt"})
            values[":a"] = _iso(now)
            table.update_item(Key=key, ConditionExpression="#s = :s", ExpressionAttributeNames=names,
                              ExpressionAttributeValues=values,
                              UpdateExpression="SET #a = :a REMOVE " + ", ".join(f"#r{i}" for i in range(len(removes))))
        except ClientError as exc:
            if not _missing(exc):
                raise


# ----------------------------------------------------------------- sweep
def _tenants():
    request = {"IndexName": os.environ.get("TENANT_LIST_INDEX_NAME", "GSI1"),
               "KeyConditionExpression": Key("GSI1PK").eq("TENANT"),
               "ProjectionExpression": "tenantId, #s", "ExpressionAttributeNames": {"#s": "status"}}
    table = dynamo.table(os.environ["TENANT_TABLE_NAME"])
    while True:
        page = table.query(**request)
        yield from page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _locations(tenant_id):
    for row in _query(os.environ["LOCATION_TABLE_NAME"], Key("PK").eq(f"TENANT#{tenant_id}")):
        sk = str(row.get("SK", ""))
        if sk.startswith("LOCATION#"):
            yield row.get("locationId") or sk.removeprefix("LOCATION#")


def run_tenant(tenant_id, policy, now, counts, budget):
    today = now.date()
    for location_id in _locations(tenant_id):
        _bookings(location_id, policy, today, now, counts, budget)
        _orders(location_id, policy, today, now, counts, budget)
        _catering(location_id, policy, today, now, counts, budget)
    _consent(tenant_id, policy, now, counts, budget)
    _subscribers(tenant_id, policy, now, counts, budget)


def _continue(context, event):
    arn = getattr(context, "invoked_function_arn", None)
    if not arn:
        return False
    _client("lambda").invoke(FunctionName=arn, InvocationType="Event", Payload=json.dumps(event).encode())
    return True


def _notify(summary):
    topic = os.environ.get("ALERT_TOPIC_ARN")
    if not topic or not summary["failed"]:
        return
    env_name = os.environ.get("ENVIRONMENT", "dev")
    lines = [f"FAILED: {f['tenantId']}: {f['error']} (the next run tries again)" for f in summary["failed"]]
    try:
        _client("sns").publish(TopicArn=topic, Subject=f"{env_name}: data retention FAILED"[:100],
                               Message="\n".join(lines))
    except ClientError:
        logger.exception("could not send the failure email")


def handler(event, context):
    event = event if isinstance(event, dict) else {}
    budget = Budget(context)
    now = now_utc()
    policy = retention.policy()
    counts = Counter()
    after = event.get("after") or ""
    summary = {"dryRun": dry_run(), "tenants": 0, "failed": [], "continued": False}
    ids = sorted(t["tenantId"] for t in _tenants() if t.get("tenantId") and t.get("status") != "purged")
    for tenant_id in ids:
        if tenant_id <= after:
            continue
        try:
            budget.check()
            run_tenant(tenant_id, policy, now, counts, budget)
            summary["tenants"] += 1
            after = tenant_id
        except OutOfTime:
            round_ = int(event.get("round", 0)) + 1
            if round_ <= MAX_ROUNDS:
                # The unfinished tenant is redone from the start: every step is idempotent.
                summary["continued"] = _continue(context, {"after": after, "round": round_})
            else:
                summary["failed"].append({"tenantId": tenant_id, "error": "ran out of time"})
            break
        except Exception as exc:  # one tenant's failure must not stop the others
            logger.exception("retention failed for %s", tenant_id)
            summary["failed"].append({"tenantId": tenant_id, "error": type(exc).__name__})
            after = tenant_id
    summary["counts"] = dict(sorted(counts.items()))
    _notify(summary)
    logger.info(json.dumps({"retention": summary}))
    return summary
