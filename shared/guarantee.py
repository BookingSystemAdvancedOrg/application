"""Card guarantee for table bookings: the location's policy, the card-on-file
SetupIntent at booking and the off-session fee charges afterwards.

Policy (location item, `guarantee`, edited by the owner):
    enabled                  bool
    minPartySize             card required from this party size (1 = always)
    noShowFeePerPerson       kr per guest when staff mark a guaranteed no-show
    lateCancelFeePerPerson   kr per guest when the guest cancels inside the cutoff
    cancelCutoffHours        hours before the start after which cancelling costs

The policy applies only when the tenant's Stripe account can take charges.
At booking the fees are SNAPSHOTTED on the reservation (`guarantee`): the
amounts and cutoff the guest accepted are what may be charged later, even
if the owner changes the policy.

Stripe (Connect, direct charges on the restaurant's account):
    booking   Customer + SetupIntent (usage off_session, card) - 0 kr; the
              guest confirms it with the embedded Payment Element (3-D Secure
              happens then, while present)
    confirm   SetupIntent succeeded -> reservation pending -> reserved,
              payment method stored (shared by POST .../confirm and the
              setup_intent.succeeded webhook - whichever comes first)
    charge    PaymentIntent off_session + confirm, idempotency key
              <kind>-<reservationId>-<attempt>: a retried request never
              charges twice; a deliberate retry after a decline is a new
              attempt
    refund    Refund of the fee's PaymentIntent (staff)

Reservation attributes: guarantee (snapshot), stripeAccountId,
stripeCustomerId, setupIntentId, stripePaymentMethodId, payment = {kind,
status, amount, attempt, paymentIntentId, error, chargedAt, refundedAmount}.
"""

from datetime import timedelta
from decimal import Decimal

from shared import dynamo
from shared import reservations as r
from shared import stripe_client as stripe

CURRENCY = "sek"
MAX_FEE_PER_PERSON = 5000
MAX_CUTOFF_HOURS = 168
PENDING_MINUTES = 20

# payment.status
PAY_PENDING = "pending"          # charge started / outcome unknown - retry is safe
PAY_PROCESSING = "processing"    # Stripe is processing; the webhook settles it
PAY_SUCCEEDED = "succeeded"
PAY_FAILED = "failed"
PAY_REFUNDED = "refunded"

KIND_NO_SHOW = "no_show"
KIND_LATE_CANCEL = "late_cancel"
_FINAL_STATUS = {
    (KIND_NO_SHOW, True): r.NO_SHOW_CHARGED,
    (KIND_NO_SHOW, False): r.NO_SHOW_CHARGE_FAILED,
    (KIND_LATE_CANCEL, True): r.CANCELLED_CHARGED,
    (KIND_LATE_CANCEL, False): r.CANCELLED_CHARGE_FAILED,
}
_BASE_STATUS = {KIND_NO_SHOW: r.NO_SHOW, KIND_LATE_CANCEL: r.CANCELLED_BY_GUEST}


# --- policy -------------------------------------------------------------------------

def _int(value, low, high, field):
    if isinstance(value, bool):
        raise ValueError(f"guarantee.{field} must be a whole number")
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError(f"guarantee.{field} must be a whole number")
        value = int(value)
    if not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"guarantee.{field} must be a whole number between {low} and {high}")
    return value


def validate_policy(raw):
    """Owner input -> stored policy (raises ValueError). None removes it."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("guarantee must be an object")
    allowed = {"enabled", "minPartySize", "noShowFeePerPerson", "lateCancelFeePerPerson",
               "cancelCutoffHours"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unsupported guarantee fields: {', '.join(unknown)}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("guarantee.enabled must be true or false")
    policy = {
        "enabled": enabled,
        "minPartySize": _int(raw.get("minPartySize", 1), 1, r.MAX_PARTY_SIZE, "minPartySize"),
        "noShowFeePerPerson": _int(raw.get("noShowFeePerPerson", 0), 0, MAX_FEE_PER_PERSON,
                                   "noShowFeePerPerson"),
        "lateCancelFeePerPerson": _int(raw.get("lateCancelFeePerPerson", 0), 0, MAX_FEE_PER_PERSON,
                                       "lateCancelFeePerPerson"),
        "cancelCutoffHours": _int(raw.get("cancelCutoffHours", 24), 0, MAX_CUTOFF_HOURS,
                                  "cancelCutoffHours"),
    }
    if enabled and not (policy["noShowFeePerPerson"] or policy["lateCancelFeePerPerson"]):
        raise ValueError("an enabled guarantee needs a no-show or late-cancellation fee")
    return policy


def policy_of(location):
    """The location's policy, or None when off / missing / unreadable."""
    try:
        policy = validate_policy((location or {}).get("guarantee"))
    except ValueError:
        return None
    return policy if policy and policy["enabled"] else None


