"""Road distance for catering delivery prices (issue 5, C6).

Amazon Location Service v2 (Core APIs, the Lambda's own region, IAM auth, no
API key): geocode both addresses, then the driving distance between them.
Core results are single use (IntendedUse SingleUse): nothing here is stored
or cached, only the distance computed from them ends up on the request.

`distance_km` answers None whenever the distance can't be trusted (an
address that only matches a town or a postal code, nothing found, the
service down); the caller then lets the restaurant set the delivery fee by
hand instead of guessing.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()

# Matches precise enough to price a delivery by (a town centre is not).
_PRECISE = {"PointAddress", "InterpolatedAddress", "SecondaryAddress", "Street", "PointOfInterest"}
_clients = {}


def _client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def position(text):
    """[lon, lat] of a Swedish address, or None."""
    result = _client("geo-places").geocode(QueryText=text[:200], MaxResults=1, Language="sv",
                                           Filter={"IncludeCountries": ["SWE"]}, IntendedUse="SingleUse")
    items = result.get("ResultItems") or []
    if not items or items[0].get("PlaceType") not in _PRECISE or not items[0].get("Position"):
        return None
    return items[0]["Position"]


def distance_km(origin_text, destination_text):
    """Driving distance in km (one decimal), or None (see the module doc)."""
    if not origin_text or not destination_text:
        return None
    try:
        origin, destination = position(origin_text), position(destination_text)
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
