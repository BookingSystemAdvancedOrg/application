"""A dish photo's focal point: ``imageFocus`` = {"x": 0-100, "y": 0-100}.

Percentages of the image width and height that stay in view when a frame
crops the photo (CSS ``object-position: x% y%``). Whole numbers only, so a
stored value round-trips through DynamoDB as an exact Decimal. Missing or
null means the centre (50 50); null in a PUT clears it.
"""

from decimal import Decimal

FIELD = "imageFocus"
_AXES = ("x", "y")


def _percent(value, axis):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"imageFocus.{axis} must be a whole number from 0 to 100")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"imageFocus.{axis} must be a whole number from 0 to 100")
    if isinstance(value, Decimal) and (not value.is_finite() or value != value.to_integral_value()):
        raise ValueError(f"imageFocus.{axis} must be a whole number from 0 to 100")
    number = int(value)
    if not 0 <= number <= 100:
        raise ValueError(f"imageFocus.{axis} must be a whole number from 0 to 100")
    return number


def parse(raw):
    """{"x", "y"} -> {"x": int, "y": int}; None stays None. ValueError otherwise."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != set(_AXES):
        raise ValueError("imageFocus must be an object with x and y")
    return {axis: _percent(raw[axis], axis) for axis in _AXES}


def validate(body):
    """The focal point present in a create or partial PUT ``body``:
    {} when absent, {"imageFocus": value or None} otherwise."""
    if FIELD not in body:
        return {}
    return {FIELD: parse(body[FIELD])}


def view(item):
    """The response field for a stored item: {} when unset (the centre),
    so older records and clients are unchanged. A malformed stored value
    is dropped rather than failing the whole menu."""
    try:
        focus = parse(item.get(FIELD))
    except ValueError:
        return {}
    return {FIELD: focus} if focus else {}