def stripe_account(tenant_row, location=None):
    """The connected account fees go to: the location's own account (a
    separate company in the group) or the tenant's when it can take
    charges. None = no card guarantee possible."""
    own = (location or {}).get("stripeAccountId")
    if isinstance(own, str) and own.startswith("acct_"):
        return own
    stripe_state = (tenant_row or {}).get("stripe") or {}
    account = stripe_state.get("accountId")
    if isinstance(account, str) and account.startswith("acct_") and stripe_state.get("chargesEnabled"):
        return account
    return None


def public_policy(location, tenant_row):
    """What the booking site shows before the guest books (None = no card needed)."""
    policy = policy_of(location)
    if not policy or not stripe_account(tenant_row, location):
        return None
    return {k: policy[k] for k in ("minPartySize", "noShowFeePerPerson", "lateCancelFeePerPerson",
                                   "cancelCutoffHours")}


def required(location, tenant_row, party_size):
    policy = policy_of(location)
    return bool(policy and stripe_account(tenant_row, location) and party_size >= policy["minPartySize"])


def snapshot(location, party_size):
    """The fees the guest accepts at booking, in öre (Stripe's minor unit)."""
    policy = policy_of(location)
    return {
        "currency": CURRENCY,
        "noShowFee": policy["noShowFeePerPerson"] * party_size * 100,
        "lateCancelFee": policy["lateCancelFeePerPerson"] * party_size * 100,
        "noShowFeePerPerson": policy["noShowFeePerPerson"],
        "lateCancelFeePerPerson": policy["lateCancelFeePerPerson"],
        "cancelCutoffHours": policy["cancelCutoffHours"],
    }


def is_guaranteed(item):
    return bool(item.get("stripePaymentMethodId") and item.get("stripeCustomerId")
                and item.get("stripeAccountId") and isinstance(item.get("guarantee"), dict))


def late_cancel_fee(item, now):
    """Öre the guest would be charged for cancelling now (0 = free)."""
    if not is_guaranteed(item):
        return 0
    snap = item["guarantee"]
    fee = int(snap.get("lateCancelFee") or 0)
    if not fee:
        return 0
    cutoff = r.parse_iso(item["bookedFor"]) - timedelta(hours=int(snap.get("cancelCutoffHours") or 0))
    return fee if now >= cutoff else 0


def no_show_fee(item):
    if not is_guaranteed(item):
        return 0
    return int(item["guarantee"].get("noShowFee") or 0)


# --- pending holds ---------------------------------------------------------------------
#
# A pending booking (card not confirmed yet) holds its tables for
# PENDING_MINUTES. A marker row LOCATION#<l> / PENDING#<expiresAt>#<id> lets
# the sweep (reservation-reminders) find the expired ones with one Query per
# location instead of reading every future booking. Confirming or cancelling
# deletes the marker in the same transaction.

def marker_key(location_id, expires_iso, reservation_id):
    return {"PK": r.location_pk(location_id), "SK": f"PENDING#{expires_iso}#{reservation_id}"}


def marker_put(item):
    return {
        "Put": {
            "TableName": r.reservation_table(),
            "Item": r.serialize({
                **marker_key(item["locationId"], item["pendingExpiresAt"], item["reservationId"]),
                "reservationId": item["reservationId"], "date": item["date"],
                "tenantId": item["tenantId"], "ttl": int(r.parse_iso(item["pendingExpiresAt"]).timestamp()) + 86400,
            }),
            "ConditionExpression": "attribute_not_exists(PK)",
        }
    }


def marker_delete_ops(item):
    if not item.get("pendingExpiresAt"):
        return []
    return [{"Delete": {
        "TableName": r.reservation_table(),
        "Key": r.serialize(marker_key(item["locationId"], item["pendingExpiresAt"], item["reservationId"])),
    }}]


