"""Menu dish visibility: the manual ``active`` toggle plus an optional
schedule window ``availableFrom`` / ``availableUntil`` on ``MENU#`` items.

Both schedule fields are UTC strings ("2026-03-29T01:00:00Z"); a missing
attribute means no bound. The window is [availableFrom, availableUntil):
the start is inclusive, the end exclusive ("until 14:00" is gone at 14:00).
Toggle off always wins. Read time decides visibility, no jobs flip flags.
"""

from datetime import datetime, timezone

SCHEDULE_FIELDS = ("availableFrom", "availableUntil")


def parse_utc(value):
    """A "...Z" timestamp -> aware UTC datetime; anything else ValueError."""
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
        raise ValueError("timestamp must be a UTC date-time ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("timestamp must be a UTC date-time ending in Z") from None
    if "T" not in value:
        raise ValueError("timestamp must be a UTC date-time ending in Z")
    return parsed.astimezone(timezone.utc)


def _format(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_schedule(body, stored=None):
    """Validates the schedule fields present in ``body`` (a create or a
    partial PUT, checked against the ``stored`` item). Returns
    {field: normalized string or None}; None means clear the field."""
    fields = {}
    for field in SCHEDULE_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if value is None:
            fields[field] = None
            continue
        try:
            fields[field] = _format(parse_utc(value))
        except ValueError:
            raise ValueError(f"{field} must be a UTC date-time ending in Z or null") from None

    merged = {field: (stored or {}).get(field) for field in SCHEDULE_FIELDS}
    merged.update(fields)
    start, end = merged["availableFrom"], merged["availableUntil"]
    if start and end and parse_utc(end) <= parse_utc(start):
        raise ValueError("availableUntil must be after availableFrom")
    return fields


def is_available(item, now):
    """True when a guest may see and order the dish at ``now`` (aware UTC).
    A malformed stored bound raises ValueError."""
    if item.get("active") is not True:
        return False
    start, end = item.get("availableFrom"), item.get("availableUntil")
    if start is not None and now < parse_utc(start):
        return False
    if end is not None and now >= parse_utc(end):
        return False
    return True
