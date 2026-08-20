"""Authorization helpers for JWT-protected routes.

Per LAMBDA_REFERENCE.md: API Gateway's native JWT authorizer only proves the
request carries a valid, signed Cognito access token - it does NOT check
which Cognito group the caller is in, and does NOT check whether the caller
is allowed to act on the specific locationId/resource in the path. Both of
those checks are this module's job, called explicitly inside every
JWT-protected handler.

Do not import/use this module from a NONE-auth function (get-menu,
get-availability, create-pending-reservation, cancel-reservation,
manage-auth) - there is no JWT claims block on those requests at all.
"""

import json
from typing import List


class Unauthorized(Exception):
    """Raise from a handler to short-circuit to a 401/403 response."""


def get_claims(event: dict) -> dict:
    """Verified Cognito access token claims for a JWT-protected route."""
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    except (KeyError, TypeError) as exc:
        raise Unauthorized("no JWT claims on this request") from exc

    if not isinstance(claims, dict):
        raise Unauthorized("no JWT claims on this request")

    return claims


def get_sub(event: dict) -> str:
    """Cognito user id (`sub`) of the caller - use this to look up their
    User table row, e.g. GetItem on PK = f"USER#{sub}" (the pattern
    established for block-table; reuse it anywhere else that needs to know
    which location the caller is assigned to).
    """
    sub = get_claims(event).get("sub")

    if not isinstance(sub, str) or not sub.strip():
        raise Unauthorized("JWT is missing a subject")

    return sub


def get_groups(event: dict) -> List[str]:
    """Cognito groups the caller belongs to: staff_user / owner_user /
    super_user. Empty list if absent from the claims - don't assume the key
    is present.
    """
    raw = get_claims(event).get("cognito:groups", [])

    if not raw:
        return []

    if isinstance(raw, str):
        value = raw.strip()

        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = None

        if isinstance(decoded, list):
            raw = decoded
        elif isinstance(decoded, str):
            raw = [decoded]
        elif value.startswith("[") and value.endswith("]"):
            raw = value[1:-1].split(",")
        else:
            raw = [value]

    if not isinstance(raw, (list, tuple, set)):
        return []

    return [
        group.strip().strip('"\'')
        for group in raw
        if isinstance(group, str) and group.strip().strip('"\'')
    ]


def require_group(event: dict, *allowed: str) -> None:
    """Raise Unauthorized unless the caller is in one of the allowed groups.

    Usage:
        require_group(event, "owner_user", "super_user")
    """
    if not set(get_groups(event)) & set(allowed):
        raise Unauthorized(f"caller is not in one of {allowed}")
