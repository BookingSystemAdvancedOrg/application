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
    refund    Owner refund of (part of) the fee's PaymentIntent, key
              refund-<reservationId>-<n> (shared/refunds.py: reserved,
              capped, recorded once, agrees with charge.refunded)

Reservation attributes: guarantee (snapshot), stripeAccountId,
stripeCustomerId, setupIntentId, stripePaymentMethodId, payment = {kind,
status, amount, attempt, paymentIntentId, error, chargedAt, refundedAmount}.
"""

from datetime import timedelta
from decimal import Decimal

from botocore.exceptions import ClientError

from shared import dynamo, refunds
from shared import reservations as r
from shared import stripe_client as stripe

CURRENCY = "sek"
MAX_FEE_PER_PERSON = 5000
MAX_CUTOFF_HOURS = 168
PENDING_MINUTES = 20
# A phone booking (the AI phone agent, source "phone"): the guest gets an SMS
# link to a Stripe-hosted Checkout page (mode setup) and has this long to add
# the card. Stripe needs a Checkout Session to live at least 30 minutes, so
# the page closes CHECKOUT_MARGIN after the window the guest is told, and the
# booking (its tables) is held CHECKOUT_MARGIN after the page closes: a card
# added in the last second still finds its booking.
PHONE_PENDING_MINUTES = 30
CHECKOUT_MARGIN = timedelta(minutes=1)

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


def free_cancel_until(item):
    """ISO UTC instant until which cancelling is free (None = no late-cancel fee)."""
    snap = item.get("guarantee") if isinstance(item.get("guarantee"), dict) else {}
    if not int(snap.get("lateCancelFee") or 0) or not int(snap.get("cancelCutoffHours") or 0):
        return None
    cutoff = r.parse_iso(item["bookedFor"]) - timedelta(hours=int(snap.get("cancelCutoffHours") or 0))
    return r.iso(cutoff)


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


def expire_items(item, now, *, action="expired", actor="system"):
    """Transaction items: a pending booking whose card was never confirmed,
    or a table hold that ran out / was let go -> expired, its tables
    released, its marker gone."""
    entry = r.history_entry(action, actor, now, status=r.EXPIRED)
    versions = r.read_lock_versions(item["locationId"], item["date"], item.get("tableIds") or [])
    return (r.lock_updates(item["locationId"], item["date"], versions) + r.release_items(item) + [{
        "Update": {
            "TableName": r.reservation_table(),
            "Key": r.serialize(r.reservation_key(item["locationId"], item["date"], item["reservationId"])),
            "UpdateExpression": ("SET #s = :expired, holdsReleased = :true, updatedAt = :now, "
                                 "updatedBy = :by, history = list_append(if_not_exists(history, :empty), :entry), "
                                 "version = if_not_exists(version, :zero) + :one REMOVE pendingExpiresAt"),
            "ConditionExpression": "#s IN (:pending, :held)",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": r.serialize({
                ":expired": r.EXPIRED, ":pending": r.PENDING, ":held": r.HELD, ":true": True,
                ":now": r.iso(now), ":by": actor, ":entry": [entry], ":empty": [], ":zero": 0, ":one": 1,
            }),
        }
    }] + marker_delete_ops(item))


# --- Stripe: card on file ------------------------------------------------------------------

def _metadata(item):
    """What the webhook (setup_intent.succeeded) finds the booking by."""
    return {"reservationId": item["reservationId"], "locationId": item["locationId"],
            "tenantId": item["tenantId"], "date": item["date"]}


def _customer(item, account):
    return stripe.request("POST", "/v1/customers", {
        "name": item.get("customerName"),
        "email": item.get("customerEmail"),
        "phone": item.get("customerPhone"),
        "metadata": _metadata(item),
    }, account=account, idempotency_key=f"customer-{item['reservationId']}")


def start_setup(item, account):
    """Customer + SetupIntent on the restaurant's account. Idempotent per booking."""
    rid = item["reservationId"]
    metadata = _metadata(item)
    customer = _customer(item, account)
    intent = stripe.request("POST", "/v1/setup_intents", {
        "customer": customer["id"],
        "usage": "off_session",
        "payment_method_types": ["card"],
        "description": f"Kortgaranti bordsbokning {item['date']} {item['startTime']}",
        "metadata": metadata,
    }, account=account, idempotency_key=f"setup-{rid}")
    return customer["id"], intent["id"], intent["client_secret"]


