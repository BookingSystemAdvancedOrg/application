"""Road distance for catering delivery prices (issue 5, C6).

Amazon Location Service v2 (Core APIs, the Lambda's own region, IAM auth, no
API key): geocode both addresses, then the driving distance between them.
The guest's address is geocoded single use (IntendedUse SingleUse, never
stored). The restaurant's origin is geocoded once with IntendedUse Storage
when the owner saves the settings and kept on the location, so a quote
needs one geocode and one route. The computed distance per (origin,
address) is kept in this Lambda's memory for 15 minutes, so a guest who only
changes quantities does not trigger new lookups.

`distance_km` answers None whenever the distance can't be trusted (an
address that only matches a town or a postal code, nothing found, the
service down); the caller then lets the restaurant set the delivery fee by
hand instead of guessing.
"""

import hashlib
import logging
import time
from decimal import ROUND_HALF_UP, Decimal

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()

# Matches precise enough to price a delivery by (a town centre is not).
_PRECISE = {"PointAddress", "InterpolatedAddress", "SecondaryAddress", "Street", "PointOfInterest"}
_clients = {}
_CACHE_SECONDS = 15 * 60
_CACHE_MAX = 1000
_distances = {}  # sha256(origin|destination) -> (km or None, monotonic time)


def _client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def position(text, *, store=False):
    """[lon, lat] of a Swedish address, or None. store=True: a result we may
    keep (IntendedUse Storage, the restaurant's origin only)."""
    result = _client("geo-places").geocode(QueryText=text[:200], MaxResults=1, Language="sv",
                                           Filter={"IncludeCountries": ["SWE"]},
                                           IntendedUse="Storage" if store else "SingleUse")
    items = result.get("ResultItems") or []
    if not items or items[0].get("PlaceType") not in _PRECISE or not items[0].get("Position"):
        return None
    return items[0]["Position"]


def distance_km(origin_text, destination_text, origin_position=None, before_lookup=None):
    """Driving distance in km (one decimal), or None (see the module doc).
    origin_position: the stored [lon, lat] of origin_text. before_lookup: called
    before a lookup that isn't cached; returning False skips it (None)."""
    if not origin_text or not destination_text:
        return None
    key = hashlib.sha256(f"{origin_text}|{destination_text}".lower().encode("utf-8")).hexdigest()
    now = time.monotonic()
    hit = _distances.get(key)
    if hit and now - hit[1] < _CACHE_SECONDS:
        return hit[0]
    if before_lookup and before_lookup() is False:
        return None
    km = _lookup(origin_text, destination_text, origin_position)
    if len(_distances) >= _CACHE_MAX:
        _distances.clear()
    _distances[key] = (km, now)
    return km


def _lookup(origin_text, destination_text, origin_position):
    try:
        origin = origin_position or position(origin_text)
        destination = position(destination_text)
        if not origin or not destination:
            return None
        routes = _client("geo-routes").calculate_routes(Origin=origin, Destination=destination,
                                                        TravelMode="Car").get("Routes") or []
        meters = (routes[0].get("Summary") or {}).get("Distance") if routes else None
    except (BotoCoreError, ClientError):
        logger.exception("amazon location unavailable")
        return None
    if meters is None:
        return None
    return float((Decimal(meters) / 1000).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def address_text(address):
    """{street, postalCode, city} -> one line for the geocoder."""
    return f"{address['street']}, {address['postalCode']} {address['city']}"