def expire_items(item, now):
    """Transaction items: a pending booking whose card was never confirmed
    -> expired, its tables released, its marker gone."""
    entry = r.history_entry("expired", "system", now, status=r.EXPIRED)
    versions = r.read_lock_versions(item["locationId"], item["date"], item.get("tableIds") or [])
    return (r.lock_updates(item["locationId"], item["date"], versions) + r.release_items(item) + [{
        "Update": {
            "TableName": r.reservation_table(),
            "Key": r.serialize(r.reservation_key(item["locationId"], item["date"], item["reservationId"])),
            "UpdateExpression": ("SET #s = :expired, holdsReleased = :true, updatedAt = :now, "
                                 "updatedBy = :by, history = list_append(if_not_exists(history, :empty), :entry), "
                                 "version = if_not_exists(version, :zero) + :one REMOVE pendingExpiresAt"),
            "ConditionExpression": "#s = :pending",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": r.serialize({
                ":expired": r.EXPIRED, ":pending": r.PENDING, ":true": True, ":now": r.iso(now),
                ":by": "system", ":entry": [entry], ":empty": [], ":zero": 0, ":one": 1,
            }),
        }
    }] + marker_delete_ops(item))


# --- Stripe: card on file ------------------------------------------------------------------

def start_setup(item, account):
    """Customer + SetupIntent on the restaurant's account. Idempotent per booking."""
    rid = item["reservationId"]
    metadata = {"reservationId": rid, "locationId": item["locationId"],
                "tenantId": item["tenantId"], "date": item["date"]}
    customer = stripe.request("POST", "/v1/customers", {
        "name": item.get("customerName"),
        "email": item.get("customerEmail"),
        "phone": item.get("customerPhone"),
        "metadata": metadata,
    }, account=account, idempotency_key=f"customer-{rid}")
    intent = stripe.request("POST", "/v1/setup_intents", {
        "customer": customer["id"],
        "usage": "off_session",
        "payment_method_types": ["card"],
        "description": f"Kortgaranti bordsbokning {item['date']} {item['startTime']}",
        "metadata": metadata,
    }, account=account, idempotency_key=f"setup-{rid}")
    return customer["id"], intent["id"], intent["client_secret"]


def retrieve_setup(item):
    return stripe.request("GET", f"/v1/setup_intents/{item['setupIntentId']}",
                          account=item["stripeAccountId"])


def setup_matches(intent, item):
    meta = (intent or {}).get("metadata") or {}
    return (intent.get("id") == item.get("setupIntentId")
            and meta.get("reservationId") == item.get("reservationId")
            and intent.get("customer") == item.get("stripeCustomerId"))


def confirm_items(item, payment_method, now, actor):
    """Transaction item: pending -> reserved with the card stored + the
    guest's confirmation notice."""
    entry = r.history_entry("guarantee_confirmed", actor, now, status=r.RESERVED)
    return {
        "Update": {
            "TableName": r.reservation_table(),
            "Key": r.serialize(r.reservation_key(item["locationId"], item["date"], item["reservationId"])),
            "UpdateExpression": ("SET #s = :reserved, stripePaymentMethodId = :pm, updatedAt = :now, "
                                 "updatedBy = :by, notice = :notice, "
                                 "history = list_append(if_not_exists(history, :empty), :entry), "
                                 "version = if_not_exists(version, :zero) + :one "
                                 "REMOVE pendingExpiresAt"),
            "ConditionExpression": "#s = :pending AND setupIntentId = :seti",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": r.serialize({
                ":reserved": r.RESERVED, ":pending": r.PENDING, ":pm": payment_method,
                ":now": r.iso(now), ":by": actor, ":seti": item["setupIntentId"],
                ":notice": r.notice(r.NOTICE_CONFIRMED, now), ":entry": [entry], ":empty": [],
                ":zero": 0, ":one": 1,
            }),
        }
    }


