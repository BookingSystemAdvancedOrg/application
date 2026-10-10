"""tenant-site-config

TRIGGER:
    API Gateway -- GET /site-config?host=<hostname>   -- Auth: NONE
                   GET /site-config?slug=<slug>       (outside prod only)

PURPOSE:
    Bootstrap of a restaurant's public website. One booking-site build is
    served on every restaurant's domain; at startup it asks which restaurant
    this hostname belongs to and gets everything it needs to render: name,
    branding, enabled features, the locations (address, contact, opening
    hours, booking rules), the legal entity for the privacy notice, and the
    public Stripe / Turnstile keys.

    Resolution: global row DOMAIN#<host>/TENANT -> tenantId, and the
    tenant's own TENANT#<t>/DOMAIN#<host> row must be active (sbs-admin
    writes both). Outside prod, sites on *.cloudfront.net / localhost can't
    be mapped by host, so SLUG#<slug>/TENANT is accepted instead.

    Unknown host/slug, inactive domain or inactive tenant: 404 (the site
    shows "not available"). Only public fields are returned - never owner
    contacts, plan internals, errors, Stripe readiness or tax rate ids.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    STRIPE_PUBLISHABLE_KEY, TURNSTILE_SITE_KEY

Full details: infrastructure docs/handoff/BACKEND.md section 6.3
"""

import os
import re

from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, guarantee, http, tenant

_HOST = re.compile(r"(?=.{1,253}\Z)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*")
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_CACHE = "public, max-age=300"
_FEATURES = ("reservations", "ordering", "catering", "terminal")
_BRANDING_STRINGS = ("logoUrl", "heroImageUrl", "primaryColor", "accentColor", "tagline",
                     "about", "faviconUrl", "instagramUrl", "facebookUrl")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_URL = re.compile(r"https://[^\s\"'<>]{1,500}")


def _not_found():
    return http.respond(404, {"error": "not found"}, {"Cache-Control": "no-store"})


def _normalize_host(raw):
    if not isinstance(raw, str):
        raise ValueError
    host = raw.strip().lower().rstrip(".")
    host = host.split(":", 1)[0]  # strip a port
    if not _HOST.fullmatch(host):
        raise ValueError
    return host


def _tenant_of(key):
    item = dynamo.table(os.environ["TENANT_TABLE_NAME"]).get_item(
        Key={"PK": key, "SK": "TENANT"}
    ).get("Item")
    tenant_id = (item or {}).get("tenantId")
    return tenant_id if isinstance(tenant_id, str) and tenant_id else None


def _domain_active(tenant_id, host):
    item = dynamo.table(os.environ["TENANT_TABLE_NAME"]).get_item(
        Key={"PK": tenant.tenant_pk(tenant_id), "SK": f"DOMAIN#{host}"}
    ).get("Item")
    return bool(item) and item.get("status") == "active"


def _branding(raw):
    """Only known, safe values: colors as #RRGGBB, images/links as https."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key in _BRANDING_STRINGS:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if key.endswith("Color"):
            if _COLOR.fullmatch(value):
                out[key] = value.lower()
        elif key.endswith("Url"):
            if _URL.fullmatch(value):
                out[key] = value
        else:
            out[key] = value[:1000]
    return out


def _address(value):
    """sbs-admin stores {street, postalCode, city, country}; the site shows one line."""
    if isinstance(value, str):
        return value.strip()[:300] or None
    if not isinstance(value, dict):
        return None
    street = str(value.get("street") or "").strip()
    town = " ".join(str(value.get(k) or "").strip() for k in ("postalCode", "city")).strip()
    country = str(value.get("country") or "").strip()
    parts = [street, town] + ([country] if country and country.upper() != "SE" else [])
    return ", ".join(p for p in parts if p)[:300] or None


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number == int(number) else number


def _public_location(row, tenant_row):
    return {
        # Card guarantee terms shown BEFORE booking (None = no card asked).
        "guarantee": guarantee.public_policy(row, tenant_row),
        "locationId": row.get("locationId"),
        "name": row.get("name"),
        "address": row.get("address"),
        "phoneNumber": row.get("phoneNumber"),
        "email": row.get("email"),
        "timezone": row.get("timezone"),
        "businessHours": row.get("businessHours") or {},
        "bookingDurationHours": _number(row.get("bookingDurationHours")),
        "maxPartySizeOnline": _number(row.get("maxPartySizeOnline")),
    }


def _site_config(params):
    unknown = sorted(set(params) - {"host", "slug"})
    if unknown or len(params) != 1:
        raise ValueError
    if "host" in params:
        host = _normalize_host(params["host"])
        tenant_id = _tenant_of(f"DOMAIN#{host}")
        if not tenant_id or not _domain_active(tenant_id, host):
            return None
    else:
        if os.environ["ENVIRONMENT"] == "prod":
            raise ValueError
        slug = params["slug"]
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug.strip().lower()):
            raise ValueError
        tenant_id = _tenant_of(f"SLUG#{slug.strip().lower()}")
        if not tenant_id:
            return None

    row = tenant.get_tenant(tenant_id, fresh=True)
    if not row or row.get("status") != "active":
        return None
    features = (row.get("entitlements") or {}).get("features") or {}
    locations = sorted(
        tenant.list_locations(tenant_id),
        key=lambda r: (str(r.get("createdAt") or "~"), str(r.get("name") or ""), r.get("SK", "")),
    )
    return {
        "tenant": {
            "tenantId": tenant_id,
            "name": row.get("name"),
            "slug": row.get("slug"),
            "branding": _branding(row.get("branding")),
            "features": {f: bool(features.get(f)) for f in _FEATURES},
        },
        "legal": {
            "legalName": row.get("legalName") or row.get("name"),
            "orgNumber": row.get("orgNumber"),
            "contactEmail": row.get("contactEmail") or row.get("replyToEmail"),
            "contactPhone": row.get("contactPhone"),
            "address": _address(row.get("address")),
        },
        "locations": [_public_location(r, row) for r in locations],
        "stripe": {
            "publishableKey": os.environ.get("STRIPE_PUBLISHABLE_KEY") or None,
            "accountId": (row.get("stripe") or {}).get("accountId"),
        },
        "turnstile": {"siteKey": os.environ.get("TURNSTILE_SITE_KEY") or None},
    }


def handler(event, context):
    method, path = http.route(event)
    if path != "/site-config":
        return _not_found()
    if method != "GET":
        return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET"})
    try:
        config = _site_config(http.query(event))
    except ValueError:
        return http.respond(400, {"error": "pass host=<hostname> (or slug= outside prod)"},
                            {"Cache-Control": "no-store"})
    except (BotoCoreError, ClientError):
        return http.respond(503, {"error": "site configuration unavailable"},
                            {"Cache-Control": "no-store"})
    if config is None:
        return _not_found()
    return http.respond(200, config, {"Cache-Control": _CACHE})
