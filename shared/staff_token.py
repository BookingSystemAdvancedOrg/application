"""The caller's own tenant on a NONE-auth route, from a Cognito access token.

Admin calls some public routes (GET /locations/{id}/availability) with the
staff member's tenant-pool access token in `Authorization: Bearer ...`. API
Gateway does not check it on a NONE-auth route, so this asks Cognito itself:
GetUser accepts only a valid, unexpired, unrevoked access token and is
authorized by that token, not by IAM (the client is unsigned, the function
needs no new permission).

Use it only to lift a gate for the caller's own restaurant (tenant.for_public
`staff_tenant`): the tenant of the request is still decided by the location.
Any failure (no header, bad token, Cognito down) means "not staff".
"""

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


def tenant_of(event) -> Optional[str]:
    """custom:tenant_id of the signed-in caller, or None."""
    token = bearer(event)
    if token is None:
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
