"""Catering pricing v2 (issue 5, M2/M3): the public quote, packages x guests,
tiers and lead time by guests, delivery bands from the road distance, VAT
per line, the request snapshot and the Stripe tax rate per VAT rate."""

import json
from decimal import Decimal

import pytest

from shared import catering as c
from shared import geo
from test_catering import (CATERING_MENU_TABLE, COMPANY, LOC_A, LOCATION_TABLE, OFFER, SETTINGS, TENANT_A,
                           body_of, create, env, location_item, staff)  # noqa: F401 - env is a fixture

PACKAGE_ID = "a" * 32
DISH_ID = "b" * 32
OFF_DISH_ID = "c" * 32


@pytest.fixture
def menu(env):  # noqa: F811
    table = env.res.Table(CATERING_MENU_TABLE)
    table.put_item(Item={**c.menu_key(LOC_A, c.DISH, DISH_ID), "name": "Kycklingspett", "pricePerPortion": Decimal("85"),
                         "allergens": [], "dietary": [], "active": True, "sort": 0, "version": 1})
    table.put_item(Item={**c.menu_key(LOC_A, c.DISH, OFF_DISH_ID), "name": "Gammal", "pricePerPortion": Decimal("10"),
                         "allergens": [], "dietary": [], "active": False, "sort": 0, "version": 1})
    table.put_item(Item={**c.menu_key(LOC_A, c.PACKAGE, PACKAGE_ID), "name": "Buffé Roma",
                         "pricePerGuest": Decimal("245.50"), "dishIds": [DISH_ID], "minGuests": 20, "active": True,
                         "sort": 0, "version": 1})
    return env


def quote(env, **body):  # noqa: F811
    data = {"guests": 40, "packages": [{"packageId": PACKAGE_ID}], "fulfilment": "delivery",
            "deliveryAddress": {"street": "Storgatan 1", "postalCode": "11122", "city": "Stockholm"}}
    data.update(body)
    event = {"routeKey": "POST /locations/{locationId}/catering/quote", "pathParameters": {"locationId": LOC_A},
             "body": json.dumps({k: v for k, v in data.items() if v is not None}),
             "requestContext": {"http": {"sourceIp": "203.0.113.9"}}}
    return env.m["requests"].handler(event, None)


def set_settings(env, **changes):  # noqa: F811
    env.res.Table(LOCATION_TABLE).put_item(Item=location_item(TENANT_A, LOC_A, cateringSettings={**SETTINGS, **changes}))
    from shared import tenant
    tenant.reset_caches()


def test_quote_packages_times_guests_tier_band_and_vat(menu):
    body = body_of(quote(menu, dishes=[{"dishId": DISH_ID, "quantity": 10}]))
    # 40 x 245.50 + 10 x 85 = 10 670 kr; 40 guests -> Nivå 1 (5 %) = 533.50 kr; 10 km -> first band 395 kr
    assert [(l["kind"], l["quantity"], l["unitPrice"], l["vatRate"]) for l in body["lines"]] == \
        [("package", 40, 24550, 12), ("dish", 10, 8500, 12)]
    assert body["subtotal"] == 1067000 and body["discount"] == 53350 and body["deliveryFee"] == 39500
    assert body["total"] == 1067000 - 53350 + 39500 and body["vatRate"] == 12
    assert body["vatBreakdown"] == [{"rate": 12, "gross": body["total"], "net": body["total"] - body["vat"],
                                     "vat": body["vat"]}]
    assert body["distanceKm"] == 10.0 and body["deliveryFeeManual"] is False
    assert body["ruleRefs"]["deliveryBand"] == {"upToKm": 25, "price": 395} and body["ruleRefs"]["tierId"] == "1" * 32
    assert body["earliestDate"] == "2026-10-06" and body["guests"] == 40


def test_quote_beyond_the_first_band_and_outside_the_area(menu):
    menu.distance["km"] = 30.2
    assert body_of(quote(menu))["deliveryFee"] == 79500
    set_settings(menu, deliveryBands=[{"upToKm": 10, "price": 300}, {"upToKm": 20, "price": 500}], maxDeliveryKm=20)
    response = quote(menu)
    assert response["statusCode"] == 409 and body_of(response) == {"error": "catering_outside_area"}


def test_quote_without_a_located_address_leaves_the_fee_to_the_restaurant(menu):
    menu.distance["km"] = None
    body = body_of(quote(menu))
    assert body["deliveryFee"] == 0 and body["deliveryFeeManual"] is True and body["distanceKm"] is None
    pickup = body_of(quote(menu, fulfilment="pickup", deliveryAddress=None))
    assert pickup["deliveryFee"] == 0 and pickup["deliveryFeeManual"] is False


def test_request_with_manual_fee_is_still_accepted(menu):
    menu.distance["km"] = None
    body = body_of(create(menu, items=None, packages=[{"packageId": PACKAGE_ID}], guests=40))
    item = c.load(LOC_A, body["requestId"])
    assert item["request"]["deliveryFeeManual"] is True and item["request"]["lines"][0]["vatRate"] == 12
    assert item["guests"] == 40 and item["serviceIncluded"] is True


def test_food_without_service_takes_the_reduced_rate(menu):
    set_settings(menu, defaultServiceIncluded=False)
    body = body_of(quote(menu, deliveryDate="2026-10-20"))
    assert body["vatRate"] == 6 and body["lines"][0]["vatRate"] == 6 and body["ruleRefs"]["vat"] == "food"
    assert body["vat"] == c.vat_of(body["total"], 6)
    assert body_of(quote(menu, deliveryDate="2028-01-10"))["vatRate"] == 12
    assert c.vat_rate_for(True, __import__("datetime").date(2026, 10, 20)) == 12


