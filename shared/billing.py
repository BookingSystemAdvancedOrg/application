"""The restaurants' SaaS subscriptions (Stripe Billing on the PLATFORM
account) as the webhook sees them: what to store on the tenant's
PROFILE.billing and how an invoice becomes revenue in the finance table.

Booqy Console (booqy-console/api, billing.py) creates the Stripe customer,
keeps the agreement (terms, prices, start and end) on PROFILE.billing and
builds one invoice per restaurant and calendar month, in advance (the card
on file is charged automatically, else the invoice is emailed);
platform-stripe-webhook keeps the ledger, the past-due state and the card
truthful through this module, and alerts the operators at once when a card
payment fails.

    TENANT#<id> / PROFILE.billing   customerId, pastDueSince, pastDueInvoices
                                    (string set), lastPaidAt,
                                    lastPaymentFailedAt, defaultPaymentMethod,
                                    cardBrand, cardLast4, cardAddedAt,
                                    cardEventCreated (this module); the
                                    agreement fields (console)
    STRIPE_CUSTOMER#<cus> / TENANT  tenantId (written by the console)

Finance table (shared/finance.py has the day rows):
    TENANT#<id> / INVOICE#<yyyy-mm-dd>#<in_...>   one invoice, overwritten by
                                                  every newer event about it
    TENANT#<id> / CREDIT#<yyyy-mm-dd>#<cn_...>    one credit note
    TENANT#<id> / DAY#<yyyy-mm-dd>                invoicedSek / paidSek /
                                                  creditedSek / refundedSek
                                                  (ADD, ex VAT; refundedSek =
                                                  paid back to the card, so
                                                  collected = paid - refunded)

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

def mark_payment(tenant_id, customer_id, *, paid, when, invoice_id=None):
    """A failed payment starts the past-due clock (if it isn't running) and
    remembers the invoice; invoice.paid forgets that invoice and clears past
    due only when no other failed invoice is still unpaid. The webhook only
    calls the failed side for an event that is not older than what the
    ledger has (a late payment_failed never re-marks a paid invoice)."""
    key = {"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"}
    names = {"#b": "billing", "#c": "customerId", "#l": "lastPaidAt" if paid else "lastPaymentFailedAt"}
    if not paid:
        names["#p"] = "pastDueSince"
    if invoice_id:
        names["#i"] = "pastDueInvoices"
    values = {":cus": customer_id, ":w": when}
    if paid:
        expression = "SET #b.#l = :w"
        if invoice_id:
            expression += " DELETE #b.#i :inv"
            values[":inv"] = {invoice_id}
    else:
        expression = "SET #b.#l = :w, #b.#p = if_not_exists(#b.#p, :w)"
        if invoice_id:
            expression += " ADD #b.#i :inv"
            values[":inv"] = {invoice_id}
    try:
        _tenant_table().update_item(
            Key=key, UpdateExpression=expression, ConditionExpression="#b.#c = :cus",
            ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        if paid:
            # Past due only ends with the last failed invoice paid.
            _tenant_table().update_item(
                Key=key, UpdateExpression="REMOVE #b.#p",
                ConditionExpression="#b.#c = :cus AND attribute_not_exists(#b.#i)",
                ExpressionAttributeNames={"#b": "billing", "#c": "customerId", "#p": "pastDueSince",
                                          "#i": "pastDueInvoices"},
                ExpressionAttributeValues={":cus": customer_id})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def forget_failed_invoice(tenant_id, customer_id, invoice_id):
    """A voided (or uncollectible) invoice no longer keeps the customer past
    due on the profile: it leaves pastDueInvoices, and past due clears when
    no other failed invoice is left (the ledger still lists an uncollectible
    invoice as owed, for the console and the reminders)."""
    key = {"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"}
    names = {"#b": "billing", "#c": "customerId", "#p": "pastDueSince", "#i": "pastDueInvoices"}
    try:
        _tenant_table().update_item(
            Key=key, UpdateExpression="DELETE #b.#i :inv", ConditionExpression="#b.#c = :cus",
            ExpressionAttributeNames={"#b": "billing", "#c": "customerId", "#i": "pastDueInvoices"},
            ExpressionAttributeValues={":cus": customer_id, ":inv": {invoice_id}})
        _tenant_table().update_item(
            Key=key, UpdateExpression="REMOVE #b.#p",
            ConditionExpression="#b.#c = :cus AND attribute_exists(#b.#p) AND attribute_not_exists(#b.#i)",
            ExpressionAttributeNames=names, ExpressionAttributeValues={":cus": customer_id})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def record_payment_method(tenant_id, customer_id, payment_method, event_created=None):
    """The customer's default card (customer.updated or a finished Checkout
    setup): its id, brand and last four digits on PROFILE.billing; None
    removes it (the card was detached). With `event_created`, an event older
    than the last one applied (cardEventCreated) changes nothing: Stripe does
    not guarantee order."""
    names = {"#b": "billing", "#c": "customerId"}
    if payment_method is None:
        expression = "REMOVE #b.defaultPaymentMethod, #b.cardBrand, #b.cardLast4"
        values = {":cus": customer_id}
    else:
        card = payment_method.get("card") or {}
        expression = ("SET #b.defaultPaymentMethod = :pm, #b.cardBrand = :brand, #b.cardLast4 = :last4, "
                      "#b.cardAddedAt = if_not_exists(#b.cardAddedAt, :now)")
        values = {":cus": customer_id, ":pm": payment_method["id"], ":brand": card.get("brand") or "card",
                  ":last4": card.get("last4") or "", ":now": now_iso()}
    condition = "#b.#c = :cus"
    if isinstance(event_created, int):
        names["#e"] = "cardEventCreated"
        values[":ec"] = event_created
        condition += " AND (attribute_not_exists(#b.#e) OR #b.#e <= :ec)"
        expression = (expression.replace("REMOVE", "SET #b.#e = :ec REMOVE", 1) if payment_method is None
                      else expression + ", #b.#e = :ec")
    try:
        _tenant_table().update_item(
            Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"}, UpdateExpression=expression,
            ConditionExpression=condition, ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def claim_alert(tenant_id, invoice, step):
    """Marks one operator alert for an invoice as sent, once (opsAlert<step>At
    on the ledger row): a redelivered event never alerts twice."""
    row = invoice_row(tenant_id, invoice, 0)
    field = f"opsAlert{step}At"
    try:
        _finance_table().update_item(
            Key={"PK": row["PK"], "SK": row["SK"]}, UpdateExpression="SET #f = :n",
            ConditionExpression="attribute_exists(PK) AND attribute_not_exists(#f)",
            ExpressionAttributeNames={"#f": field}, ExpressionAttributeValues={":n": now_iso()})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def unclaim_alert(tenant_id, invoice, step):
    row = invoice_row(tenant_id, invoice, 0)
    _finance_table().update_item(Key={"PK": row["PK"], "SK": row["SK"]}, UpdateExpression="REMOVE #f",
                                 ExpressionAttributeNames={"#f": f"opsAlert{step}At"})


def tenant_name(tenant_id):
    item = _tenant_table().get_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"},
                                    ProjectionExpression="#n, slug", ExpressionAttributeNames={"#n": "name"}).get("Item")
    return (item or {}).get("name") or tenant_id, (item or {}).get("slug")


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


def due_date_of(invoice):
    """The due date: Stripe's own for an emailed invoice; a card invoice
    (charge_automatically) has none, so the day it was finalized (charged):
    reminders and the console count the days late from it."""
    due = _iso(invoice.get("due_date"))
    if due or invoice.get("collection_method") != "charge_automatically":
        return due
    return _iso((invoice.get("status_transitions") or {}).get("finalized_at"))


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
        "dueDate": due_date_of(invoice),
        # The console puts the billed period (a calendar month, or part of
        # one) in the metadata; Stripe's own period_start/end of a one-off
        # invoice is its creation.
        "kind": metadata.get("kind"),
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


def refunded_minor(note):
    """The part of a credit note paid back to the card (öre, incl VAT): the
    rest of it was credited (reduced the invoice, or went to the customer's
    balance for the next one). Booqy Console reads it the same way."""
    total = int(note.get("total") or 0)
    refunds = note.get("refunds")
    if isinstance(refunds, list) and refunds:
        amount = sum(int(r.get("amount_refunded") or 0) for r in refunds if isinstance(r, dict))
    elif note.get("refund"):
        amount = total - int(note.get("out_of_band_amount") or 0)
    else:
        amount = 0
    return max(0, min(amount, total))


def credit_note_row(tenant_id, note, event_created):
    """The CREDIT# ledger row of a credit note (without the booking flags).
    Booqy Console (booqy-console/api billing.py) writes the same row when an
    operator issues the note, so whichever of the two comes first books."""
    day = _day(note.get("created")) or finance.day_key()
    ex = note.get("total_excluding_tax")
    if ex is None:
        ex = note.get("subtotal")
    total = int(note.get("total") or 0)
    refunded = refunded_minor(note)
    refunded_ex = (Decimal(refunded) * Decimal(int(ex or 0)) / Decimal(total) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP) if total > 0 else Decimal("0.00")
    metadata = note.get("metadata") or {}
    return {"PK": finance.tenant_pk(tenant_id), "SK": f"CREDIT#{day}#{note['id']}", "day": day,
            "creditNoteId": note["id"], "invoiceId": _id(note.get("invoice")), "number": note.get("number"),
            "reason": note.get("reason"), "amountSek": sek(ex or 0), "totalSek": sek(total),
            "refundedSek": sek(refunded), "refundedExSek": refunded_ex,
            "postPaymentSek": sek(post_payment_minor(note)),
            "method": metadata.get("booqyMethod"), "memo": note.get("memo"),
            "createdAt": _iso(note.get("created")), "eventCreated": int(event_created), "updatedAt": now_iso()}


def post_payment_minor(note):
    """What a credit note took back from money already paid (öre, incl VAT);
    the rest only lowered what was still owed. The console reads it to say
    what is left to refund on the invoice."""
    if isinstance(note.get("post_payment_amount"), int):
        return note["post_payment_amount"]
    if note.get("type") == "pre_payment":
        return 0
    return int(note.get("total") or 0)


def record_credit_note(tenant_id, note, event_created):
    """A credit note reduces revenue on the day it was issued (creditedSek),
    and the part refunded to the card reduces what was collected
    (refundedSek). The row is written once; each booking is fenced by its
    own flag, so a redelivery (or the console having written the row first)
    never counts twice, and a booking interrupted after the put is retried."""
    if not isinstance(note.get("id"), str) or not note["id"].startswith("cn_"):
        return "ignored:no_credit_note"
    row = credit_note_row(tenant_id, note, event_created)
    day = row.pop("day")
    fresh = True
    try:
        _finance_table().put_item(Item={k: v for k, v in row.items() if v is not None},
                                  ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        fresh = False
    key = {"PK": row["PK"], "SK": row["SK"]}
    if not fresh:   # a row written before postPaymentSek existed gets it now
        _finance_table().update_item(Key=key, UpdateExpression="SET postPaymentSek = if_not_exists(postPaymentSek, :p)",
                                     ExpressionAttributeValues={":p": row["postPaymentSek"]})
    booked = []
    if _book(key, "bookedCredited", tenant_id, day, "creditedSek", row["amountSek"]):
        booked.append("credited")
    if _book(key, "bookedRefunded", tenant_id, day, "refundedSek", row["refundedExSek"]):
        booked.append("refunded")
    return ("applied" if fresh else "stale") + (":" + "+".join(booked) if booked else "")
