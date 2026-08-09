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

from typing import List


class Unauthorized(Exception):
    """Raise from a handler to short-circuit to a 401/403 response."""


def get_claims(event: dict) -> dict:
    """Verified Cognito access token claims for a JWT-protected route."""
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]
    except KeyError as exc:
        raise Unauthorized("no JWT claims on this request") from exc


def get_sub(event: dict) -> str:
    """Cognito user id (`sub`) of the caller - use this to look up their
    User table row, e.g. GetItem on PK = f"USER#{sub}" (the pattern
    established for block-table; reuse it anywhere else that needs to know
    which location the caller is assigned to).
    """
    return get_claims(event)["sub"]


def get_groups(event: dict) -> List[str]:
    """Cognito groups the caller belongs to: staff / owner_user / super_user.
    Empty list if absent from the claims - don't assume the key is present.
    """
    raw = get_claims(event).get("cognito:groups", [])
    return list(raw) if raw else []


def require_group(event: dict, *allowed: str) -> None:
    """Raise Unauthorized unless the caller is in one of the allowed groups.

    Usage:
        require_group(event, "owner_user", "super_user")
    """
    if not set(get_groups(event)) & set(allowed):
        raise Unauthorized(f"caller is not in one of {allowed}")
