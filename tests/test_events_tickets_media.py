"""Event images (feature K, ADR-K 1, CONTRACTS.md K2-K4, K15, K16): the menu
image upload's key for THIS tenant and location plus required alt text. The
Lambda never touches S3, so these tests also prove no S3 call is made."""

import boto3
import pytest

from shared import tenant as shared_tenant
from shared.features.events_tickets import media
from tenant_support import LOC_A, LOC_A2, LOC_B, REGION, TENANT_A, TENANT_B, features_on, tenant_row
import test_events_tickets as base
from test_events_tickets import ACCT_A, E1, L, OWNER_A, OWNER_B, STAFF_A, call, create, publish

env = base.env  # the shared moto fixture (two tenants, events on)

UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
KEY = f"menu-images/{TENANT_A}/locations/{LOC_A}/menu/{UUID}.webp"
ALT = "Långbord dukat med vinglas i levande ljus"


@pytest.fixture
def no_s3(monkeypatch):
    import botocore.client
    real = botocore.client.BaseClient._make_api_call

    def guarded(self, operation_name, api_params):
        if self.meta.service_model.service_name == "s3":
            raise AssertionError("events-tickets has no S3 access")
        return real(self, operation_name, api_params)
    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", guarded)


def put(e, version, claims=OWNER_A, loc=LOC_A, **over):
    body = {"title": "Vinmiddag", "description": "", "settings": {"perOrderMax": 6, "transferAllowed": True},
            "version": version, **over}
    return call(e, "PUT", L + "/{eventId}", params={"locationId": loc, "eventId": E1}, body=body, claims=claims)


def test_create_with_image_and_alt(env, no_s3):
    status, body = create(env, imageKey=KEY, imageAlt=f"  {ALT} ")
    assert status == 201 and body["imageKey"] == KEY and body["imageAlt"] == ALT
    _, staff = call(env, "GET", L + "/{eventId}", params={"locationId": LOC_A, "eventId": E1}, claims=STAFF_A)
    assert staff["imageKey"] == KEY and staff["imageAlt"] == ALT
    # the same create again (client ULID) answers the stored event
    assert create(env, imageKey=KEY, imageAlt=ALT) == (201, body)


def test_create_without_image_has_no_image_fields(env):
    status, body = create(env)
    assert status == 201 and "imageKey" not in body and "imageAlt" not in body
    assert create(env, event_id="01EVENTCCCCCCCCCCCCCCCCCCC", imageKey="", imageAlt="ignored")[0] == 201


@pytest.mark.parametrize("key", [
    f"menu-images/{TENANT_B}/locations/{LOC_B}/menu/{UUID}.webp",       # another tenant's upload
    f"menu-images/{TENANT_B}/locations/{LOC_A}/menu/{UUID}.webp",       # right location id, wrong tenant
    f"menu-images/{TENANT_A}/locations/{LOC_A2}/menu/{UUID}.webp",      # own tenant, other location
    f"menu-images/{TENANT_A}/locations/{LOC_A}/menu/../{UUID}.webp",    # traversal
    f"menu-images/{TENANT_A}/locations/{LOC_A}/menu/{UUID}.gif",        # not an allowed type
    f"menu-images/{TENANT_A}/locations/{LOC_A}/menu/bild.webp",         # not a pre-signed-url name
    f"/menu-images/{TENANT_A}/locations/{LOC_A}/menu/{UUID}.webp",
    f"https://cdn.example/menu-images/{TENANT_A}/locations/{LOC_A}/menu/{UUID}.webp",
    "x.jpg", 42, ["k"],
])
def test_foreign_or_malformed_key_is_unknown_image(env, no_s3, key):
    status, body = create(env, imageKey=key, imageAlt=ALT)
    assert (status, body["code"], body["field"]) == (400, "unknown_image", "imageKey")


@pytest.mark.parametrize("alt", [None, "", "   ", "x" * 201, 7])
def test_alt_text_is_required_with_an_image(env, alt):
    over = {"imageKey": KEY} if alt is None else {"imageKey": KEY, "imageAlt": alt}
    status, body = create(env, **over)
    assert status == 400 and body["field"] == "imageAlt"


def test_update_sets_replaces_and_removes_image(env, no_s3):
    create(env)
    status, body = put(env, 1, imageKey=KEY, imageAlt=ALT)
    assert status == 200 and body["imageKey"] == KEY and body["version"] == 2
    other = KEY.replace(".webp", ".jpg")
    status, body = put(env, 2, imageKey=other, imageAlt="Ny bild")
    assert status == 200 and (body["imageKey"], body["imageAlt"]) == (other, "Ny bild")
    status, body = put(env, 3)  # PUT without imageKey: the event has no image
    assert status == 200 and "imageKey" not in body and "imageAlt" not in body
    row = env.table.get_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": f"EVENT#{E1}"})["Item"]
    assert "imageKey" not in row and "imageAlt" not in row


def test_update_refuses_foreign_key_role_and_tenant(env):
    create(env)
    foreign = f"menu-images/{TENANT_B}/locations/{LOC_B}/menu/{UUID}.png"
    assert put(env, 1, imageKey=foreign, imageAlt=ALT)[1]["code"] == "unknown_image"
    assert put(env, 1, imageKey=KEY, imageAlt=ALT, claims=STAFF_A)[0] == 403
    assert put(env, 1, imageKey=KEY, imageAlt=ALT, claims=OWNER_B, loc=LOC_B)[0] == 404
    assert put(env, 1, imageKey=KEY, imageAlt=ALT, loc=LOC_A2)[0] == 404  # the event is LOC_A's
    stale = put(env, 5, imageKey=KEY, imageAlt=ALT)
    assert stale[0] == 409 and stale[1]["code"] == "changed_retry"
    assert put(env, 1, imageKey=KEY, imageAlt=ALT)[0] == 200


def test_public_list_and_detail_show_image(env):
    create(env, imageKey=KEY, imageAlt=ALT)
    assert call(env, "GET", "/public/locations/{locationId}/events", params={"locationId": LOC_A})[1] == \
        {"events": []}  # draft
    publish(env)
    _, listing = call(env, "GET", "/public/locations/{locationId}/events", params={"locationId": LOC_A})
    assert listing["events"][0]["imageKey"] == KEY and listing["events"][0]["imageAlt"] == ALT
    _, detail = call(env, "GET", "/public/locations/{locationId}/events/{eventId}",
                     params={"locationId": LOC_A, "eventId": E1})
    assert detail["imageKey"] == KEY and detail["imageAlt"] == ALT
    assert call(env, "GET", "/public/locations/{locationId}/events/{eventId}",
                params={"locationId": LOC_B, "eventId": E1})[0] == 404


def test_image_refused_with_the_feature_off(env):
    boto3.resource("dynamodb", region_name=REGION).Table("test-tenant").put_item(
        Item=tenant_row(TENANT_A, features=features_on(), stripe={"accountId": ACCT_A}))
    shared_tenant.reset_caches()
    assert create(env, imageKey=KEY, imageAlt=ALT)[1] == {"error": "feature_not_in_plan"}


def test_clean_image_unit():
    assert media.clean_image({}, TENANT_A, LOC_A) is None
    assert media.clean_image({"imageKey": KEY, "imageAlt": "a"}, TENANT_A, LOC_A) == {"imageKey": KEY, "imageAlt": "a"}
    assert media.image_view({}) == {}
    assert media.image_view({"imageKey": KEY}) == {"imageKey": KEY, "imageAlt": ""}
