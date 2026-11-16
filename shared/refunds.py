"""Restaurant refunds to guests: one set of rules for booking fees, food
orders and catering, so money moves exactly once.

RULES
    * Owner only (each caller checks ctx.is_owner before calling).
    * Amount in öre: at least 1, at most what is left (cap minus what is
      already refunded); left out = what is left.
    * Reserve, then Stripe, then record:
        1. reserve - one conditional write puts a pending refund {n, amount,
           key, at, by, reason, notify} on the item and bumps `count`, only
           while nothing else is pending and the refund state is the one that
           was read (its `rev`). Two clicks or two people at once: the second
           gets 409 refund_in_progress / changed_retry.
        2. Stripe - POST /v1/refunds with the explicit amount and the key
           <prefix>-<n>: unique per intended refund, never reused for another
           amount. Metadata carries refundKey so a replay can find it.
        3. record - while pending.n is still n: total += amount, the refund
           appended to `items`, pending cleared, plus the domain's own sets
           (status, history, guest notice), one conditional write.
    * Stripe says no (4xx): the pending refund is dropped (its key is burnt,
      the next refund uses n + 1) and the error goes back.
    * Stripe unreachable: the pending refund stays. The charge.refunded
      webhook completes it if Stripe did refund; a request after
      STALE_SECONDS replays it (same key and amount, first looked up by
      refundKey so it is safe after Stripe's 24 h key window) and answers
      409 changed_retry, so the screen reloads instead of refunding again.
    * charge.refunded (Stripe's cumulative amount_refunded T, our total R):
      T <= R is already recorded. A pending refund of amount a with T - R >= a
      is ours: completed (with its notice) and anything beyond it is recorded
      as an external refund (Stripe Dashboard). T - R < a: not ours yet, wait
      for the event that includes it. No pending: the difference is external.
      Totals always end at Stripe's amount_refunded and nothing counts twice.

STATE (normalised; each domain maps it onto its own attributes)
    {"total": int, "count": int, "rev": int, "pending": dict | None, "items": list}
"""

from datetime import datetime, timezone

from shared import stripe_client as stripe

STALE_SECONDS = 120
MAX_REASON = 200


