"""Request parsing for API Gateway HTTP API (v2) events."""

import base64
import binascii
import hmac
import json
import os
from decimal import Decimal

from shared.responses import json_response

_MAX_ID = 128


def route(event):
    """(METHOD, "/path/{template}") from the route key."""
    route_key = event.get("routeKey") if isinstance(event, dict) else None
    if isinstance(route_key, str) and " " in route_key:
        method, path = route_key.split(" ", 1)
        return method.upper(), path
    http = ((event or {}).get("requestContext") or {}).get("http") or {}
    return str(http.get("method", "")).upper(), str(http.get("path", ""))


def path_id(event, name):
    params = event.get("pathParameters") if isinstance(event, dict) else None
    value = (params or {}).get(name) if isinstance(params, dict) else None
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_ID or "#" in value:
        raise ValueError(f"{name} is invalid")
    return value.strip()


def query(event):
    value = event.get("queryStringParameters") if isinstance(event, dict) else None
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("query parameters must be an object")
    return value


def edge_header(event, name):
    """A header set by the admin app's CloudFront, or None.

    Behind CloudFront (/api/* on the admin app's address) the edge adds the
    viewer's IP and host (x-booqy-viewer-ip, x-booqy-viewer-host) plus a
    shared secret in x-booqy-edge. Those headers are only trusted when the
    secret matches CLOUDFRONT_EDGE_SECRET, so a caller going straight to API
    Gateway can't set them itself.
    """
    secret = os.environ.get("CLOUDFRONT_EDGE_SECRET") or ""
    if not secret:
        return None
    edge = header(event, "x-booqy-edge") or ""
    if not edge or not hmac.compare_digest(edge.encode(), secret.encode()):
        return None
    value = (header(event, name) or "").strip()
    return value or None


def source_ip(event):
    """The caller's IP address (None if absent): the viewer's when the
    request came through the admin app's CloudFront, else API Gateway's."""
    viewer = edge_header(event, "x-booqy-viewer-ip")
    if viewer:
        return viewer
    context = event.get("requestContext") if isinstance(event, dict) else None
    http = (context or {}).get("http") if isinstance(context, dict) else None
    value = (http or {}).get("sourceIp") if isinstance(http, dict) else None
    return value if isinstance(value, str) and value else None


def header(event, name):
    headers = event.get("headers") if isinstance(event, dict) else None
    if not isinstance(headers, dict):
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == wanted:
            return value if isinstance(value, str) else None
    return None


def _reject_constant(name):
    raise ValueError(f"{name} is not allowed")


def body(event, *, allowed=None, required=True):
    raw = event.get("body") if isinstance(event, dict) else None
    if raw is None or raw == "":
        if required:
            raise ValueError("request body is required")
        return {}
    if event.get("isBase64Encoded") is True:
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, TypeError):
            raise ValueError("request body must be valid JSON") from None
    try:
        parsed = json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant)
    except (TypeError, ValueError):
        raise ValueError("request body must be valid JSON") from None
    if not isinstance(parsed, dict):
        raise ValueError("request body must be a JSON object")
    if allowed is not None:
        unknown = sorted(set(parsed) - set(allowed))
        if unknown:
            raise ValueError(f"unsupported fields: {', '.join(unknown)}")
    return parsed


def respond(status, payload, headers=None):
    return json_response(status, payload, headers={"Cache-Control": "no-store", **(headers or {})})


def error(status, code):
    return respond(status, {"error": code})