def confirm_if_ready(item, now, actor, *, intent=None):
    """Pending booking whose SetupIntent succeeded -> reserved. Returns the
    (possibly updated) item; raises r.InvalidState('card_not_confirmed')
    while the guest hasn't finished, r.InvalidState('expired') if the hold
    ran out first."""
    if item["status"] == r.RESERVED or item["status"] not in (r.PENDING, r.EXPIRED):
        return item
    if item["status"] == r.EXPIRED:
        raise r.InvalidState("expired")
    intent = intent or retrieve_setup(item)
    if not setup_matches(intent, item) or intent.get("status") != "succeeded":
        raise r.InvalidState("card_not_confirmed")
    payment_method = intent.get("payment_method")
    if isinstance(payment_method, dict):
        payment_method = payment_method.get("id")
    if not isinstance(payment_method, str):
        raise r.InvalidState("card_not_confirmed")
    try:
        r.transact([confirm_items(item, payment_method, now, actor)] + marker_delete_ops(item))
    except r.Conflict:
        fresh = r.load(item["locationId"], item["reservationId"])
        if fresh["status"] == r.RESERVED:
            return fresh  # the webhook (or a double click) got there first
        if fresh["status"] == r.EXPIRED:
            raise r.InvalidState("expired") from None
        raise
    return {**item, "status": r.RESERVED, "stripePaymentMethodId": payment_method}


# --- Stripe: fees -----------------------------------------------------------------------------

def _outcome_from_intent(intent):
    status = (intent or {}).get("status")
    if status == "succeeded":
        return PAY_SUCCEEDED, None
    if status == "processing":
        return PAY_PROCESSING, None
    error = (intent.get("last_payment_error") or {}) if intent else {}
    return PAY_FAILED, error.get("decline_code") or error.get("code") or status or "failed"


def charge(item, kind):
    """Off-session fee charge for `item` (whose payment.attempt is set).
    Returns (pay_status, payment_intent_id, error_code). Raises
    StripeUnavailable when the outcome is unknown (retry = same key)."""
    payment = item["payment"]
    amount = int(payment["amount"])
    rid = item["reservationId"]
    label = "Utebliven gäst" if kind == KIND_NO_SHOW else "Sen avbokning"
    try:
        intent = stripe.request("POST", "/v1/payment_intents", {
            "amount": amount,
            "currency": CURRENCY,
            "customer": item["stripeCustomerId"],
            "payment_method": item["stripePaymentMethodId"],
            "payment_method_types": ["card"],
            "off_session": True,
            "confirm": True,
            "description": f"{label} - bordsbokning {item['date']} {item['startTime']}",
            "metadata": {"reservationId": rid, "locationId": item["locationId"],
                         "tenantId": item["tenantId"], "date": item["date"], "feeKind": kind},
        }, account=item["stripeAccountId"],
            idempotency_key=f"{kind}-{rid}-{int(payment.get('attempt') or 1)}")
    except stripe.StripeError as exc:
        intent_id = exc.payment_intent.get("id") if isinstance(exc.payment_intent, dict) else None
        return PAY_FAILED, intent_id, exc.decline_code or exc.code or exc.type or "card_error"
    status, error = _outcome_from_intent(intent)
    return status, intent.get("id"), error


def refund(item, amount=None):
    payment = item["payment"]
    n = int(payment.get("refunds") or 0) + 1
    params = {"payment_intent": payment["paymentIntentId"], "reason": "requested_by_customer"}
    if amount:
        params["amount"] = int(amount)
    return stripe.request("POST", "/v1/refunds", params, account=item["stripeAccountId"],
                          idempotency_key=f"refund-{item['reservationId']}-{n}")


# --- DynamoDB: recording fee outcomes -----------------------------------------------------------

def _key(item):
    return r.reservation_key(item["locationId"], item["date"], item["reservationId"])


