"""The caller's own tenant on a NONE-auth route, from a Cognito access token.

Admin calls some public routes (GET /locations/{id}/availability) with the
staff member's tenant-pool access token in `Authorization: Bearer ...`. API
Gateway does not check it on a NONE-auth route, so this asks Cognito itself:
GetUser accepts only a valid, unexpired, unrevoked access token and is
authorized by that token, not by IAM (the client is unsigned, the function
needs no new permission).

GetUser accepts an access token from ANY user pool (the pool is read from
the token), so before asking Cognito the unverified claims must say: issued
by Booqy's tenant pool (TENANT_USER_POOL_ID), an access token, for the staff
app client (TENANT_APP_CLIENT_ID). GetUser then proves the token is genuine,
unexpired and not revoked. Without both env vars nobody counts as staff.

Use it only to lift a gate for the caller's own restaurant (tenant.for_public
`staff_tenant`): the tenant of the request is still decided by the location.
Any failure (no header, bad token, Cognito down) means "not staff".
"""

import base64
import binascii
import json
import os
from typing import Optional

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from shared import http

_MAX_TOKEN_LENGTH = 8192
_client = None


def _cognito():
    global _client
    if _client is None:
        _client = boto3.client(
            "cognito-idp",
            config=Config(
                signature_version=UNSIGNED,
                connect_timeout=2,
                read_timeout=2,
                retries={"max_attempts": 1},
            ),
        )
    return _client


def bearer(event) -> Optional[str]:
    value = http.header(event, "authorization") or ""
    scheme, _, token = value.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > _MAX_TOKEN_LENGTH:
        return None
    return token


def _claims(token: str) -> Optional[dict]:
    """The JWT payload, NOT verified (GetUser verifies the token)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
    except (binascii.Error, ValueError):
        return None
    return claims if isinstance(claims, dict) else None


def _from_tenant_pool(token: str) -> bool:
    pool_id = os.environ.get("TENANT_USER_POOL_ID", "").strip()
    client_id = os.environ.get("TENANT_APP_CLIENT_ID", "").strip()
    if not pool_id or not client_id:
        return False
    claims = _claims(token)
    if claims is None:
        return False
    region = pool_id.split("_", 1)[0]
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
    return (
        claims.get("iss") == issuer
        and claims.get("token_use") == "access"
        and claims.get("client_id") == client_id
    )


def tenant_of(event) -> Optional[str]:
    """custom:tenant_id of the signed-in caller, or None."""
    token = bearer(event)
    if token is None or not _from_tenant_pool(token):
        return None
    try:
        user = _cognito().get_user(AccessToken=token)
    except (BotoCoreError, ClientError):
        return None
    for attribute in user.get("UserAttributes") or []:
        if attribute.get("Name") == "custom:tenant_id":
            value = attribute.get("Value")
            return value if isinstance(value, str) and value else None
    return None
