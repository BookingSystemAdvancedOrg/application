"""Helpers for building API Gateway (HTTP API v2) Lambda proxy responses.

Not used by the three functions that aren't behind API Gateway
(notification, no-show-check, stripe-webhook) - see each of their
docstrings for what they should return/do instead.
"""

import json
from decimal import Decimal
from typing import Any, Optional


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
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "body": json.dumps(body, default=_json_default),
    }


def error_response(status_code: int, message: str) -> dict:
    """Shorthand for the common case of returning a single error message."""
    return json_response(status_code, {"error": message})
