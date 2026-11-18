"""Booqy Console calling a tenant Lambda on a restaurant's behalf.

Booqy's operators manage every sign-in account of any customer from the
console's Users tab (invite, edit, role, staff permissions, disable, enable,
remove). The rules for that already live in manage-user and tenant-account
(the admin app's Personal page and Behörigheter), so the console's
platform-tenants Lambda invokes those functions directly instead of keeping a
second copy.

A direct invoke (lambda:InvokeFunction, granted to platform-tenants only)
carries

    {"booqyConsole": {"operator": "<operator username>", "tenantId": "<id>",
                      "method": "PUT", "path": "/users/<sub>", "body": {...}}}

and is turned into the API Gateway event the handler already understands,
with the claims of an owner of that restaurant: the caller is
"operator:<username>" (audit fields such as createdBy and updatedBy say who
it was). API Gateway never produces a top-level "booqyConsole" key, and an
event that has one next to API Gateway fields is refused, so a restaurant's
own token can't pass for an operator.

manage-user additionally treats this caller as able to manage owners
(is_operator()), which no restaurant user can.

Dev twins (dev only, booqy-console docs/DEV-TWINS.md): the console sets up a
customer's twin in dev through the admin's own functions (create-location,
get-menu, manage-menu, the layout functions, google-business, phone-agent,
tenant-account's DPA). Those call unwrap(event, dev_only=True): in prod such
a call is refused, so prod keeps the Users tab and nothing more. A call for a
route with path parameters names its API Gateway route template:

    {"booqyConsole": {..., "method": "PUT", "path": "/locations/01abc",
                      "route": "/locations/{locationId}"}}

and gets the routeKey and pathParameters API Gateway would give
({proxy+} takes the rest of the path).
"""

from __future__ import annotations

import contextvars
import functools
import json
import os
import re
from typing import Optional

KEY = "booqyConsole"
SUB_PREFIX = "operator:"
_OPERATOR = contextvars.ContextVar("booqy_console_operator", default=None)
_NAME = re.compile(r"^[A-Za-z0-9@._+-]{1,128}$")
_TENANT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


class Refused(ValueError):
    """A malformed console call (never reaches the route code)."""


_PARAM = re.compile(r"^\{([A-Za-z]+)(\+?)\}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9._~@:-]{1,200}$")


def _route_params(route, path: str) -> dict:
    """pathParameters for `path` under the route template `route`
    ("/locations/{locationId}/menu/{proxy+}"); Refused when it doesn't match."""
    if not isinstance(route, str) or not route.startswith("/"):
        raise Refused("route must be a route template")
    want, got = route.strip("/").split("/"), path.strip("/").split("/")
    params = {}
    for i, part in enumerate(want):
        match = _PARAM.match(part)
        if match and match.group(2):
            if i != len(want) - 1 or len(got) <= i or not all(_SEGMENT.match(g) for g in got[i:]):
                raise Refused("path does not match route")
            params[match.group(1)] = "/".join(got[i:])
            return params
        if i >= len(got):
            raise Refused("path does not match route")
        if match:
            if not _SEGMENT.match(got[i]):
                raise Refused("path does not match route")
            params[match.group(1)] = got[i]
        elif part != got[i]:
            raise Refused("path does not match route")
    if len(got) != len(want):
        raise Refused("path does not match route")
    return params


def unwrap(event: dict, *, prefix: str = "", dev_only: bool = False) -> dict:
    """The API Gateway event for a console call, else `event` unchanged.

    prefix: the route prefix the function's own paths carry in API Gateway
    ("" for tenant-account's /tenant, the proxy path for manage-user).
    dev_only: refuse console calls in prod (the dev twin seeder's functions)."""
    _OPERATOR.set(None)
    if not isinstance(event, dict) or KEY not in event:
        return event
    if dev_only and os.environ.get("ENVIRONMENT") == "prod":
        raise Refused("console calls are dev only here")
    if "requestContext" in event or "routeKey" in event or "rawPath" in event:
        raise Refused("console call mixed with API Gateway fields")
    call = event[KEY]
    if not isinstance(call, dict):
        raise Refused("console call must be an object")
    operator, tenant_id = call.get("operator"), call.get("tenantId")
    method, path, body = str(call.get("method") or "").upper(), call.get("path"), call.get("body")
    if not isinstance(operator, str) or not _NAME.match(operator):
        raise Refused("operator is required")
    if not isinstance(tenant_id, str) or not _TENANT.match(tenant_id):
        raise Refused("tenantId is required")
    if method not in _METHODS or not isinstance(path, str) or not path.startswith("/"):
        raise Refused("method and path are required")
    if body is not None and not isinstance(body, dict):
        raise Refused("body must be an object")
    route = call.get("route")
    if route is not None:
        params = _route_params(route, path)
        route_key = f"{method} {route}"
    else:
        route_path = path[len(prefix):] if prefix and path.startswith(prefix) else path
        params = {"proxy": route_path.lstrip("/")} if prefix else {}
        route_key = f"{method} {path}"
    _OPERATOR.set(operator)
    claims = {
        "sub": f"{SUB_PREFIX}{operator}",
        "tenant_id": tenant_id,
        "role": "owner_user",
        "cognito:groups": "[owner_user]",
        "token_use": "access",
    }
    return {
        "version": "2.0",
        "routeKey": route_key,
        "rawPath": path,
        "pathParameters": params,
        "requestContext": {"http": {"method": method, "path": path},
                           "authorizer": {"jwt": {"claims": claims}}},
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }


def is_operator() -> bool:
    """The request being handled came from Booqy Console."""
    return _OPERATOR.get() is not None


def operator() -> Optional[str]:
    return _OPERATOR.get()


def dev_twin_entry(handler):
    """Lets a function's Lambda handler take the dev twin seeder's console
    calls (dev only; prod answers 400). API Gateway events pass unchanged."""
    @functools.wraps(handler)
    def wrapped(event, context=None):
        try:
            event = unwrap(event, dev_only=True)
        except Refused as exc:
            return {"statusCode": 400, "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"error": "console_refused", "message": str(exc)})}
        return handler(event, context)
    return wrapped
