"""Tenant context - the one place that decides whose data a request may touch.

Every handler calls this before reading or writing anything:

    from shared import tenant

    try:
        ctx = tenant.for_jwt(event, location_id=location_id, feature="reservations")
    except tenant.TenantError as exc:
        return exc.response()

Rules (docs/handoff/BACKEND.md in the infrastructure repo):

1. The tenant comes from a trusted source only: the `tenant_id` claim that the
   pre-token-generation trigger puts in every tenant-pool token, or - on
   public routes - the location row the `{locationId}` belongs to. Never from
   a path, body, query string or header.
2. Every location is checked against that tenant. A location of another
   tenant answers 404 exactly like a location that doesn't exist, so a caller
   can't even learn that an id is in use.
3. Only `active` tenants are served, and only plan features they have.

The tenant row is cached per container for TENANT_CACHE_SECONDS (default 60)
so a suspension takes effect within a minute; the location -> tenant mapping
never changes, so it is cached for the container's lifetime.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional

from boto3.dynamodb.conditions import Key

from shared import dynamo
from shared.responses import json_response

ROLES = ("owner_user", "staff_user")
FEATURES = ("reservations", "ordering", "catering", "terminal")

_tenant_cache: dict = {}
_location_tenant_cache: dict = {}


class TenantError(Exception):
    """A request the tenant rules refuse. `response()` is the API answer."""

    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status = status
        self.error = error

    def response(self, headers: Optional[dict] = None) -> dict:
        return json_response(
            self.status,
            {"error": self.error},
            headers={"Cache-Control": "no-store", **(headers or {})},
        )


def _not_found() -> TenantError:
    # Same answer for "doesn't exist" and "belongs to someone else".
    return TenantError(404, "not found")


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    tenant: dict
    role: Optional[str] = None          # None on public routes
    sub: Optional[str] = None           # caller's Cognito sub (JWT routes)
    location: Optional[dict] = None     # the checked location row, if any
    claims: dict = field(default_factory=dict, repr=False)

    @property
    def is_owner(self) -> bool:
        return self.role == "owner_user"

    @property
    def location_id(self) -> Optional[str]:
        return self.location.get("locationId") if self.location else None

    @property
    def location_key(self) -> dict:
        """Primary key of this tenant's location row (GetItem/UpdateItem)."""
        return location_key(self.tenant_id, self.location_id)

    def stripe_account(self) -> Optional[str]:
        """Connected account payments for this location go to: the
        location's own account (a separate company in the group) or the
        tenant's."""
        own = (self.location or {}).get("stripeAccountId")
        return own or (self.tenant.get("stripe") or {}).get("accountId")


# --- keys --------------------------------------------------------------------

def tenant_pk(tenant_id: str) -> str:
    return f"TENANT#{tenant_id}"


def location_key(tenant_id: str, location_id: str) -> dict:
    return {"PK": tenant_pk(tenant_id), "SK": f"LOCATION#{location_id}"}


# --- lookups -------------------------------------------------------------------

def _cache_seconds() -> float:
    try:
        return float(os.environ.get("TENANT_CACHE_SECONDS", "60"))
    except ValueError:
        return 60.0


def get_tenant(tenant_id: str, *, fresh: bool = False) -> Optional[dict]:
    """The tenant's PROFILE row, or None. Cached briefly per container;
    `fresh=True` reads it strongly consistent (and refreshes the cache) -
    for responses that must reflect a change made a moment ago. An unknown
    tenant is never cached, so a tenant that was just onboarded is served
    immediately."""
    now = time.monotonic()
    hit = _tenant_cache.get(tenant_id)
    if hit and hit[0] > now and not fresh:
        return hit[1]
    item = dynamo.table(os.environ["TENANT_TABLE_NAME"]).get_item(
        Key={"PK": tenant_pk(tenant_id), "SK": "PROFILE"}, ConsistentRead=fresh
    ).get("Item")
    if item is None:
        _tenant_cache.pop(tenant_id, None)
    else:
        _tenant_cache[tenant_id] = (now + _cache_seconds(), item)
    return item


def invalidate(tenant_id: str) -> None:
    """Forget the cached tenant row - call after writing it."""
    _tenant_cache.pop(tenant_id, None)


def get_location(tenant_id: str, location_id: str, *, consistent: bool = True) -> Optional[dict]:
    """A location row by its full key - use when the tenant is known."""
    return dynamo.table(os.environ["LOCATION_TABLE_NAME"]).get_item(
        Key=location_key(tenant_id, location_id), ConsistentRead=consistent
    ).get("Item")