@pytest.mark.parametrize("change, error", [
    ({"guests": 10}, "at least 15 guests"),
    ({"guests": 18}, "Buffé Roma needs at least 20 guests"),
    ({"packages": None, "dishes": [{"dishId": OFF_DISH_ID, "quantity": 20}]}, "catering_item_unavailable"),
    ({"packages": [{"packageId": "x" * 32}]}, "catering_item_unavailable"),
    ({"packages": None}, "choose at least one package or dish"),
    ({"packages": [{"packageId": PACKAGE_ID, "price": 1}]}, "each of packages needs packageId and quantity"),
    ({"deliveryDate": "20 okt"}, "deliveryDate must be YYYY-MM-DD"),
])
def test_quote_rules(menu, change, error):
    response = quote(menu, **change)
    assert response["statusCode"] == 400 and error in body_of(response)["error"]


def test_lead_time_by_guests(menu):
    set_settings(menu, leadTimeRules=[{"fromGuests": 1, "toGuests": 49, "days": 3},
                                      {"fromGuests": 50, "toGuests": None, "days": 14}])
    assert body_of(quote(menu))["earliestDate"] == "2026-10-04"
    assert body_of(quote(menu, guests=60))["earliestDate"] == "2026-10-15"
    # 60 guests on the 10th: too soon; 40 guests on the 10th: fine
    late = create(menu, items=None, packages=[{"packageId": PACKAGE_ID}], guests=60, deliveryDate="2026-10-10")
    assert body_of(late) == {"error": "catering_too_soon"}
    ok = create(menu, items=None, packages=[{"packageId": PACKAGE_ID}], guests=40, deliveryDate="2026-10-10",
                phone="0701234568", email="b@example.se")
    assert ok["statusCode"] == 201


def test_lead_time_counts_calendar_days_across_dst():
    # 2026-10-25 03:00 is the autumn DST change in Stockholm; 22:30 UTC on the 24th is already the 25th locally.
    from datetime import datetime, timezone
    settings = c.validate_settings({"advanceDays": 1})
    now = datetime(2026, 10, 24, 22, 30, tzinfo=timezone.utc)
    assert c.earliest_date(settings, {"timezone": "Europe/Stockholm"}, 20, now) == "2026-10-26"
    with pytest.raises(c.Unavailable):
        c.check_date(settings, {"timezone": "Europe/Stockholm"}, "2026-10-25", now, 20)


def test_offer_takes_the_reduced_rate_without_service_and_stripe_gets_that_rate(menu):
    rid = body_of(create(menu))["requestId"]
    sent = body_of(staff(menu, "POST", "/offer/send", body={**OFFER, "serviceIncluded": False}, request_id=rid))
    assert sent["offer"]["totals"]["vatRate"] == 6 and sent["offer"]["lines"][0]["vatRate"] == 6
    bad = staff(menu, "PUT", "/offer", body={**OFFER, "serviceIncluded": "nej"}, request_id=rid)
    assert bad["statusCode"] == 400


def test_stripe_tax_rate_per_vat_rate(menu):
    from shared import catering_stripe
    first = catering_stripe.tax_rate("acct_roma", 6)
    created = [x for x in menu.stripe.calls if x["path"] == "/v1/tax_rates" and x["method"] == "POST"]
    assert first == "txr_12" and created[-1]["params"]["percentage"] == 6
    assert created[-1]["key"] == "tax-rate-se-6-incl"
    assert catering_stripe._offer_rate("acct_roma", {"totals": {}}) == "txr_12"


class _Geo:
    def __init__(self, places, meters):
        self.places, self.meters = places, meters

    def geocode(self, **kw):
        assert kw["Filter"] == {"IncludeCountries": ["SWE"]} and kw["IntendedUse"] == "SingleUse"
        return {"ResultItems": [self.places.pop(0)]}

    def calculate_routes(self, **kw):
        assert kw["TravelMode"] == "Car"
        return {"Routes": [{"Summary": {"Distance": self.meters}}]}


def test_geo_distance(monkeypatch):
    precise = {"PlaceType": "PointAddress", "Position": [18.07, 59.31]}
    fake = _Geo([precise, dict(precise)], 12345)
    monkeypatch.setattr(geo, "_client", lambda name: fake)
    assert geo.distance_km("Götgatan 1", "Storgatan 1, 11122 Stockholm") == 12.3
    vague = _Geo([precise, {"PlaceType": "Locality", "Position": [18, 59]}], 1)
    monkeypatch.setattr(geo, "_client", lambda name: vague)
    assert geo.distance_km("Götgatan 1", "Stockholm") is None
    assert geo.distance_km("", "x") is None
    assert geo.address_text({"street": "Storgatan 1", "postalCode": "11122", "city": "Stockholm"}) == \
        "Storgatan 1, 11122 Stockholm"


def test_company_request_needs_a_signatory(menu):
    missing = create(menu, **{**COMPANY, "signatoryName": None})
    assert body_of(missing) == {"error": "signatoryName is required"}


def test_settings_report_whether_the_delivery_origin_is_found(env):  # noqa: F811
    def put(body):
        event = {"routeKey": "PUT /locations/{locationId}/catering/settings", "pathParameters": {"locationId": LOC_A},
                 "body": json.dumps(body)}
        from tenant_support import with_tenant_claims
        return body_of(env.m["settings"].handler(with_tenant_claims(event), None))
    assert put({"originAddress": "Götgatan 1, Stockholm"})["originFound"] is True
    assert put({"originAddress": "okänd plats"})["originFound"] is False
    assert put({"delivery": False})["originFound"] is None
