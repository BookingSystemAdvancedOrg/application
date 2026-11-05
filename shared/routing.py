"""Greedy API Gateway routes -> the handler's per-endpoint ROUTES keys.

The dev HTTP API has no room for more routes, so the JWT routes of the Run 0-2
features are registered as greedy routes per path prefix (for example
`ANY /locations/{locationId}/phone-agent` and
`ANY /locations/{locationId}/phone-agent/{proxy+}`, likewise under
`/tenant/<feature>`) behind the same Cognito authorizer. Such an event carries
the greedy route key and `pathParameters` of `{locationId, proxy}`, which the
handlers' `ROUTES` dicts (keyed `"GET /locations/{locationId}/phone-agent/calls"`)
do not know.

`resolve(event, routes)` rewrites the event in place to what the per-endpoint
route would have delivered: `routeKey` becomes the matching ROUTES key and
`pathParameters` its URL-decoded template values. The handler's own dispatch,
tenant and role checks and error bodies then run unchanged. When nothing
matches (unknown sub-path, wrong method) the event is left as it is, so the
handler answers with its existing error (404 "not found"). An event whose
`routeKey` is already a ROUTES key (a per-endpoint route, or a test) is never
touched, so the old routes keep working while the greedy ones roll out.

Matching follows API Gateway: method and segment count must be equal, a
literal segment beats a `{param}` segment at the same position, a `{param}`
matches exactly one non-empty segment, and parameter values are percent-decoded
(`unquote`, so `+` stays `+`) while literals are compared as sent. A stage
prefix in `rawPath` (`requestContext.stage` other than `$default`) is removed
before matching.
"""

from urllib.parse import unquote

_DEFAULT_STAGE = "$default"


def resolve(event, routes):
    """Set `event["routeKey"]` and `event["pathParameters"]` from the request
    path when `routes` has a matching per-endpoint key; returns the event.
    No-op when the event already carries a key of `routes` or nothing matches."""
    if not isinstance(event, dict) or not isinstance(routes, dict):
        return event
    route_key = event.get("routeKey")
    if isinstance(route_key, str) and route_key in routes:
        return event
    method, path = _request(event)
    if method is None or path is None:
        return event
    match = _match(method, path, routes)
    if match is None:
        return event
    key, params = match
    event["routeKey"] = key
    event["pathParameters"] = params
    return event


def _request(event):
    """(METHOD, path without the stage prefix) or (None, None)."""
    context = event.get("requestContext")
    context = context if isinstance(context, dict) else {}
    http = context.get("http")
    http = http if isinstance(http, dict) else {}
    method = http.get("method")
    path = event.get("rawPath")
    if not isinstance(path, str):
        path = http.get("path")
    if not isinstance(method, str) or not method or not isinstance(path, str) or not path.startswith("/"):
        return None, None
    stage = context.get("stage")
    if isinstance(stage, str) and stage and stage != _DEFAULT_STAGE:
        prefix = "/" + stage
        if path == prefix:
            path = "/"
        elif path.startswith(prefix + "/"):
            path = path[len(prefix):]
    return method.upper(), path


def _match(method, path, routes):
    """The most specific (key, params) of `routes` for METHOD + raw path, or None."""
    segments = path.split("/")[1:]
    best = None
    for key in routes:
        if not isinstance(key, str) or " " not in key:
            continue
        key_method, template = key.split(" ", 1)
        if key_method.upper() != method or not template.startswith("/"):
            continue
        parts = template.split("/")[1:]
        if len(parts) != len(segments):
            continue
        params, specificity = {}, []
        for part, segment in zip(parts, segments):
            if _is_param(part):
                if not segment:
                    break
                params[part[1:-1]] = unquote(segment)
                specificity.append(0)
            elif part == segment:
                specificity.append(1)
            else:
                break
        else:
            candidate = (tuple(specificity), key, params)
            if best is None or candidate[0] > best[0]:
                best = candidate
    return None if best is None else (best[1], best[2])


def _is_param(part):
    return len(part) > 2 and part.startswith("{") and part.endswith("}") and not part.endswith("+}")
