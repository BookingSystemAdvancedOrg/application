"""tenant-account

TRIGGER:
    API Gateway -- Auth: JWT (tenant user pool)
    GET   /tenant
    PATCH /tenant
    POST  /tenant/stripe/account-link

PURPOSE:
    The signed-in restaurant's own account: name, plan and what it allows,
    usage, Stripe onboarding status and domains (GET - owner and staff), the
    few fields an owner may edit themselves (PATCH: senderName,
    replyToEmail, branding) and a fresh Stripe onboarding link (POST, owner).
    Everything else about the tenant is managed by operators in sbs-admin.

    Multi-tenant: the tenant is the token's tenant_id claim - there is no
    tenantId in any path or body. Operator-only fields (lastError, the
    onboarding execution, audit rows, Stripe tax-rate ids) are never
    returned. A suspended/offboarded tenant gets 403 tenant_inactive.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    TENANT_TABLE_NAME -- tenant table (PROFILE + DOMAIN# rows)
    LOCATION_TABLE_NAME / LOCATION_ID_INDEX_NAME -- tenant context
    STRIPE_SECRET_ARN -- platform Stripe key, Secrets Manager {"apiKey": ...}
    STRIPE_API_VERSION -- Stripe-Version header
    ADMIN_APP_URL -- admin app base URL (Stripe return/refresh links)

AWS RESOURCE ACCESS:
    Tenant table GetItem/Query; UpdateItem limited by IAM to senderName,
    replyToEmail, branding, updatedAt, updatedBy with ReturnValues
    NONE/UPDATED_*; GetSecretValue on the platform Stripe key.

Full details: infrastructure docs/handoff/BACKEND.md section 6.2
"""

import base64
import binascii
import json
import math
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import tenant
from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
TENANT_TABLE_NAME = os.environ["TENANT_TABLE_NAME"]

