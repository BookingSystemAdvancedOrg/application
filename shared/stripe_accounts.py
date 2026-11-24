"""A restaurant's Stripe connected account as stored on its tenant row.

PROFILE.stripe = {accountId, chargesEnabled, payoutsEnabled,
detailsSubmitted, requirementsDue, paymentMethods, disconnected,
eventCreated, updatedAt}.

paymentMethods = {card, swish, klarna}: the account's capability status
(active, pending, inactive, unrequested) for the methods the site's
payment steps offer. Apple Pay and Google Pay ride on card; the restaurant
turns Swish and Klarna on in its own Stripe Dashboard (an account with the
full Dashboard manages its own payment methods).
The onboarding workflow creates the account and the STRIPE_ACCOUNT#<acct> /
TENANT row; platform-stripe-webhook keeps the status fields current (same
fields as Booqy Console's "Check status with Stripe").
"""

import os
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from shared import dynamo
from shared.tenant import tenant_pk

DISCONNECTED = {
    "chargesEnabled": False,
    "payoutsEnabled": False,
    "disconnected": True,
}


def _table():
    return dynamo.table(os.environ["TENANT_TABLE_NAME"])


# paymentMethods key -> the account capability behind it
METHOD_CAPABILITIES = {"card": "card_payments", "swish": "swish_payments", "klarna": "klarna_payments"}
_CAPABILITY_STATES = ("active", "pending", "inactive", "unrequested")


def payment_methods_of(account: dict) -> dict:
    capabilities = account.get("capabilities") if isinstance(account.get("capabilities"), dict) else {}
    out = {}
    for key, capability in METHOD_CAPABILITIES.items():
        value = capabilities.get(capability)
        if isinstance(value, dict):  # an expanded capability object
            value = value.get("status")
        out[key] = value if value in _CAPABILITY_STATES else "unrequested"
    return out


def state_of(account: dict) -> dict:
    """Status fields from a v1 account object (Stripe answers for Accounts
    v2 accounts in the v1 shape too)."""
    requirements = account.get("requirements") or {}
    due = requirements.get("currently_due") or []
    return {
        "paymentMethods": payment_methods_of(account),
        "chargesEnabled": bool(account.get("charges_enabled")),
        "payoutsEnabled": bool(account.get("payouts_enabled")),
        "detailsSubmitted": bool(account.get("details_submitted")),
        "requirementsDue": [str(item) for item in due if isinstance(item, str)][:50],
        "disconnected": False,
    }


def tenant_of_account(account_id: str):
    item = _table().get_item(
        Key={"PK": f"STRIPE_ACCOUNT#{account_id}", "SK": "TENANT"}, ConsistentRead=True,
    ).get("Item")
    tenant_id = (item or {}).get("tenantId")
    return tenant_id if isinstance(tenant_id, str) and tenant_id else None


def store(tenant_id: str, account_id: str, state: dict, event_created: int) -> bool:
    """Writes `state` unless the tenant no longer owns the account or a newer
    event was already applied. Returns False when skipped."""
    names = {"#s": "stripe", "#e": "eventCreated", "#a": "accountId", "#u": "updatedAt"}
    values = {":acct": account_id, ":c": int(event_created),
              ":now": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    sets = ["#s.#e = :c", "#s.#u = :now"]
    for i, (key, value) in enumerate(sorted(state.items())):
        names[f"#f{i}"] = key
        values[f":v{i}"] = value
        sets.append(f"#s.#f{i} = :v{i}")
    try:
        _table().update_item(
            Key={"PK": tenant_pk(tenant_id), "SK": "PROFILE"},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="#s.#a = :acct AND (attribute_not_exists(#s.#e) OR #s.#e <= :c)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True