class Conflict(Exception):
    """409 with ``code``: refund_in_progress, changed_retry, nothing_to_refund."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Changed(Exception):
    """A domain write lost its condition (the state moved on)."""


def _iso(at):
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_seconds(pending, now):
    try:
        at = datetime.fromisoformat(str(pending.get("at")).replace("Z", "+00:00"))
    except ValueError:
        return STALE_SECONDS
    return (now - at).total_seconds()


def parse_amount(value, remaining):
    """The requested amount in öre, or what is left when left out."""
    if remaining <= 0:
        raise Conflict("nothing_to_refund")
    if value is None:
        return remaining
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= remaining:
        raise ValueError(f"amount must be a whole number of öre between 1 and {remaining}")
    return value


def parse_reason(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value.strip()) > MAX_REASON:
        raise ValueError(f"reason must be text of at most {MAX_REASON} characters")
    return value.strip() or None


def parse_notify(value):
    if value is None:
        return True
    if not isinstance(value, bool):
        raise ValueError("notifyGuest must be true or false")
    return value


def normal(total=0, count=0, rev=0, pending=None, items=None):
    return {"total": int(total or 0), "count": int(count or 0), "rev": int(rev or 0),
            "pending": pending or None, "items": list(items or [])}


def entry_of(pending, refund_id, now):
    entry = {"n": int(pending["n"]), "amount": int(pending["amount"]), "refundId": refund_id,
             "at": _iso(now), "by": pending.get("by"), "reason": pending.get("reason"),
             "notify": bool(pending.get("notify")), "manual": True if pending.get("manual") else None}
    return {k: v for k, v in entry.items() if v is not None}


# --- Stripe ---------------------------------------------------------------------------------

def stripe_create(account, payment_intent, pending, metadata, *, replay=False):
    """POST /v1/refunds for a pending refund. On a replay the refund is first
    looked up by its refundKey (it may exist although we never heard back)."""
    key = pending["key"]
    if replay:
        found = stripe.request("GET", "/v1/refunds", {"payment_intent": payment_intent, "limit": 100},
                               account=account)
        for obj in (found or {}).get("data") or []:
            if ((obj.get("metadata") or {}).get("refundKey") == key
                    and obj.get("status") not in ("failed", "canceled")):
                return obj
    params = {"payment_intent": payment_intent, "amount": int(pending["amount"]),
              "reason": "requested_by_customer", "metadata": {**metadata, "refundKey": key}}
    return stripe.request("POST", "/v1/refunds", params, account=account, idempotency_key=key)


# --- the flow -------------------------------------------------------------------------------

class Domain:
    """What a domain provides. ``write`` is ONE conditional write on
    old["rev"] (and pending as read) that stores ``new`` plus the domain's
    own sets for ``completed`` refunds (history, notice, status); it raises
    Changed when the condition fails and returns the updated item."""

    prefix = "refund"

    def state(self, item):  # pragma: no cover - interface
        raise NotImplementedError

    def write(self, item, old, new, completed, now):  # pragma: no cover - interface
        raise NotImplementedError

    def reload(self, item):  # pragma: no cover - interface
        raise NotImplementedError

    def create(self, item, pending, *, replay=False):  # pragma: no cover - interface
        """The Stripe refund object, or {} when no money moves through Stripe
        (paid out of band: recorded as a manual refund owed)."""
        raise NotImplementedError


def start(domain, item, *, amount, cap, actor, now, reason=None, notify=True, key=None, extra=None):
    """Reserve and run one refund. Returns (item, entry) where entry is the
    completed refund (or None when the webhook completed it first)."""
    st = domain.state(item)
    if st["pending"]:
        if _age_seconds(st["pending"], now) < STALE_SECONDS:
            raise Conflict("refund_in_progress")
        finish(domain, item, st["pending"], now, replay=True)
        raise Conflict("changed_retry")
    amount = parse_amount(amount, int(cap) - st["total"])
    n = st["count"] + 1
    pending = {"n": n, "amount": amount, "key": key or f"{domain.prefix}-{n}", "at": _iso(now),
               "by": actor, "notify": bool(notify)}
    if reason:
        pending["reason"] = reason
    pending.update(extra or {})
    new = {**st, "pending": pending, "count": n, "rev": st["rev"] + 1}
    try:
        item = domain.write(item, st, new, [], now)
    except Changed:
        raise Conflict("changed_retry") from None
    return finish(domain, item, pending, now)


def finish(domain, item, pending, now, *, replay=False):
    try:
        obj = domain.create(item, pending, replay=replay)
    except stripe.StripeError:
        drop(domain, item, pending, now)
        raise
    return complete(domain, item, pending, (obj or {}).get("id"), now)


def drop(domain, item, pending, now):
    for _ in range(4):
        st = domain.state(item)
        if not st["pending"] or int(st["pending"]["n"]) != int(pending["n"]):
            return item
        try:
            return domain.write(item, st, {**st, "pending": None, "rev": st["rev"] + 1}, [], now)
        except Changed:
            item = domain.reload(item)
    return item


def complete(domain, item, pending, refund_id, now):
    for _ in range(4):
        st = domain.state(item)
        if not st["pending"] or int(st["pending"]["n"]) != int(pending["n"]):
            return item, None  # the webhook recorded it first
        entry = entry_of(pending, refund_id, now)
        new = {**st, "total": st["total"] + entry["amount"], "items": st["items"] + [entry],
               "pending": None, "rev": st["rev"] + 1}
        try:
            return domain.write(item, st, new, [entry], now), entry
        except Changed:
            item = domain.reload(item)
    raise Conflict("changed_retry")


def from_stripe(domain, item, stripe_total, now, refund_id=None):
    """charge.refunded: bring our total up to Stripe's amount_refunded.
    Returns (item, result)."""
    stripe_total = int(stripe_total or 0)
    for _ in range(4):
        st = domain.state(item)
        if stripe_total <= st["total"]:
            return item, "already_recorded"
        extra = stripe_total - st["total"]
        entries = []
        pending = st["pending"]
        if pending:
            if extra < int(pending["amount"]):
                return item, "pending"
            entries.append(entry_of(pending, None, now))
            extra -= int(pending["amount"])
        if extra:
            entries.append({"amount": extra, "at": _iso(now), "by": "stripe", "external": True,
                            **({"refundId": refund_id} if refund_id else {})})
        new = {**st, "total": stripe_total, "items": st["items"] + entries, "pending": None,
               "rev": st["rev"] + 1}
        try:
            return domain.write(item, st, new, entries, now), "recorded"
        except Changed:
            item = domain.reload(item)
    raise Changed("refund state kept changing")
