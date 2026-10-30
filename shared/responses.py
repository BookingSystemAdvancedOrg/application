"""Helpers for building API Gateway (HTTP API v2) Lambda proxy responses.

Not used by the three functions that aren't behind API Gateway
(notification, no-show-check, stripe-webhook) - see each of their
docstrings for what they should return/do instead.
"""

import json
import re
from decimal import Decimal
from typing import Any, Optional

# Error bodies: {"error": <what it always was>, "code": <stable snake_case>,
# "field": <the request field it is about, when a validation message names
# one>}. `error` stays as it was (older frontends match on it); `code` is
# what clients map to copy. A body whose `error` already is a code gets no
# extra keys, so {"error": "table_taken"} is unchanged.
_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
# legacy short phrases that are codes in all but spelling ("not found")
_PHRASE = re.compile(r"^[a-z]+(?: [a-z]+){1,4}$")
_VALIDATION = re.compile(r"\b(?:is|are|must|may|contains|cannot|can't|has|needs|should|isn't|doesn't)\b")
_FIELD = re.compile(r"^([a-z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+|\[\d+\])*) (?:is|are|must|may|contains|cannot|can't|has|needs|should|isn't|doesn't)\b")
_NOT_FIELDS = {"request", "body", "query", "it", "this", "the", "value", "path"}
# Python's own ValueError texts: never shown to a caller.
_INTERNAL = re.compile(
    r"invalid literal for|Invalid isoformat|could not convert|unconverted data|does not match format"
    r"|out of range|not enough values|too many values|object has no attribute|Traceback|NoneType"
    r"|botocore|boto3|DynamoDB|ConditionalCheckFailed|ProvisionedThroughput|stripe\.com|scrive",
    re.IGNORECASE,
)
_BY_STATUS = {400: "invalid_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
              405: "method_not_allowed", 409: "conflict", 413: "too_large", 415: "unsupported_media_type",
              422: "invalid_request", 429: "too_many_requests"}
_MAX_MESSAGE = 300


def error_fields(status_code: int, message: str) -> dict:
    """The error body for a status and message (see the note above)."""
    if _CODE.match(message):
        return {"error": message}
    if _INTERNAL.search(message):
        return {"error": "invalid value" if status_code < 500 else "internal_error",
                "code": _BY_STATUS.get(status_code, "internal_error") if status_code < 500 else "internal_error"}
    message = message[:_MAX_MESSAGE]
    if _PHRASE.match(message) and not _VALIDATION.search(message):
        return {"error": message, "code": message.replace(" ", "_")}
    body = {"error": message}
    if status_code >= 500:
        body["code"] = "internal_error"
        return body
    field = _FIELD.match(message)
    if "JSON" in message:
        body["code"] = "invalid_json"
    elif message.startswith("unsupported fields"):
        body["code"] = "unsupported_fields"
    elif message.endswith(" is required") or message.endswith(" are required"):
        body["code"] = "required"
    else:
        body["code"] = "invalid_value" if field else _BY_STATUS.get(status_code, "invalid_request")
    if field and field.group(1) not in _NOT_FIELDS:
        body["field"] = field.group(1)
    return body


def _json_default(value: Any) -> Any:
    # DynamoDB's boto3 resource API returns numbers as Decimal - json.dumps
    # doesn't know how to serialize those on its own.
    if isinstance(value, Decimal):
        return (
            int(value)
            if value == value.to_integral_value()
            else float(value)
        )
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def json_response(status_code: int, body: Any, headers: Optional[dict] = None) -> dict:
    """Build a Lambda proxy response dict for an API Gateway v2 (HTTP API) route."""
    if status_code >= 400 and isinstance(body, dict) and isinstance(body.get("error"), str) and "code" not in body:
        body = {**body, **error_fields(status_code, body["error"])}
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "body": json.dumps(body, default=_json_default),
    }


def error_response(status_code: int, message: str) -> dict:
    """Shorthand for the common case of returning a single error message."""
    return json_response(status_code, {"error": message})