def _checkout_fee_text(item, lang):
    """The fees the guest accepts, shown above the button on the Checkout page."""
    from shared import messages  # local: messages is a leaf module, keep guarantee's imports small
    lines = messages.guarantee_terms(item, lang)
    return " ".join(lines)[:1200]


def start_checkout(item, account, *, return_url, lang, now, success_url=None):
    """Customer + Checkout Session (mode setup) on the restaurant's account:
    the Stripe-hosted page a phone guest opens from the SMS. The SetupIntent
    Checkout creates carries the booking's metadata, so the webhook's
    setup_intent.succeeded turns the booking reserved exactly like an online
    one. `success_url` (default `return_url`): where the guest lands after
    adding the card (the booking's manage page, which confirms it).
    Idempotent per booking. Returns (customer id, session)."""
    rid = item["reservationId"]
    customer = _customer(item, account)
    session = stripe.request("POST", "/v1/checkout/sessions", {
        "mode": "setup",
        "customer": customer["id"],
        "payment_method_types": ["card"],
        "locale": "en" if lang == "en" else "sv",
        "expires_at": int((now + timedelta(minutes=PHONE_PENDING_MINUTES) + CHECKOUT_MARGIN).timestamp()),
        "success_url": success_url or return_url,
        "cancel_url": return_url,
        "client_reference_id": rid,
        "metadata": _metadata(item),
        "setup_intent_data": {
            "description": f"Kortgaranti bordsbokning {item['date']} {item['startTime']}",
            "metadata": _metadata(item),
        },
        "custom_text": {"submit": {"message": _checkout_fee_text(item, lang)}},
    }, account=account, idempotency_key=f"checkout-{rid}")
    if not isinstance(session.get("url"), str) or not session.get("id"):
        raise stripe.StripeUnavailable("checkout session without url")
    return customer["id"], session


def phone_pending_expires(now):
    """pendingExpiresAt of a phone booking: the Checkout page closes first
    (the same window for the restaurant's own card page)."""
    return now + timedelta(minutes=PHONE_PENDING_MINUTES) + 2 * CHECKOUT_MARGIN


def retrieve_setup(item):
    return stripe.request("GET", f"/v1/setup_intents/{item['setupIntentId']}",
                          account=item["stripeAccountId"])


def card_hold_open(item, now):
    """A pending booking with a card step (SetupIntent) whose hold has not
    run out: the guest can still add the card, even when card_step could
    not read the form from Stripe just now."""
    expires = item.get("pendingExpiresAt")
    return (item.get("status") == r.PENDING and bool(item.get("setupIntentId"))
            and bool(item.get("stripeAccountId")) and bool(expires) and r.parse_iso(expires) > now)


def card_step(item, now):
    """The card form of a pending booking for its manage page (the guest's
    GET with a valid manage token): {clientSecret, stripeAccount, expiresAt},
    the shape the online booking answers with. Read from Stripe, never
    stored. None when the booking is not pending, has no SetupIntent (a
    Checkout-page phone booking), its hold ran out, the SetupIntent is no
    longer open or Stripe cannot be asked."""
    if item.get("status") != r.PENDING or not item.get("setupIntentId") or not item.get("stripeAccountId"):
        return None
    expires = item.get("pendingExpiresAt")
    if not expires or r.parse_iso(expires) <= now:
        return None
    try:
        intent = retrieve_setup(item)
    except (stripe.StripeError, stripe.StripeUnavailable):
        return None
    secret = (intent or {}).get("client_secret")
    if (not isinstance(secret, str) or not secret or intent.get("status") in ("succeeded", "canceled")
            or not setup_matches(intent, item)):
        return None
    return {"clientSecret": secret, "stripeAccount": item["stripeAccountId"], "expiresAt": expires}


def checkout_setup(item):
    """The SetupIntent of a phone booking's completed Checkout page, else None."""
    if not item.get("checkoutSessionId"):
        return None
    session = stripe.request("GET", f"/v1/checkout/sessions/{item['checkoutSessionId']}",
                             {"expand": ["setup_intent"]}, account=item["stripeAccountId"])
    intent = session.get("setup_intent")
    return intent if session.get("status") == "complete" and isinstance(intent, dict) else None