_STRIPE_API = "https://api.stripe.com"
_ROUTES = {
    ("GET", "/tenant"),
    ("PATCH", "/tenant"),
    ("POST", "/tenant/stripe/account-link"),
}
_EDITABLE = ("senderName", "replyToEmail", "branding")
# SMS sender ids: max 11 characters, letters/digits/space only.
_SENDER_NAME = re.compile(r"[A-Za-z0-9 ]{1,11}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_MAX_BRANDING_BYTES = 4096
_MAX_BRANDING_DEPTH = 8
_stripe_key = None


class _ServiceFailure(Exception):
    """A dependency answered something unusable."""


def _response(status, body, headers=None):
    return json_response(
        status, body, headers={"Cache-Control": "no-store", **(headers or {})}
    )


def _error(status, message):
    return _response(status, {"error": message})


def _route(event):
    request_context = event.get("requestContext") or {}
    http = request_context.get("http") if isinstance(request_context, dict) else None
    http = http if isinstance(http, dict) else {}
    method = http.get("method")
    method = method.upper() if isinstance(method, str) else ""
    route_key = event.get("routeKey")
    if isinstance(route_key, str) and " " in route_key:
        path = route_key.split(" ", 1)[1]
    else:
        path = http.get("path") if isinstance(http.get("path"), str) else ""
    return method, path.rstrip("/") or "/"


def _now():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# --- GET /tenant ---------------------------------------------------------------

def _domains(tenant_id, primary):
    rows, request = [], {
        "KeyConditionExpression": (
            Key("PK").eq(tenant.tenant_pk(tenant_id))
            & Key("SK").begins_with("DOMAIN#")
        ),
    }
    while True:
        page = table(TENANT_TABLE_NAME).query(**request)
        rows.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            break
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    domains = []
    for row in rows:
        domain = row.get("domain") or row["SK"].split("#", 1)[1]
        domains.append({
            "domain": domain,
            "kind": row.get("kind"),
            "status": row.get("status"),
            "cnameTarget": row.get("cnameTarget"),
            "primary": domain == primary,
        })
    return sorted(domains, key=lambda d: d["domain"])


def _public_tenant(tenant_id, row, role):
    stripe = row.get("stripe") or {}
    entitlements = row.get("entitlements") or {}
    return {
        "tenantId": tenant_id,
        "name": row.get("name"),
        "slug": row.get("slug"),
        "status": row.get("status"),
        "planId": row.get("planId"),
        "entitlements": {
            "maxLocations": entitlements.get("maxLocations"),
            "features": entitlements.get("features") or {},
        },
        "locationCount": row.get("locationCount", 0),
        "stripe": {
            "connected": bool(stripe.get("accountId")),
            "chargesEnabled": bool(stripe.get("chargesEnabled")),
            "payoutsEnabled": bool(stripe.get("payoutsEnabled")),
            "detailsSubmitted": bool(stripe.get("detailsSubmitted")),
        },
        "primaryDomain": row.get("primaryDomain"),
        "domains": _domains(tenant_id, row.get("primaryDomain")),
        "senderName": row.get("senderName"),
        "replyToEmail": row.get("replyToEmail"),
        "branding": row.get("branding") or {},
        "role": role,
    }


# --- PATCH /tenant ---------------------------------------------------------------

def _body(event):
    raw = event.get("body")
    if raw is None or raw == "":
        raise ValueError("request body is required")
    if event.get("isBase64Encoded") is True:
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, TypeError):
            raise ValueError("request body must be valid JSON") from None
    try:
        # Decimal, not float: DynamoDB stores numbers as Decimal only.
        body = json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant)
    except (TypeError, ValueError):
        raise ValueError("request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    return body


def _reject_constant(name):
    raise ValueError(f"{name} is not allowed")


def _storable(value, depth=0):
    """Branding must be storable in DynamoDB: no empty map keys, finite
    numbers, bounded nesting."""
    if depth > _MAX_BRANDING_DEPTH:
        return False
    if isinstance(value, dict):
        return all(
            isinstance(k, str) and k and _storable(v, depth + 1)
            for k, v in value.items()
        )
    if isinstance(value, list):
        return all(_storable(v, depth + 1) for v in value)
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, (str, bool, int))


def _validated_edit(body):
    """None or "" removes a field (falls back to the platform default)."""
    unknown = sorted(set(body) - set(_EDITABLE))
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(unknown)}")
    if not body:
        raise ValueError("nothing to update")
    fields = {}
    if "senderName" in body:
        value = body["senderName"]
        if value in (None, ""):
            fields["senderName"] = None
        elif not isinstance(value, str) or not _SENDER_NAME.fullmatch(value.strip()):
            raise ValueError("senderName must be 1-11 letters, digits or spaces")
        else:
            fields["senderName"] = value.strip()
    if "replyToEmail" in body:
        value = body["replyToEmail"]
        if value in (None, ""):
            fields["replyToEmail"] = None
        elif (
            not isinstance(value, str)
            or len(value.strip()) > 320
            or not _EMAIL.fullmatch(value.strip())
        ):
            raise ValueError("replyToEmail must be a valid email address")
        else:
            fields["replyToEmail"] = value.strip().lower()
    if "branding" in body:
        value = body["branding"]
        if value in (None, {}):
            fields["branding"] = None
        elif (
            not isinstance(value, dict)
            or not _storable(value)
            or len(json.dumps(value, default=str).encode("utf-8")) > _MAX_BRANDING_BYTES
        ):
            raise ValueError("branding must be an object of at most 4 KB")
        else:
            fields["branding"] = value
    return fields