def begin_charge_update(item, kind, now, actor, amount=None):
    """Mark a charge as started (payment.status pending, attempt + 1) on a
    booking already in its base status (no_show / cancelled_no_charge) or a
    *_charge_failed one being retried. Returns the item with `payment`."""
    previous = item.get("payment") or {}
    # An attempt whose outcome is unknown (Stripe unreachable / processing)
    # is re-sent with the SAME idempotency key - Stripe answers with the
    # original result instead of charging again. Only a settled failure
    # starts a new attempt.
    unsettled = previous.get("status") in (PAY_PENDING, PAY_PROCESSING) and previous.get("kind") == kind
    attempt = int(previous.get("attempt") or 0) + (0 if unsettled else 1)
    if amount is None:
        amount = int(previous.get("amount") or 0) or (no_show_fee(item) if kind == KIND_NO_SHOW else 0)
    if amount <= 0:
        raise r.InvalidState("no_fee")
    payment = {"kind": kind, "status": PAY_PENDING, "amount": amount, "attempt": max(attempt, 1),
               "startedAt": r.iso(now), "startedBy": actor}
    if unsettled and previous.get("paymentIntentId"):
        payment["paymentIntentId"] = previous["paymentIntentId"]
    allowed = [_BASE_STATUS[kind], _FINAL_STATUS[(kind, False)]]
    dynamo.table(r.reservation_table()).update_item(
        Key=_key(item),
        UpdateExpression="SET payment = :p, updatedAt = :now, updatedBy = :by",
        ConditionExpression=("#s IN (:a, :b) AND (attribute_not_exists(payment) "
                             "OR payment.#ps <> :succeeded)"),
        ExpressionAttributeNames={"#s": "status", "#ps": "status"},
        ExpressionAttributeValues={":p": payment, ":now": r.iso(now), ":by": actor,
                                   ":a": allowed[0], ":b": allowed[1], ":succeeded": PAY_SUCCEEDED},
    )
    return {**item, "payment": payment}


def record_outcome(item, kind, pay_status, intent_id, error, now, actor):
    """Store the charge result; a final result moves the status to
    *_charged / *_charge_failed and notifies (guest receipt / restaurant).
    Guarded on the attempt, so a late webhook for an old attempt is a no-op."""
    payment = dict(item["payment"])
    payment.update({"status": pay_status, "paymentIntentId": intent_id, "error": error,
                    "settledAt": r.iso(now)})
    payment = {k: v for k, v in payment.items() if v is not None}
    names = {"#pa": "attempt"}
    values = {":p": payment, ":now": r.iso(now), ":by": actor, ":attempt": payment["attempt"]}
    expression = "SET payment = :p, updatedAt = :now, updatedBy = :by"
    new_status = item["status"]
    if pay_status in (PAY_SUCCEEDED, PAY_FAILED):
        new_status = _FINAL_STATUS[(kind, pay_status == PAY_SUCCEEDED)]
        notice_kind = r.NOTICE_FEE_CHARGED if pay_status == PAY_SUCCEEDED else r.NOTICE_FEE_FAILED
        entry = r.history_entry("fee", actor, now, status=new_status, amount=payment["amount"],
                                kind=kind, error=error)
        names["#s"] = "status"
        expression += (", #s = :status, notice = :notice, "
                       "history = list_append(if_not_exists(history, :empty), :entry)")
        values.update({":status": new_status, ":notice": r.notice(notice_kind, now),
                       ":entry": [entry], ":empty": []})
    dynamo.table(r.reservation_table()).update_item(
        Key=_key(item),
        UpdateExpression=expression,
        ConditionExpression="payment.#pa = :attempt",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    return {**item, "status": new_status, "payment": payment}


def run_charge(item, kind, now, actor, amount=None):
    """begin -> Stripe -> record. A Stripe outage leaves payment.status
    pending (staff can retry; the same attempt key can't double-charge)."""
    item = begin_charge_update(item, kind, now, actor, amount)
    try:
        pay_status, intent_id, error = charge(item, kind)
    except stripe.StripeUnavailable:
        return item
    return record_outcome(item, kind, pay_status, intent_id, error, now, actor)


def record_refund(item, refund_obj, now, actor):
    payment = dict(item["payment"])
    refunded = int(payment.get("refundedAmount") or 0) + int(refund_obj.get("amount") or 0)
    payment.update({"refundedAmount": refunded, "refunds": int(payment.get("refunds") or 0) + 1,
                    "refundedAt": r.iso(now)})
    if refunded >= int(payment["amount"]):
        payment["status"] = PAY_REFUNDED
    entry = r.history_entry("refund", actor, now, amount=int(refund_obj.get("amount") or 0))
    dynamo.table(r.reservation_table()).update_item(
        Key=_key(item),
        UpdateExpression=("SET payment = :p, updatedAt = :now, updatedBy = :by, "
                          "history = list_append(if_not_exists(history, :empty), :entry)"),
        ConditionExpression="payment.#pa = :attempt",
        ExpressionAttributeNames={"#pa": "attempt"},
        ExpressionAttributeValues={":p": payment, ":now": r.iso(now), ":by": actor,
                                   ":attempt": payment["attempt"], ":entry": [entry], ":empty": []},
    )
    return {**item, "payment": payment}
