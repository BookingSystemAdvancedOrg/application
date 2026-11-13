"""The SHA-256 hash of an append-only ledger entry (personalliggare): the
entry as sorted JSON without its own `hash`, DynamoDB Decimals as int or
str, so the hash is the same on write and on read. Each entry stores the
previous entry's hash as `prevHash`, which makes the ledger a chain."""

import hashlib
import json
from decimal import Decimal


def plain(value):
    """DynamoDB Decimals -> int/str so the hash is the same on write and read."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def entry_hash(entry):
    body = {k: v for k, v in plain(entry).items() if k != "hash"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