def setup_matches(intent, item):
    """The SetupIntent is this booking's: the one created at booking, or for
    a phone booking (Checkout creates its SetupIntent only when the guest
    submits the page) any SetupIntent of the booking's own customer that
    carries its reservationId."""
    meta = (intent or {}).get("metadata") or {}
    own = item.get("setupIntentId")
    if own:
        same = intent.get("id") == own
    else:
        same = bool(item.get("checkoutSessionId")) and str(intent.get("id") or "").startswith("seti_")
    return (same and meta.get("reservationId") == item.get("reservationId")
            and intent.get("customer") == item.get("stripeCustomerId"))


def confirm_items(item, payment_method, now, actor, setup_intent_id=None):
    """Transaction item: pending -> reserved with the card stored + the
    guest's confirmation notice. `setup_intent_id`: a phone booking's
    Checkout SetupIntent, stored now (the booking had none)."""
    entry = r.history_entry("guarantee_confirmed", actor, now, status=r.RESERVED)
    expression = ("SET #s = :reserved, stripePaymentMethodId = :pm, updatedAt = :now, "
                  "updatedBy = :by, notice = :notice, "
                  "history = list_append(if_not_exists(history, :empty), :entry), "
                  "version = if_not_exists(version, :zero) + :one")
    if setup_intent_id:
        expression += ", setupIntentId = :seti"
        condition = "#s = :pending AND attribute_not_exists(setupIntentId)"
    else:
        condition = "#s = :pending AND setupIntentId = :seti"
    return {
        "Update": {
            "TableName": r.reservation_table(),
            "Key": r.serialize(r.reservation_key(item["locationId"], item["date"], item["reservationId"])),
            "UpdateExpression": expression + " REMOVE pendingExpiresAt",
            "ConditionExpression": condition,
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": r.serialize({
                ":reserved": r.RESERVED, ":pending": r.PENDING, ":pm": payment_method,
                ":now": r.iso(now), ":by": actor, ":seti": setup_intent_id or item["setupIntentId"],
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
    # Started from a table hold: the hold's clock is the card step's clock -
    # once it ran out the tables may already be someone else's.
    if item.get("fromHold") and item.get("pendingExpiresAt") and r.parse_iso(item["pendingExpiresAt"]) <= now:
        raise r.InvalidState("expired")
    if intent is None and not item.get("setupIntentId"):
        # A phone booking: the SetupIntent of its Checkout page, once the
        # guest submitted it (the return page and the expiry job ask; the
        # webhook brings it).
        intent = checkout_setup(item)
        if not isinstance(intent, dict):
            raise r.InvalidState("card_not_confirmed")
    intent = intent or retrieve_setup(item)
    if not setup_matches(intent, item) or intent.get("status") != "succeeded":
        raise r.InvalidState("card_not_confirmed")
    payment_method = intent.get("payment_method")
    if isinstance(payment_method, dict):
        payment_method = payment_method.get("id")
    if not isinstance(payment_method, str):
        raise r.InvalidState("card_not_confirmed")
    adopted = None if item.get("setupIntentId") else intent["id"]
    try:
        r.transact([confirm_items(item, payment_method, now, actor, adopted)] + marker_delete_ops(item)
                   + (r.keep_hold_items(item) if item.get("fromHold") else []))
    except r.Conflict:
        fresh = r.load(item["locationId"], item["reservationId"])
        if fresh["status"] == r.RESERVED:
            return fresh  # the webhook (or a double click) got there first
        if fresh["status"] == r.EXPIRED:
            raise r.InvalidState("expired") from None
        raise
    done = {**item, "status": r.RESERVED, "stripePaymentMethodId": payment_method}
    if adopted:
        done["setupIntentId"] = adopted
    return done


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


# --- refunds (shared/refunds.py rules) -------------------------------------------------------
#
# payment.refundedAmount (total), payment.refunds (count of refunds Booqy
# started, n in the key refund-<reservationId>-<n>), payment.refundRev,
# payment.refundPending, payment.refundItems [{n?, amount, at, by, reason?,
# refundId?, external?}], payment.refundNotice {amount, reason?} (what the
# guest's fee_refunded message says), refundedAt (last refund).

class _Fees(refunds.Domain):
    def __init__(self, item):
        self.prefix = f"refund-{item['reservationId']}"

    def state(self, item):
        payment = item.get("payment") or {}
        total = int(payment.get("refundedAmount") or 0)
        items = list(payment.get("refundItems") or [])
        if total and not items:  # recorded before refundItems existed
            items = [{"amount": total, "at": payment.get("refundedAt") or payment.get("settledAt"), "by": "stripe"}]
        return refunds.normal(total, payment.get("refunds"), payment.get("refundRev"),
                              payment.get("refundPending"), items)

    def reload(self, item):
        return r.load(item["locationId"], item["reservationId"])

    def create(self, item, pending, *, replay=False):
        return refunds.stripe_create(item["stripeAccountId"], item["payment"]["paymentIntentId"], pending,
                                     {"reservationId": item["reservationId"], "locationId": item["locationId"],
                                      "kind": "fee_refund"}, replay=replay)

    def write(self, item, old, new, completed, now):
        payment = dict(item["payment"])
        payment.update({"refundedAmount": new["total"], "refunds": new["count"], "refundRev": new["rev"],
                        "refundItems": new["items"]})
        payment.pop("refundPending", None)
        if new["pending"]:
            payment["refundPending"] = new["pending"]
        if completed:
            payment["refundedAt"] = r.iso(now)
        if new["total"] >= int(payment["amount"]):
            payment["status"] = PAY_REFUNDED
        sets = ["payment = :p", "updatedAt = :now", "updatedBy = :by"]
        values = {":p": payment, ":now": r.iso(now), ":attempt": payment["attempt"],
                  ":by": ((completed[-1] if completed else None) or new["pending"] or old["pending"]
                          or {}).get("by") or "stripe"}
        if completed:
            sets.append("history = list_append(if_not_exists(history, :empty), :entries)")
            values[":empty"] = []
            values[":entries"] = [r.history_entry("refund", e.get("by") or "stripe", now, amount=e["amount"],
                                                  reason=e.get("reason")) for e in completed]
            told = [e for e in completed if e.get("notify")]
            if told and r.can_be_notified(item):
                payment["refundNotice"] = {k: v for k, v in {"amount": told[-1]["amount"],
                                                             "reason": told[-1].get("reason")}.items() if v}
                sets.append("notice = :notice")
                values[":notice"] = r.notice(r.NOTICE_FEE_REFUNDED, now)
        condition = "payment.#pa = :attempt AND "
        if old["rev"]:
            condition += "payment.#rr = :rev"
            values[":rev"] = old["rev"]
        else:
            condition += "attribute_not_exists(payment.#rr)"
        try:
            dynamo.table(r.reservation_table()).update_item(
                Key=_key(item), UpdateExpression="SET " + ", ".join(sets), ConditionExpression=condition,
                ExpressionAttributeNames={"#pa": "attempt", "#rr": "refundRev"}, ExpressionAttributeValues=values)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise refunds.Changed() from None
            raise
        return {**item, "payment": payment}


def refundable(item):
    """What is left to refund of a charged fee (öre); 0 when nothing can be."""
    payment = item.get("payment") or {}
    if item.get("status") not in (r.NO_SHOW_CHARGED, r.CANCELLED_CHARGED) or payment.get("status") not in (
            PAY_SUCCEEDED, PAY_REFUNDED):
        return 0
    return max(int(payment.get("amount") or 0) - int(payment.get("refundedAmount") or 0), 0)


def refund(item, amount, now, actor, *, reason=None, notify=True):
    """Owner refund of (part of) a charged fee. Raises refunds.Conflict /
    ValueError / stripe errors; returns the updated item."""
    payment = item.get("payment") or {}
    if item.get("status") not in (r.NO_SHOW_CHARGED, r.CANCELLED_CHARGED) or payment.get("status") not in (
            PAY_SUCCEEDED, PAY_REFUNDED) or not payment.get("paymentIntentId"):
        raise r.InvalidState("not_refundable")
    domain = _Fees(item)
    item, _ = refunds.start(domain, item, amount=amount, cap=int(payment["amount"]), actor=actor, now=now,
                            reason=reason, notify=notify)
    return item


def record_stripe_refund(item, amount_refunded, now):
    """charge.refunded on the fee's PaymentIntent. Returns the result string."""
    _, result = refunds.from_stripe(_Fees(item), item, amount_refunded, now)
    return result