def tenant_of_location(location_id: str) -> Optional[str]:
    """Which tenant owns a locationId (byLocationId index; cached - a
    location never moves to another tenant)."""
    if location_id in _location_tenant_cache:
        return _location_tenant_cache[location_id]
    items = dynamo.table(os.environ["LOCATION_TABLE_NAME"]).query(
        IndexName=os.environ["LOCATION_ID_INDEX_NAME"],
        KeyConditionExpression=Key("locationId").eq(location_id),
    ).get("Items") or []
    # Only tenant rows count (pre-tenancy rows have no tenantId), and an id
    # that resolves to more than one tenant is refused rather than guessed.
    tenants = {
        i.get("tenantId") for i in items
        if isinstance(i.get("tenantId"), str) and i.get("PK") == tenant_pk(i.get("tenantId"))
    }
    if len(tenants) != 1:
        return None
    tenant_id = tenants.pop()
    _location_tenant_cache[location_id] = tenant_id
    return tenant_id


def list_locations(tenant_id: str) -> list:
    """All of a tenant's locations - a Query on its own partition, never a
    Scan + filter."""
    tbl = dynamo.table(os.environ["LOCATION_TABLE_NAME"])
    items, kwargs = [], {
        "KeyConditionExpression": Key("PK").eq(tenant_pk(tenant_id)) & Key("SK").begins_with("LOCATION#"),
        "ConsistentRead": True,
    }
    while True:
        page = tbl.query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# --- checks --------------------------------------------------------------------

def require_feature(tenant_row: dict, feature: Optional[str]) -> None:
    if not feature:
        return
    features = ((tenant_row.get("entitlements") or {}).get("features") or {})
    if not features.get(feature):
        raise TenantError(403, "feature_not_in_plan")


def _claims(event: dict) -> dict:
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    except (KeyError, TypeError):
        raise TenantError(401, "unauthorized")
    if not isinstance(claims, dict):
        raise TenantError(401, "unauthorized")
    return claims


def _groups(claims: dict) -> list:
    raw = claims.get("cognito:groups") or []
    if isinstance(raw, str):  # API Gateway may flatten the list to "[a b]"
        raw = raw.strip("[]").replace(",", " ").replace('"', " ").split()
    if not isinstance(raw, (list, tuple)):
        return []
    return [g for g in raw if isinstance(g, str)]


def staff_location(sub: str, tenant_id: str) -> Optional[str]:
    """The location an active staff user of this tenant is assigned to
    (their USER# profile) when the function has the user table; None = not
    checked here; "" = no usable assignment (missing, disabled, other
    tenant), which matches no location."""
    table_name = os.environ.get("USER_TABLE_NAME")
    if not table_name:
        return None
    item = dynamo.table(table_name).get_item(
        Key={"PK": f"USER#{sub}", "SK": "PROFILE"}
    ).get("Item") or {}
    if item.get("tenantId") != tenant_id or item.get("status") != "active":
        return ""
    return item.get("locationId") or ""


_staff_location = staff_location


def for_jwt(
    event: dict,
    *,
    location_id: Optional[str] = None,
    feature: Optional[str] = None,
    owner_only: bool = False,
) -> TenantContext:
    """Tenant-pool JWT routes. Raises TenantError (401/403/404)."""
    claims = _claims(event)
    tenant_id = claims.get("tenant_id")
    role = claims.get("role")
    if role not in ROLES:  # older tokens: fall back to the Cognito group
        role = next((g for g in _groups(claims) if g in ROLES), None)
    sub = claims.get("sub")
    if not isinstance(tenant_id, str) or not tenant_id or role not in ROLES or not sub:
        raise TenantError(403, "no_tenant")
    if owner_only and role != "owner_user":
        raise TenantError(403, "owner_only")

    tenant_row = get_tenant(tenant_id)
    if not tenant_row or tenant_row.get("status") != "active":
        raise TenantError(403, "tenant_inactive")
    require_feature(tenant_row, feature)

    location = None
    if location_id is not None:
        location = get_location(tenant_id, location_id)
        if not location:
            raise _not_found()
        if role == "staff_user":
            assigned = _staff_location(sub, tenant_id)
            if assigned is not None and assigned != location_id:
                raise _not_found()
    return TenantContext(tenant_id, tenant_row, role, sub, location, claims)


def for_public(location_id: str, *, feature: Optional[str] = None) -> TenantContext:
    """Public (no-login) routes: the location decides the tenant. Unknown
    locations, inactive tenants and features outside the plan all look like
    nothing is there (404) - a suspended restaurant's site simply has no
    menu or availability."""
    if not isinstance(location_id, str) or not location_id.strip():
        raise _not_found()
    tenant_id = tenant_of_location(location_id)
    if not tenant_id:
        raise _not_found()
    tenant_row = get_tenant(tenant_id)
    if not tenant_row or tenant_row.get("status") != "active":
        raise _not_found()
    try:
        require_feature(tenant_row, feature)
    except TenantError:
        raise _not_found()
    location = get_location(tenant_id, location_id, consistent=False)
    if not location:
        raise _not_found()
    return TenantContext(tenant_id, tenant_row, None, None, location)


def reset_caches() -> None:
    """Tests only."""
    _tenant_cache.clear()
    _location_tenant_cache.clear()
