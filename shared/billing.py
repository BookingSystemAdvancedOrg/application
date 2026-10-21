"""The restaurants' SaaS subscriptions (Stripe Billing on the PLATFORM
account) as the webhook sees them: what to store on the tenant's
PROFILE.billing and how an invoice becomes revenue in the finance table.

Booqy Console (booqy-console/api, billing.py) creates the Stripe customer,
keeps the agreement (terms, prices, start and end) on PROFILE.billing and
builds one invoice per restaurant and period (25th to 24th) in arrears;
platform-stripe-webhook keeps the ledger and the past-due state truthful
through this module.

    TENANT#<id> / PROFILE.billing   customerId, pastDueSince, lastPaidAt,
                                    lastPaymentFailedAt (this module); the
                                    agreement fields (console)
    STRIPE_CUSTOMER#<cus> / TENANT  tenantId (written by the console)

Finance table (shared/finance.py has the day rows):
    TENANT#<id> / INVOICE#<yyyy-mm-dd>#<in_...>   one invoice, overwritten by
                                                  every newer event about it
    TENANT#<id> / CREDIT#<yyyy-mm-dd>#<cn_...>    one credit note
    TENANT#<id> / DAY#<yyyy-mm-dd>                invoicedSek / paidSek /
                                                  creditedSek (ADD, ex VAT)

Every amount is in SEK excluding VAT: the Finance page compares revenue
with AWS cost, and VAT is not revenue. Revenue is booked once per invoice
and event kind (flags on the ledger row inside the same transaction as the
ADD), so a redelivered event never double counts.
"""

import os
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from botocore.exceptions import ClientError

from shared import dynamo
from shared import finance



def _tenant_table():
    return dynamo.table(os.environ["TENANT_TABLE_NAME"])


def _finance_table():
    return dynamo.table(os.environ["FINANCE_TABLE_NAME"])


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso(ts):
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _day(ts):
    iso = _iso(ts)
    return iso[:10] if iso else None


def sek(minor):
    """Stripe minor units (öre) -> SEK with two decimals."""
    if not isinstance(minor, (int, float)):
        return Decimal("0.00")
    return (Decimal(int(minor)) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _id(value):
    """Stripe fields that may be an id or an expanded object."""
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) and value else None


def tenant_of_customer(customer_id):
    if not isinstance(customer_id, str) or not customer_id.startswith("cus_"):
        return None
    item = _tenant_table().get_item(
        Key={"PK": f"STRIPE_CUSTOMER#{customer_id}", "SK": "TENANT"}, ConsistentRead=True).get("Item")
    tenant_id = (item or {}).get("tenantId")
    return tenant_id if isinstance(tenant_id, str) and tenant_id else None


# --- past due on the profile ------------------------------------------------------------

def mark_payment(tenant_id, customer_id, *, paid, when):
    """invoice.paid clears past due (and remembers the date); a failed
    payment starts the past-due clock if it isn't running."""
    names = {"#b": "billing", "#c": "customerId", "#p": "pastDueSince",
             "#l": "lastPaidAt" if paid else "lastPaymentFailedAt"}
    expression = "SET #b.#l = :w" + (" REMOVE #b.#p" if paid else ", #b.#p = if_not_exists(#b.#p, :w)")
    try:
        _tenant_table().update_item(
            Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"},
            UpdateExpression=expression, ConditionExpression="#b.#c = :cus",
            ExpressionAttributeNames=names, ExpressionAttributeValues={":cus": customer_id, ":w": when})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


# --- invoices and revenue ---------------------------------------------------------------

