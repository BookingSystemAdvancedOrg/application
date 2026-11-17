"""Catering refunds (shared/refunds.py rules) on the REQUEST# head item.

    refundedAmount   öre refunded so far (Stripe and manual)
    refundCount      refunds Booqy started (key cat-refund-<requestId>-<n>)
    refundRev        bumped on every refund write
    refundPending    the refund in flight {n, amount, key, at, by, reason?,
                     notify, paymentIntent? | manual?}
    refunds          [{n?, amount, at, by, reason?, refundId?, manual?,
                     external?, cancel?}] - one row per refund, the
                     bookkeeping reverses each on its own day

Money source per request:
    private Checkout            the request's paymentIntentId
    company invoice, by card    the invoice's PaymentIntent
    company invoice, Bankgiro   no Stripe call: a manual refund owed (the
                                restaurant pays back by hand; booked as a
                                debt until then)

Every write also goes through head_update (status + version), so a refund
never crosses another change to the request, and an AUDIT# "refunded" row
is written with each completed refund.
"""

from shared import catering as c
from shared import catering_stripe as cs
from shared import refunds

REFUNDABLE = (c.CONFIRMED, c.DELIVERED, c.CLOSED)


def signed_total(item):
    """The signed offer's total (öre): what can be refunded at most."""
    version = item.get("signedOfferVersion")
    if not version:
        return 0
    try:
        offer = c.load_offer(item["requestId"], int(version))
    except c.NotFound:
        return 0
    return int((offer.get("totals") or {}).get("total") or 0)


def refundable(item, total=None, statuses=REFUNDABLE):
    if not item.get("paid") or item.get("status") not in statuses:
        return 0
    total = signed_total(item) if total is None else total
    return max(int(total) - int(item.get("refundedAmount") or 0), 0)


class _Catering(refunds.Domain):
    def __init__(self, item, account=None, *, cancel=False):
        self.prefix = f"cat-refund-{item['requestId']}"
        self.account = account or item.get("stripeAccountId")
        self.cancel = cancel

    def state(self, item):
        total = int(item.get("refundedAmount") or 0)
        rows = list(item.get("refunds") or [])
        if total and not rows:  # recorded by the webhook before the list existed
            rows = [{"amount": total, "at": item.get("updatedAt"), "by": "stripe", "external": True}]
        return refunds.normal(total, item.get("refundCount"), item.get("refundRev"), item.get("refundPending"), rows)

    def reload(self, item):
        return c.load(item["locationId"], item["requestId"])

    def create(self, item, pending, *, replay=False):
        if pending.get("manual"):
            return {}
        return refunds.stripe_create(self.account, pending["paymentIntent"], pending,
                                     {"requestId": item["requestId"], "locationId": item["locationId"],
                                      "kind": "catering_refund"}, replay=replay)

    def write(self, item, old, new, completed, now):
        sets = ["refundedAmount = :ra", "refundCount = :rc", "refundRev = :rr", "refunds = :rl",
                "updatedAt = :now"]
        values = {":ra": new["total"], ":rc": new["count"], ":rr": new["rev"], ":rl": new["items"],
                  ":now": c.iso(now)}
        removes = []
        if new["pending"]:
            sets.append("refundPending = :rp")
            values[":rp"] = new["pending"]
        else:
            removes.append("refundPending")
        ops = []
        for entry in completed:
            if self.cancel:
                entry["cancel"] = True
            ops.append(c.audit_put(item["requestId"], "refunded", entry.get("by") or "stripe", now,
                                   amount=entry["amount"], reason=entry.get("reason"),
                                   manual=True if entry.get("manual") else None,
                                   external=True if entry.get("external") else None))
        told = [e for e in completed if e.get("notify")]
        if told and not self.cancel and (item.get("customerEmail") or item.get("customerPhone")):
            sets.append("notice = :n")
            values[":n"] = c.notice(c.N_REFUNDED, now, amount=told[-1]["amount"],
                                    **({"reason": told[-1]["reason"]} if told[-1].get("reason") else {}),
                                    **({"manual": True} if told[-1].get("manual") else {}))
        try:
            c.transact([c.head_update(item, sets=sets, values=values, removes=removes)] + ops)
        except c.InvalidState:
            raise refunds.Changed() from None
        return c.load(item["locationId"], item["requestId"])


def _source(item, account):
    """{"paymentIntent": pi} or {"manual": True}; InvalidState when the
    request was not paid in a way that can be refunded."""
    if item.get("customerType") == c.PRIVATE:
        pi = item.get("paymentIntentId")
        if not pi or pi == "-":
            raise c.InvalidState("not_refundable")
        return {"paymentIntent": pi}
    if not item.get("stripeInvoiceId"):
        raise c.InvalidState("not_refundable")
    invoice = cs.get_invoice(account, item["stripeInvoiceId"])
    if invoice.get("status") != "paid":
        raise c.InvalidState("not_paid")
    pi = cs.invoice_payment_intent(account, invoice)
    return {"paymentIntent": pi} if pi else {"manual": True}


def refund(item, account, amount, now, actor, *, reason=None, notify=True, cancel=False, statuses=REFUNDABLE):
    """(Part of) a paid request back without cancelling it (cancel=True: the
    rest, as part of a cancellation, without its own guest notice).
    Returns (item, entry)."""
    if not item.get("paid"):
        raise c.InvalidState("not_paid")
    if item.get("status") not in statuses:
        raise c.InvalidState("not_refundable")
    pending = item.get("refundPending") or {}
    source = {k: pending[k] for k in ("paymentIntent", "manual") if k in pending} or _source(item, account)
    domain = _Catering(item, account, cancel=cancel)
    return refunds.start(domain, item, amount=amount, cap=signed_total(item), actor=actor, now=now,
                         reason=reason, notify=notify, extra=source)


def record_stripe_refund(item, amount_refunded, now):
    """charge.refunded on the request's payment: (item, result)."""
    return refunds.from_stripe(_Catering(item), item, amount_refunded, now)
