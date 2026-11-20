"""Structured logs and EMF metrics without transcripts or phone numbers
(FROZEN: every builder logs through this module, never print()).

`event(name, **fields)` writes one JSON line. Fields named like text are
dropped, strings are cut to 64 chars, and any run of 5 or more digits
(phone numbers, booking references) is masked, so a mistake in a caller
cannot leak a transcript or a number into CloudWatch. Ids are hashed with
`call_tag()` before they are logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time

NAMESPACE = "Booqy/VoiceGateway"
FORBIDDEN = frozenset({"text", "transcript", "reply", "sentence", "partial", "final", "caller", "from", "to",
                       "phone", "number", "e164", "name", "utterance", "ssml", "messages", "content"})
_DIGITS = re.compile(r"\d{5,}")
MAX_STR = 64

_logger = logging.getLogger("voice_gateway")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False

sink = None   # tests: a list to collect the dicts instead of writing them


def call_tag(value: str | None) -> str | None:
    """A short stable hash for ids (call id, call ref): joins log lines of
    one call without storing the id itself."""
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _clean(value):
    if isinstance(value, str):
        return _DIGITS.sub("#####", value[:MAX_STR])
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value[:20]]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in list(value.items())[:20] if str(k).lower() not in FORBIDDEN}
    return _clean(str(value))


def safe(fields: dict) -> dict:
    return {k: _clean(v) for k, v in fields.items() if k.lower() not in FORBIDDEN}


def _write(record: dict) -> None:
    if sink is not None:
        sink.append(record)
    else:
        _logger.info(json.dumps(record, ensure_ascii=False, default=str))


def event(name: str, level: str = "info", **fields) -> None:
    _write({"ts": int(time.time() * 1000), "event": name, "level": level, **safe(fields)})


def metric(name: str, value: float, unit: str = "Milliseconds", **dims) -> None:
    """One CloudWatch Embedded Metric Format line. Dimensions are low
    cardinality only (language, kind, env): never a call id."""
    dims = safe({k: v for k, v in dims.items() if v is not None})
    _write({"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
        "Namespace": NAMESPACE, "Dimensions": [sorted(dims)] if dims else [[]],
        "Metrics": [{"Name": name, "Unit": unit}]}]}, name: value, **dims})