def _apply_edit(ctx, fields):
    sets = ["updatedAt = :now", "updatedBy = :by"]
    removes = []
    names = {}
    values = {":now": _now(), ":by": ctx.sub}
    for index, (field, value) in enumerate(fields.items()):
        names[f"#f{index}"] = field
        if value is None:
            removes.append(f"#f{index}")
        else:
            sets.append(f"#f{index} = :v{index}")
            values[f":v{index}"] = value
    expression = "SET " + ", ".join(sets)
    if removes:
        expression += " REMOVE " + ", ".join(removes)
    table(TENANT_TABLE_NAME).update_item(
        Key={"PK": tenant.tenant_pk(ctx.tenant_id), "SK": "PROFILE"},
        UpdateExpression=expression,
        ConditionExpression="attribute_exists(PK)",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        # IAM refuses ALL_OLD/ALL_NEW: they would return the whole row.
        ReturnValues="NONE",
    )


def _fresh_profile(tenant_id):
    item = tenant.get_tenant(tenant_id, fresh=True)
    if not isinstance(item, dict):
        raise _ServiceFailure
    return item


# --- POST /tenant/stripe/account-link --------------------------------------------

def _stripe_api_key():
    global _stripe_key
    if _stripe_key is None:
        secret = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["STRIPE_SECRET_ARN"]
        )
        _stripe_key = json.loads(secret["SecretString"])["apiKey"]
    return _stripe_key


def _stripe_v2_post(path, body):
    request = urllib.request.Request(
        f"{_STRIPE_API}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {_stripe_api_key()}",
            "Content-Type": "application/json",
            "Stripe-Version": os.environ["STRIPE_API_VERSION"],
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise _ServiceFailure from None


def _account_link(ctx):
    account_id = (ctx.tenant.get("stripe") or {}).get("accountId")
    if not account_id:
        # Onboarding creates the account; until then there is nothing to link.
        return _error(HTTPStatus.CONFLICT.value, "no_stripe_account")
    base = os.environ["ADMIN_APP_URL"].rstrip("/")
    link = _stripe_v2_post("/v2/core/account_links", {
        "account": account_id,
        "use_case": {
            "type": "account_onboarding",
            "account_onboarding": {
                "configurations": ["merchant"],
                "refresh_url": f"{base}/settings/payments?stripe=refresh",
                "return_url": f"{base}/settings/payments?stripe=return",
            },
        },
    })
    if not isinstance(link, dict) or not isinstance(link.get("url"), str):
        raise _ServiceFailure
    return _response(
        HTTPStatus.OK.value,
        {"url": link["url"], "expiresAt": link.get("expires_at")},
    )


# --- handler ------------------------------------------------------------------------

def handler(event, context):
    method, path = _route(event)
    if (method, path) not in _ROUTES:
        allowed = sorted(m for m, p in _ROUTES if p == path)
        if allowed:
            return _response(
                HTTPStatus.METHOD_NOT_ALLOWED.value,
                {"error": "method not allowed"},
                headers={"Allow": ", ".join(allowed)},
            )
        return _error(HTTPStatus.NOT_FOUND.value, "not found")

    try:
        # Reading is for everyone in the restaurant; changing is owner only.
        ctx = tenant.for_jwt(event, owner_only=method != "GET")
        if method == "GET":
            # Fresh, not the per-container cache: the owner must see a change
            # (an edit, a new location, Stripe onboarding) immediately.
            return _response(
                HTTPStatus.OK.value,
                _public_tenant(ctx.tenant_id, _fresh_profile(ctx.tenant_id), ctx.role),
            )
        if method == "PATCH":
            try:
                _apply_edit(ctx, _validated_edit(_body(event)))
            finally:
                tenant.invalidate(ctx.tenant_id)
            return _response(
                HTTPStatus.OK.value,
                _public_tenant(ctx.tenant_id, _fresh_profile(ctx.tenant_id), ctx.role),
            )
        return _account_link(
            tenant.TenantContext(ctx.tenant_id, _fresh_profile(ctx.tenant_id), ctx.role, ctx.sub)
        )
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return _error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except (BotoCoreError, ClientError, _ServiceFailure, KeyError):
        return _error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "account service unavailable",
        )
