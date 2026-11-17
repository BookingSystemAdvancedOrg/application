"""Audit #58: a failed booking from a hold gives back the guest's phone and
e-mail attempts, but the IP address keeps its attempt (as online booking)."""
from test_guarantee import ACCOUNT, g, markers, stored  # noqa: F401
from test_reservations import DAY, body_of, env, event, guard  # noqa: F401
from test_table_holds import hold


def book_held(env, held, ip="203.0.113.5", **extra):
    payload = {"date": DAY, "startTime": held["startTime"], "tableIds": held["tableIds"],
               "partySize": held["partySize"], "name": "Anna Svensson", "email": "anna@example.se",
               "phone": "070-123 45 67", "acceptTerms": True, "holdId": held["holdId"], **extra}
    e = event("POST", "/locations/{locationId}/reservations", body=payload,
              headers={"x-manage-token": held["manageToken"]})
    e["requestContext"]["http"]["sourceIp"] = ip
    return env["m"]["create"].handler(e, None)


def test_failed_hold_bookings_still_count_against_the_ip(guard):
    held = body_of(hold(guard))
    codes = [
        book_held(guard, held, startTime="19:00", email=f"g{i}@example.se",
                  phone=f"07012345{i}0")["statusCode"]
        for i in range(3)
    ]
    assert codes == [400, 400, 429]


def test_failed_hold_bookings_give_back_the_contact_attempts(guard, monkeypatch):
    monkeypatch.setattr(guard["m"]["create"], "IP_LIMIT", (10, 600))
    held = body_of(hold(guard))
    for _ in range(3):
        assert book_held(guard, held, startTime="19:00")["statusCode"] == 400
    assert book_held(guard, held)["statusCode"] == 201
