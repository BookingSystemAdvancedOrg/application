"""Which location a call is about (owner requirement 2026-11-13).

One number per location is the default: the called number names the
location. A tenant can let several locations answer on one number
(`shareNumberWith`, L4); the agent then asks which restaurant first and
matches the caller's answer against the words that tell the locations apart
(for a restaurant in Malmö and Norrmalm: "malmö" and "norrmalm"). Until a
location is chosen the agent loop does not run, so no tool ever reads a
location the caller did not choose.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[^\W\d_]+")
_IGNORE = {"restaurang", "restaurangen", "restaurant", "the", "och", "and", "i", "in", "på", "at", "vid", "av",
           "of", "gatan", "vägen", "ab"}
MIN_PREFIX = 4


def words(text) -> list:
    return [w for w in _WORD.findall(str(text or "").casefold()) if w not in _IGNORE]


def _place_words(location) -> set:
    parts = [location.get("name")]
    address = location.get("address")
    if isinstance(address, dict):
        parts += [address.get("city"), address.get("area"), address.get("street")]
    else:
        parts.append(address)
    parts.append(location.get("city"))
    return {w for p in parts if p for w in words(p)}


def distinctive(locations) -> dict:
    """locationId -> the words only that location has (name, address, city)."""
    places = {loc["locationId"]: _place_words(loc) for loc in locations}
    out = {}
    for lid, own in places.items():
        others = set().union(*(w for k, w in places.items() if k != lid)) if len(places) > 1 else set()
        out[lid] = own - others
    return out


def label(location) -> str:
    """How the agent names a location in the question: its distinctive name."""
    return " ".join(str(location.get("name") or "").split())


def pick(text, locations):
    """The locationId the caller named, or None when the answer names none or
    more than one of them."""
    said = words(text)
    hits = set()
    for lid, own in distinctive(locations).items():
        for w in said:
            if w in own or (len(w) >= MIN_PREFIX and any(o.startswith(w) or w.startswith(o) for o in own
                                                          if len(o) >= MIN_PREFIX)):
                hits.add(lid)
    return hits.pop() if len(hits) == 1 else None


def rest_of(text, location, locations) -> list:
    """The caller's words besides the ones that named the location."""
    own = distinctive(locations).get(location["locationId"], set())
    return [w for w in words(text) if w not in own]