def _ex_vat(amount_minor, invoice):
    """Share of an amount that is revenue: total excluding tax over total."""
    total = invoice.get("total")
    ex = invoice.get("total_excluding_tax")
    if ex is None:
        ex = (invoice.get("total") or 0) - (invoice.get("tax") or 0)
    if not isinstance(total, (int, float)) or total <= 0:
        return sek(amount_minor)
    return (Decimal(int(amount_minor or 0)) * Decimal(int(ex)) / Decimal(int(total)) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def invoice_row(tenant_id, invoice, event_created):
    """The ledger row for an invoice (without the booking flags)."""
    transitions = invoice.get("status_transitions") or {}
    metadata = invoice.get("metadata") or {}
    created_day = _day(invoice.get("created")) or finance.day_key()
    return {
        "PK": finance.tenant_pk(tenant_id), "SK": f"INVOICE#{created_day}#{invoice['id']}",
        "invoiceId": invoice["id"], "number": invoice.get("number"), "status": invoice.get("status"),
        "customerId": _id(invoice.get("customer")), "subscriptionId": _id(invoice.get("subscription")),
        "currency": (invoice.get("currency") or "sek").lower(),
        "subtotalSek": _ex_vat(invoice.get("total"), invoice),
        "taxSek": sek(invoice.get("tax") or 0),
        "totalSek": sek(invoice.get("total")),
        "amountPaidSek": sek(invoice.get("amount_paid") or 0),
        "amountDueSek": sek(invoice.get("amount_due") or 0),
        "amountRemainingSek": sek(invoice.get("amount_remaining") or 0),
        "createdAt": _iso(invoice.get("created")),
        "finalizedAt": _iso(transitions.get("finalized_at")),
        "paidAt": _iso(transitions.get("paid_at")),
        "voidedAt": _iso(transitions.get("voided_at")),
        "uncollectibleAt": _iso(transitions.get("marked_uncollectible_at")),
        "dueDate": _iso(invoice.get("due_date")),
        # The console puts the billed period (25th to 24th) in the metadata;
        # Stripe's own period_start/end of a one-off invoice is its creation.
        "periodStart": metadata.get("periodStart") or _iso(invoice.get("period_start")),
        "periodEnd": metadata.get("periodEnd") or _iso(invoice.get("period_end")),
        "hostedInvoiceUrl": invoice.get("hosted_invoice_url"),
        "invoicePdf": invoice.get("invoice_pdf"),
        "paidOutOfBand": bool(invoice.get("paid_out_of_band")),
        "collectionMethod": invoice.get("collection_method"),
        "eventCreated": int(event_created),
        "updatedAt": now_iso(),
    }


def _upsert_invoice(row):
    names = {"#e": "eventCreated"}
    values = {":c": row["eventCreated"]}
    sets = []
    for i, (key, value) in enumerate(sorted(row.items())):
        if key in ("PK", "SK") or value is None:
            continue
        names[f"#f{i}"] = key
        values[f":v{i}"] = value
        sets.append(f"#f{i} = :v{i}")
    try:
        _finance_table().update_item(
            Key={"PK": row["PK"], "SK": row["SK"]}, UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_not_exists(#e) OR #e <= :c",
            ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _book(row_key, flag, tenant_id, day, field, amount):
    """One ADD on the day row, fenced by a flag on the ledger row: the same
    revenue is never booked twice."""
    if not amount:
        return False
    table = os.environ["FINANCE_TABLE_NAME"]
    try:
        dynamo.client().transact_write_items(TransactItems=[
            {"Update": {"TableName": table,
                        "Key": {"PK": {"S": row_key["PK"]}, "SK": {"S": row_key["SK"]}},
                        "UpdateExpression": "SET #f = :d",
                        "ConditionExpression": "attribute_exists(PK) AND attribute_not_exists(#f)",
                        "ExpressionAttributeNames": {"#f": flag},
                        "ExpressionAttributeValues": {":d": {"S": day}}}},
            {"Update": {"TableName": table,
                        "Key": {"PK": {"S": finance.tenant_pk(tenant_id)}, "SK": {"S": finance.day_sk(day)}},
                        "UpdateExpression": "ADD #a :a SET bookedAt = :now",
                        "ExpressionAttributeNames": {"#a": field},
                        "ExpressionAttributeValues": {":a": {"N": str(amount)}, ":now": {"S": now_iso()}}}},
        ])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            return False
        raise
    return True


def record_invoice(tenant_id, invoice, event_created):
    """Upserts the ledger row and books revenue: the ex-VAT total when the
    invoice is finalized, what was paid when it is paid, and the reversal
    when it is voided. Returns what happened."""
    if not isinstance(invoice.get("id"), str) or not invoice["id"].startswith("in_"):
        return "ignored:no_invoice"
    row = invoice_row(tenant_id, invoice, event_created)
    key = {"PK": row["PK"], "SK": row["SK"]}
    fresh = _upsert_invoice(row)
    booked = []
    status = row["status"]
    if status in ("open", "paid", "uncollectible", "void"):
        day = (row["finalizedAt"] or row["createdAt"] or now_iso())[:10]
        if _book(key, "bookedInvoiced", tenant_id, day, "invoicedSek", row["subtotalSek"]):
            booked.append("invoiced")
    if status == "paid" and row["amountPaidSek"]:
        day = (row["paidAt"] or now_iso())[:10]
        paid_ex_vat = _ex_vat(invoice.get("amount_paid"), invoice)
        if _book(key, "bookedPaid", tenant_id, day, "paidSek", paid_ex_vat):
            booked.append("paid")
    if status == "void":
        day = (row["voidedAt"] or now_iso())[:10]
        if _book(key, "bookedVoided", tenant_id, day, "invoicedSek", -row["subtotalSek"]):
            booked.append("voided")
    return ("applied" if fresh else "stale") + (":" + "+".join(booked) if booked else "")


def record_credit_note(tenant_id, note, event_created):
    """A credit note reduces revenue on the day it was issued."""
    if not isinstance(note.get("id"), str) or not note["id"].startswith("cn_"):
        return "ignored:no_credit_note"
    day = _day(note.get("created")) or finance.day_key()
    ex = note.get("total_excluding_tax")
    if ex is None:
        ex = note.get("subtotal")
    amount = sek(ex or 0)
    row = {"PK": finance.tenant_pk(tenant_id), "SK": f"CREDIT#{day}#{note['id']}",
           "creditNoteId": note["id"], "invoiceId": _id(note.get("invoice")), "number": note.get("number"),
           "reason": note.get("reason"), "amountSek": amount, "totalSek": sek(note.get("total") or 0),
           "createdAt": _iso(note.get("created")), "eventCreated": int(event_created), "updatedAt": now_iso()}
    try:
        _finance_table().put_item(Item={k: v for k, v in row.items() if v is not None},
                                  ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return "stale"
        raise
    if _book({"PK": row["PK"], "SK": row["SK"]}, "bookedCredited", tenant_id, day, "creditedSek", amount):
        return "applied:credited"
    return "applied"
