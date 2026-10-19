"""Marketing: cookieless visit counts, attribution, subscribers, templates,
campaigns, sending, clicks, unsubscribe and SES suppression."""

import importlib.util
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

from shared import marketing as m
from shared import tenant as shared_tenant
from tenant_support import (ALL_FEATURES, LOC_A, REGION, TENANT_A, TENANT_B,
                            location_row, seed_tenancy, tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
MKT_TABLE = "test-marketing"
MENU_TABLE = "test-menu"
OUTBOX = "test-outbox"
RES_TABLE = "test-reservation"
ORDER_TABLE = "test-order"
_ser = TypeSerializer()


def load(name):
    spec = importlib.util.spec_from_file_location(f"mkt_{name.replace('-', '_')}", ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
        monkeypatch.setenv("ENVIRONMENT", "dev")
        features = {**ALL_FEATURES, "marketing": True}
        seed_tenancy(monkeypatch,
                     tenant_a=tenant_row(TENANT_A, features=features, name="Bistro Linnea",
                                         branding={"primaryColor": "#9a3b26"}, replyToEmail="hej@linnea.se"),
                     tenant_b=tenant_row(TENANT_B, features=features),
                     locations_a=[location_row(TENANT_A, LOC_A, timezone="Europe/Stockholm")])
        monkeypatch.setenv("USER_TABLE_NAME", "test-user")
        ddb = boto3.client("dynamodb", region_name=REGION)
        for name in (MKT_TABLE, MENU_TABLE):
            ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                        {"AttributeName": "SK", "KeyType": "RANGE"}],
                             AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                                   {"AttributeName": "SK", "AttributeType": "S"}])
        secret = boto3.client("secretsmanager", region_name=REGION).create_secret(
            Name="mkt-key", SecretString="k" * 48)["ARN"]
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=OUTBOX, CreateBucketConfiguration={"LocationConstraint": REGION})
        for k, v in {"MARKETING_TABLE_NAME": MKT_TABLE, "MENU_TABLE_NAME": MENU_TABLE,
                     "MARKETING_LINK_KEY_SECRET_ARN": secret, "MAIL_OUTBOX_BUCKET_NAME": OUTBOX,
                     "NO_REPLY_EMAIL_ADDRESS": "no-reply@mail.dev.booqy.se",
                     "MARKETING_LINK_BASE": "https://restaurant.dev.booqy.se/api",
                     "MENU_IMAGE_BASE_URL": "https://cdn.example", "CUSTOMER_SITE_URL": "https://site.example",
                     "RESERVATION_TABLE_NAME": RES_TABLE, "ORDER_TABLE_NAME": ORDER_TABLE,
                     "MARKETING_SEND_RATE": "1000"}.items():
            monkeypatch.setenv(k, v)
        boto3.resource("dynamodb", region_name=REGION).Table("test-tenant").update_item(
            Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"}, UpdateExpression="SET slug = :s",
            ExpressionAttributeValues={":s": "bistro-linnea"})
        shared_tenant.reset_caches()
        m.reset_key_cache()
        yield SimpleNamespace(table=boto3.resource("dynamodb", region_name=REGION).Table(MKT_TABLE),
                              menu=boto3.resource("dynamodb", region_name=REGION).Table(MENU_TABLE),
                              s3=boto3.client("s3", region_name=REGION))


def body_of(response):
    return json.loads(response["body"]) if response.get("body") else None


# --- pure helpers ----------------------------------------------------------------------------

@pytest.mark.parametrize(("source", "medium", "ref", "expected"), [
    ("instagram", None, None, "instagram"), ("IG", None, None, "instagram"), (None, "qr", None, "qr"),
    (None, "email", None, "email"), (None, None, "https://www.google.se/search?q=x", "google"),
    (None, None, "https://l.facebook.com/l.php", "facebook"), (None, None, "https://site.example/meny", "direct"),
    (None, None, "https://blogg.example/post", "other"), (None, None, None, "direct"),
    ("partner-x", None, None, "other"), ("xmas-affisch", None, None, "other"),
    ("directmail", None, None, "other"), ("google-ads", None, None, "google"),
])
def test_channel_of(source, medium, ref, expected):
    assert m.channel_of(source, medium, m.referrer_host(ref), own_host="site.example") == expected


def test_attribution_is_cleaned_never_an_error():
    assert m.validate_attribution(None) is None
    assert m.validate_attribution({"source": "<script>", "campaign": "x" * 200}) is None
    assert m.validate_attribution({"source": "instagram", "campaign": "Höst 2026", "campaignId": "nope"}) == {
        "source": "instagram", "campaign": "Höst 2026", "channel": "instagram"}


def test_signed_links_reject_tampering(env):
    token = m.sign({"k": "u", "t": TENANT_A, "s": "a" * 24})
    assert m.verify(token)["t"] == TENANT_A
    body, mac = token.split(".")
    forged = m.sign({"k": "u", "t": TENANT_B, "s": "a" * 24}).split(".")[0]
    assert m.verify(f"{forged}.{mac}") is None
    assert m.verify(token + "x") is None
    assert m.verify("garbage") is None


def test_template_validation():
    with pytest.raises(ValueError, match="blocks"):
        m.validate_template({"name": "x", "subject": "y", "blocks": []})
    with pytest.raises(ValueError, match="https"):
        m.validate_template({"name": "x", "subject": "y", "blocks": [{"type": "image", "url": "http://a.se/x.png"}]})
    with pytest.raises(ValueError, match="destination"):
        m.validate_template({"name": "x", "subject": "y", "blocks": [{"type": "button", "label": "a",
                                                                      "destination": "evil"}]})
    for starter in m.starter_templates():
        m.validate_template(starter)


def test_render_escapes_and_has_unsubscribe():
    template = m.validate_template({"name": "n", "subject": "Hej {förnamn}", "blocks": [
        {"type": "text", "text": "<b>fett</b> för {förnamn}"},
        {"type": "button", "label": "Boka", "destination": "book"}]})
    subject, text, html_body = m.render_email(
        template, tenant_row={}, restaurant="Bistro", values={"förnamn": "Anna"}, menu_items={},
        site_url="https://site.example", link_for=lambda i, u: u, unsubscribe_url="https://u.example/x")
    assert subject == "Hej Anna"
    assert "&lt;b&gt;fett&lt;/b&gt;" in html_body and "<b>fett" not in html_body
    assert "https://u.example/x" in html_body and "https://u.example/x" in text
    assert "https://site.example/boka-bord" in html_body


def test_audience_rules():
    today = date(2026, 10, 19)
    sub = {"status": "subscribed", "lastVisit": "2026-09-01", "locations": {LOC_A}}
    assert m.in_audience(sub, {"type": "all"}, today)
    assert m.in_audience(sub, {"type": "recent", "days": 60}, today)
    assert not m.in_audience(sub, {"type": "lapsed", "days": 60}, today)
    assert m.in_audience(sub, {"type": "lapsed", "days": 30}, today)
    assert not m.in_audience(sub, {"type": "all", "locationId": "other"}, today)
    assert not m.in_audience({**sub, "status": "unsubscribed"}, {"type": "all"}, today)


# --- visits ------------------------------------------------------------------------------------

def track(public, body, ua="Mozilla/5.0 (iPhone)"):
    return public.handler({"routeKey": "POST /t", "headers": {"user-agent": ua, "origin": "https://site.example"},
                           "body": json.dumps(body), "requestContext": {"http": {"sourceIp": "1.2.3.4"}}}, None)


def stats_rows(env, tenant_id=TENANT_A):
    return env.table.query(KeyConditionExpression="PK = :p AND begins_with(SK, :d)",
                           ExpressionAttributeValues={":p": m.stats_pk(tenant_id), ":d": "DAY#"})["Items"]


def test_visits_are_counted_without_storing_people(env):
    public = load("marketing-public")
    assert track(public, {"locationId": LOC_A, "type": "visit", "path": "/meny",
                          "source": "instagram", "campaign": "Höst"})["statusCode"] == 204
    assert track(public, {"locationId": LOC_A, "type": "page", "path": "/boka-bord",
                          "source": "instagram", "campaign": "Höst"})["statusCode"] == 204
    track(public, {"locationId": LOC_A, "type": "visit", "path": "/", "referrer": "https://www.google.com/"})
    rows = {(r["channel"], r["campaign"]): r for r in stats_rows(env)}
    assert rows[("instagram", "höst")]["visits"] == 1 and rows[("instagram", "höst")]["pageviews"] == 2
    assert rows[("google", "-")]["visits"] == 1
    stored = json.dumps([{k: str(v) for k, v in r.items()} for r in stats_rows(env)])
    assert "1.2.3.4" not in stored and "iPhone" not in stored


def test_bots_and_plans_without_marketing_are_not_counted(env, monkeypatch):
    public = load("marketing-public")
    assert track(public, {"locationId": LOC_A, "type": "visit"}, ua="Googlebot/2.1")["statusCode"] == 204
    boto3.resource("dynamodb", region_name=REGION).Table("test-tenant").update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
        UpdateExpression="SET entitlements.features.marketing = :f", ExpressionAttributeValues={":f": False})
    shared_tenant.reset_caches()
    assert track(public, {"locationId": LOC_A, "type": "visit"})["statusCode"] == 204
    assert stats_rows(env) == []
    assert track(public, {"locationId": LOC_A, "type": "visit", "path": "x<y"})["statusCode"] in (204, 400)


# --- sync: bookings, orders, subscribers -------------------------------------------------------

def stream(table, new, old=None):
    ddb = {"NewImage": {k: _ser.serialize(v) for k, v in new.items()}, "SequenceNumber": "1"}
    if old:
        ddb["OldImage"] = {k: _ser.serialize(v) for k, v in old.items()}
    return {"Records": [{"eventSourceARN": f"arn:aws:dynamodb:{REGION}:1:table/{table}/stream/x", "dynamodb": ddb}]}


def booking(**extra):
    return {"tenantId": TENANT_A, "locationId": LOC_A, "reservationId": "r1", "status": "reserved",
            "source": "online", "date": "2026-10-20", "createdAt": "2026-10-19T10:00:00Z", "partySize": 4,
            "timezone": "Europe/Stockholm", "customerName": "Anna Andersson",
            "customerEmail": "anna@example.se", "marketingOptIn": True,
            "attribution": {"channel": "instagram", "campaign": "Höst"}, **extra}


def subscriber(env, email="anna@example.se", tenant_id=TENANT_A):
    return env.table.get_item(Key=m.sub_key(tenant_id, m.subscriber_id(tenant_id, email))).get("Item")


def test_online_booking_counts_and_subscribes(env):
    sync = load("marketing-sync")
    assert sync.handler(stream(RES_TABLE, booking()), None) == {"batchItemFailures": []}
    row = next(r for r in stats_rows(env) if r["channel"] == "instagram")
    assert row["bookings"] == 1 and row["guests"] == 4
    sub = subscriber(env)
    assert sub["status"] == "subscribed" and sub["consentSource"] == "booking" and sub["lastVisit"] == "2026-10-20"
    # A later change to the same booking is not counted again.
    sync.handler(stream(RES_TABLE, booking(notes="x"), old=booking()), None)
    assert next(r for r in stats_rows(env) if r["channel"] == "instagram")["bookings"] == 1


def test_no_opt_in_no_subscriber_and_staff_bookings_not_counted(env):
    sync = load("marketing-sync")
    sync.handler(stream(RES_TABLE, booking(marketingOptIn=False)), None)
    assert subscriber(env) is None
    sync.handler(stream(RES_TABLE, booking(source="phone", reservationId="r2")), None)
    assert sum(int(r.get("bookings") or 0) for r in stats_rows(env)) == 1


def test_new_opt_in_resubscribes_but_bounced_stays_suppressed(env):
    sync = load("marketing-sync")
    sync.handler(stream(RES_TABLE, booking()), None)
    key = m.sub_key(TENANT_A, m.subscriber_id(TENANT_A, "anna@example.se"))
    env.table.update_item(Key=key, UpdateExpression="SET #s = :u", ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":u": "unsubscribed"})
    sync.handler(stream(RES_TABLE, booking(reservationId="r3")), None)
    assert subscriber(env)["status"] == "subscribed"
    env.table.update_item(Key=key, UpdateExpression="SET #s = :b", ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":b": "bounced"})
    sync.handler(stream(RES_TABLE, booking(reservationId="r4")), None)
    assert subscriber(env)["status"] == "bounced"


def test_paid_order_adds_revenue(env):
    sync = load("marketing-sync")
    order = {"tenantId": TENANT_A, "locationId": LOC_A, "orderId": "o1", "paymentStatus": "paid",
             "totalAmount": 24900, "createdAt": "2026-10-19T11:00:00Z", "timezone": "Europe/Stockholm",
             "attribution": {"channel": "email", "campaign": "Höst", "campaignId": "a" * 16}}
    sync.handler(stream(ORDER_TABLE, order, old={**order, "paymentStatus": "processing"}), None)
    sync.handler(stream(ORDER_TABLE, {**order, "status": "ready"}, old=order), None)  # already paid
    row = next(r for r in stats_rows(env) if r["channel"] == "email")
    assert row["orders"] == 1 and row["revenueOre"] == 24900


# --- admin API, sending, links -----------------------------------------------------------------

def admin(fn, method, path, body=None, *, tenant_id=TENANT_A, role="owner_user", query=None):
    event = {"rawPath": path, "routeKey": "ANY /marketing/{proxy+}",
             "requestContext": {"http": {"method": method, "path": path}},
             "queryStringParameters": query, "body": json.dumps(body) if body is not None else None}
    return fn.handler(with_tenant_claims(event, tenant_id, role), None)


TEMPLATE = {"name": "Höst", "subject": "Hej {förnamn}, höst på {restaurang}", "preheader": "Nytt",
            "blocks": [{"type": "heading", "text": "Hösten är här"}, {"type": "text", "text": "Välkommen!"},
                       {"type": "dish", "menuItemId": "dish-1", "locationId": LOC_A},
                       {"type": "button", "label": "Boka bord", "destination": "book"}]}


def make_subscribers(env, n=3):
    sync = load("marketing-sync")
    for i in range(n):
        sync.handler(stream(RES_TABLE, booking(reservationId=f"r{i}", customerEmail=f"guest{i}@example.se",
                                               customerName=f"Gäst{i} Test")), None)


def draft(env, fn):
    env.menu.put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": "MENU#dish-1", "menuItemId": "dish-1",
                            "name": "Pumpasoppa", "description": "Rostad pumpa", "price": Decimal(145),
                            "imageKey": "menu-images/a.jpg", "active": True})
    tpl = body_of(admin(fn, "POST", "/marketing/templates", TEMPLATE))
    cmp = body_of(admin(fn, "POST", "/marketing/campaigns", {"name": "Höst 2026", "templateId": tpl["templateId"],
                                                             "audience": {"type": "all"}}))
    return tpl, cmp


def outbox(env):
    keys = [o["Key"] for o in env.s3.list_objects_v2(Bucket=OUTBOX).get("Contents") or []]
    return [json.loads(env.s3.get_object(Bucket=OUTBOX, Key=k)["Body"].read()) for k in keys]


def test_owner_only_and_plan_gated(env):
    fn = load("marketing")
    assert admin(fn, "GET", "/marketing/templates", role="staff_user")["statusCode"] == 403
    boto3.resource("dynamodb", region_name=REGION).Table("test-tenant").update_item(
        Key={"PK": f"TENANT#{TENANT_B}", "SK": "PROFILE"},
        UpdateExpression="SET entitlements.features.marketing = :f", ExpressionAttributeValues={":f": False})
    shared_tenant.reset_caches()
    assert admin(fn, "GET", "/marketing/templates", tenant_id=TENANT_B)["statusCode"] == 403


def test_tenants_cannot_see_each_others_campaigns(env):
    fn = load("marketing")
    _, cmp = draft(env, fn)
    assert admin(fn, "GET", f"/marketing/campaigns/{cmp['campaignId']}", tenant_id=TENANT_B)["statusCode"] == 404
    assert body_of(admin(fn, "GET", "/marketing/campaigns", tenant_id=TENANT_B))["campaigns"] == []


def test_preview_and_test_send_only_to_owner(env):
    fn = load("marketing")
    _, cmp = draft(env, fn)
    preview = body_of(admin(fn, "POST", "/marketing/preview", {"template": TEMPLATE}))
    assert "Pumpasoppa" in preview["html"] and "145 kr" in preview["html"]
    assert preview["subject"] == "Hej Anna, höst på Bistro Linnea"
    sent = body_of(admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/test"))
    assert sent == {"sentTo": "owner@a.example"}
    (mail,) = outbox(env)
    assert mail["to"] == "owner@a.example" and mail["subject"].startswith("[Test]")


def test_send_freezes_queues_and_delivers_once(env):
    fn, sender, public = load("marketing"), load("marketing-send"), load("marketing-public")
    make_subscribers(env, 3)
    env.table.update_item(Key=m.sub_key(TENANT_A, m.subscriber_id(TENANT_A, "guest2@example.se")),
                          UpdateExpression="SET #s = :u", ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":u": "unsubscribed"})
    tpl, cmp = draft(env, fn)
    audience = body_of(admin(fn, "GET", "/marketing/audience", query={"type": "all"}))
    assert audience["count"] == 2
    queued = body_of(admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/send", {}))
    assert queued["status"] == "scheduled" and queued["audienceSize"] == 2
    # Editing the template now changes nothing in the queued campaign.
    admin(fn, "PUT", f"/marketing/templates/{tpl['templateId']}", {**TEMPLATE, "subject": "Ändrad"})
    assert admin(fn, "PUT", f"/marketing/campaigns/{cmp['campaignId']}",
                 {"name": "x", "templateId": tpl["templateId"]})["statusCode"] == 409

    ctx = SimpleNamespace(get_remaining_time_in_millis=lambda: 120000)
    assert sender.handler({"source": "schedule"}, ctx) == {"campaigns": 1}
    mails = outbox(env)
    assert sorted(x["to"] for x in mails) == ["guest0@example.se", "guest1@example.se"]
    mail = next(x for x in mails if x["to"] == "guest0@example.se")
    assert mail["subject"] == "Hej Gäst0, höst på Bistro Linnea"
    assert mail["listUnsubscribe"].startswith("https://restaurant.dev.booqy.se/api/m/u/")
    assert "restaurant.dev.booqy.se/api/m/c/" in mail["html"] and "site.example" not in mail["html"]
    campaign = body_of(admin(fn, "GET", f"/marketing/campaigns/{cmp['campaignId']}"))
    assert campaign["status"] == "sent" and campaign["stats"]["sent"] == 2
    assert sender.handler({"source": "schedule"}, ctx) == {"campaigns": 0}
    assert len(outbox(env)) == 2

    # Click: recorded once, redirected to the frozen, tagged booking link.
    token = mail["html"].split("/api/m/c/")[1].split('"')[0]
    for _ in range(2):
        out = public.handler({"routeKey": "GET /m/c/{token}", "pathParameters": {"token": token}}, None)
        assert out["statusCode"] == 302
    target = urlsplit(out["headers"]["Location"])
    assert target.netloc == "site.example" and target.path == "/boka-bord"
    q = parse_qs(target.query)
    assert q["utm_source"] == ["email"] and q["bq_c"] == [cmp["campaignId"]] and q["restaurang"] == ["bistro-linnea"]
    assert body_of(admin(fn, "GET", f"/marketing/campaigns/{cmp['campaignId']}"))["stats"]["clicked"] == 1

    # One-click unsubscribe from the header.
    unsub = mail["listUnsubscribe"].split("/api/m/u/")[1]
    page = public.handler({"routeKey": "GET /m/u/{token}", "pathParameters": {"token": unsub}}, None)
    assert page["statusCode"] == 200 and "<form" in page["body"]
    assert subscriber(env, "guest0@example.se")["status"] == "subscribed"  # GET alone changes nothing
    public.handler({"routeKey": "POST /m/u/{token}", "pathParameters": {"token": unsub},
                    "body": "List-Unsubscribe=One-Click"}, None)
    assert subscriber(env, "guest0@example.se")["status"] == "unsubscribed"


def test_monthly_limit_pauses_campaign(env, monkeypatch):
    fn, sender = load("marketing"), load("marketing-send")
    monkeypatch.setenv("MARKETING_MONTHLY_LIMIT", "1")
    make_subscribers(env, 3)
    _, cmp = draft(env, fn)
    admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/send", {})
    sender.handler({"source": "schedule"}, SimpleNamespace(get_remaining_time_in_millis=lambda: 120000))
    assert len(outbox(env)) == 1
    assert body_of(admin(fn, "GET", f"/marketing/campaigns/{cmp['campaignId']}"))["status"] == "paused_limit"
    # Still at the limit: resuming is refused.
    assert admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/resume")["statusCode"] == 400
    # A higher limit (new month): continues, and never mails anyone twice.
    monkeypatch.setenv("MARKETING_MONTHLY_LIMIT", "10")
    assert body_of(admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/resume"))["status"] == "scheduled"
    sender.handler({"source": "schedule"}, SimpleNamespace(get_remaining_time_in_millis=lambda: 120000))
    assert sorted(o["to"] for o in outbox(env)) == ["guest0@example.se", "guest1@example.se", "guest2@example.se"]
    assert body_of(admin(fn, "GET", f"/marketing/campaigns/{cmp['campaignId']}"))["status"] == "sent"
    assert admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/resume")["statusCode"] == 409


def test_scheduled_campaign_waits_and_can_be_cancelled(env):
    fn, sender = load("marketing"), load("marketing-send")
    make_subscribers(env, 1)
    _, cmp = draft(env, fn)
    later = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/send", {"scheduledAt": later})
    ctx = SimpleNamespace(get_remaining_time_in_millis=lambda: 120000)
    assert sender.handler({"source": "schedule"}, ctx) == {"campaigns": 0}
    assert body_of(admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/cancel"))["status"] == "cancelled"


def test_ses_bounce_suppresses_address(env):
    fn, sender, sync = load("marketing"), load("marketing-send"), load("marketing-sync")
    make_subscribers(env, 1)
    _, cmp = draft(env, fn)
    admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/send", {})
    sender.handler({"source": "schedule"}, SimpleNamespace(get_remaining_time_in_millis=lambda: 120000))
    msg = env.table.scan(FilterExpression="SK = :s", ExpressionAttributeValues={":s": "MSG"})["Items"][0]
    message_id = msg["PK"].split("#", 1)[1]
    sync.handler({"source": "aws.ses", "detail-type": "Bounce",
                  "detail": {"eventType": "Bounce", "mail": {"messageId": message_id},
                             "bounce": {"bounceType": "Permanent"}}}, None)
    assert subscriber(env, "guest0@example.se")["status"] == "bounced"
    assert body_of(admin(fn, "GET", f"/marketing/campaigns/{cmp['campaignId']}"))["stats"]["bounced"] == 1


def test_subscriber_admin_and_export(env):
    fn = load("marketing")
    make_subscribers(env, 2)
    page = body_of(admin(fn, "GET", "/marketing/subscribers"))
    assert {s["email"] for s in page["subscribers"]} == {"guest0@example.se", "guest1@example.se"}
    sid = page["subscribers"][0]["subscriberId"]
    assert admin(fn, "POST", f"/marketing/subscribers/{sid}/unsubscribe")["statusCode"] == 204
    csv_out = admin(fn, "GET", "/marketing/subscribers/export")
    assert csv_out["headers"]["Content-Type"].startswith("text/csv") and "unsubscribed" in csv_out["body"]
    assert admin(fn, "DELETE", f"/marketing/subscribers/{sid}")["statusCode"] == 204
    assert len(body_of(admin(fn, "GET", "/marketing/subscribers"))["subscribers"]) == 1


def test_overview_totals(env):
    fn, public, sync = load("marketing"), load("marketing-public"), load("marketing-sync")
    today = m.local_day()
    track(public, {"locationId": LOC_A, "type": "visit", "source": "instagram"})
    sync.handler(stream(RES_TABLE, booking(createdAt=datetime.now(timezone.utc).isoformat())), None)
    out = body_of(admin(fn, "GET", "/marketing/overview", query={"from": today, "to": today}))
    assert out["totals"]["visits"] == 1 and out["totals"]["bookings"] == 1
    assert out["channels"][0]["channel"] == "instagram" and out["subscribers"] == 1
    assert len(out["series"]) == 1


def test_reservation_and_order_accept_attribution():
    """The public create functions accept `attribution` (and only clean it)."""
    for name in ("create-pending-reservation", "payment-intent", "catering-requests"):
        text = (ROOT / name / "app.py").read_text()
        assert '"attribution"' in text and "marketing.validate_attribution" in text, name



def test_campaign_tags_only_on_the_restaurants_own_host():
    site = "https://pizza.se"
    tagged = m.with_campaign_tags("https://pizza.se/boka-bord", "host", "a" * 16, site)
    assert "utm_campaign=host" in tagged and "bq_c=" in tagged
    assert m.with_campaign_tags("https://pizza.se.evil.com/x", "host", "a" * 16, site) == "https://pizza.se.evil.com/x"
    assert m.with_campaign_tags("http://pizza.se/x", "host", "a" * 16, site) == "http://pizza.se/x"


def test_distinct_campaign_tags_per_day_are_capped(env, monkeypatch):
    public = load("marketing-public")
    monkeypatch.setattr(public, "MAX_CAMPAIGNS_PER_DAY", 2)
    for tag in ("a", "b", "c", "a"):
        assert track(public, {"locationId": LOC_A, "type": "visit", "campaign": tag})["statusCode"] == 204
    rows = stats_rows(env)
    assert sorted(r.get("campaign") or "-" for r in rows) == ["-", "a", "b"]
    assert sum(int(r["visits"]) for r in rows) == 4


def test_cancel_during_sending_stops_and_stays_cancelled(env, monkeypatch):
    fn, sender = load("marketing"), load("marketing-send")
    make_subscribers(env, 3)
    _, cmp = draft(env, fn)
    admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/send", {})
    real = sender._still_sending
    calls = {"n": 0}

    def cancel_after_first_check(table, campaign):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(table, campaign)
        admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/cancel")
        return real(table, campaign)

    monkeypatch.setattr(sender, "PAGE", 1)
    monkeypatch.setattr(sender, "_still_sending", cancel_after_first_check)
    sender.handler({"source": "schedule"}, SimpleNamespace(get_remaining_time_in_millis=lambda: 120000))
    assert len(outbox(env)) == 1
    assert body_of(admin(fn, "GET", f"/marketing/campaigns/{cmp['campaignId']}"))["status"] == "cancelled"
    # The schedule entry is gone: nothing more is ever sent.
    sender.handler({"source": "schedule"}, SimpleNamespace(get_remaining_time_in_millis=lambda: 120000))
    assert len(outbox(env)) == 1


def test_nothing_sent_releases_recipient_and_usage(env, monkeypatch):
    fn, sender = load("marketing"), load("marketing-send")
    make_subscribers(env, 2)
    _, cmp = draft(env, fn)
    admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/send", {})

    real_sign, state = m.sign, {"broken": True}

    def maybe_broken(payload):
        if state["broken"]:
            raise m.LinkKeyUnavailable("secret unavailable")
        return real_sign(payload)

    monkeypatch.setattr(sender.m, "sign", maybe_broken)
    sender.handler({"source": "schedule"}, SimpleNamespace(get_remaining_time_in_millis=lambda: 120000))
    usage = env.table.get_item(Key={"PK": m.usage_pk(TENANT_A), "SK": f"MONTH#{m.month_key()}"}).get("Item") or {}
    assert int(usage.get("sent") or 0) == 0
    recipients = env.table.query(KeyConditionExpression="PK = :p",
                                 ExpressionAttributeValues={":p": m.recipients_pk(TENANT_A, cmp["campaignId"])})
    assert recipients["Items"] == []
    state["broken"] = False
    sender.handler({"source": "schedule"}, SimpleNamespace(get_remaining_time_in_millis=lambda: 120000))
    assert len(outbox(env)) == 2


def test_csv_export_neutralises_formulas(env):
    fn, sync = load("marketing"), load("marketing-sync")
    sync.handler(stream(RES_TABLE, booking(customerEmail="evil@example.se", customerName="=HYPERLINK(1)")), None)
    out = admin(fn, "GET", "/marketing/subscribers/export")["body"]
    assert "'=HYPERLINK" in out


# --- welcome offer ---------------------------------------------------------------------------

from shared import marketing_offer as offer  # noqa: E402

OFFER = {"enabled": True, "percent": 15, "minOrderOre": 20000, "validDays": 30,
         "headline": "15 % på nästa beställning", "text": "Lämna din e-post.", "buttonLabel": "Skicka koden",
         "delaySeconds": 10, "scrollPercent": 30, "pages": "menu", "frequency": "visit", "showOnMobile": True,
         "imageMenuItemId": "dish-1", "imageLocationId": LOC_A}


def public_get(public, path, query=None):
    return public.handler({"routeKey": f"GET {path}", "queryStringParameters": query,
                           "requestContext": {"http": {"method": "GET", "sourceIp": "1.2.3.4"}}}, None)


def public_post(public, path, body):
    return public.handler({"routeKey": f"POST {path}", "body": json.dumps(body), "headers": {},
                           "requestContext": {"http": {"method": "POST", "sourceIp": "1.2.3.4"}}}, None)


def save_offer(env, fn, **extra):
    env.menu.put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": "MENU#dish-1", "menuItemId": "dish-1",
                            "name": "Pumpasoppa", "price": Decimal(145), "imageKey": "menu-images/a.jpg",
                            "active": True})
    return admin(fn, "PUT", "/marketing/offer", {**OFFER, **extra})


def signup(public, email="ny@example.se", **extra):
    return public_post(public, "/m/signup", {"locationId": LOC_A, "email": email, "name": "Nils Ny",
                                             "consent": True, **extra})


def test_offer_settings_are_owner_only_and_resolve_the_dish(env):
    fn = load("marketing")
    assert body_of(admin(fn, "GET", "/marketing/offer"))["enabled"] is False  # defaults before saving
    saved = body_of(save_offer(env, fn))
    assert saved["imageUrl"] == "https://cdn.example/menu-images/a.jpg" and saved["dishName"] == "Pumpasoppa"
    assert saved["stats"] == {"views": 0, "signups": 0, "redeemed": 0, "revenueOre": 0, "discountOre": 0}
    assert admin(fn, "PUT", "/marketing/offer", OFFER, role="staff_user")["statusCode"] == 403
    assert save_offer(env, fn, percent=80)["statusCode"] == 400
    assert save_offer(env, fn, imageMenuItemId="nope")["statusCode"] == 400
    assert save_offer(env, fn, activeFrom="2026-12-01", activeTo="2026-11-01")["statusCode"] == 400


def test_public_offer_only_when_live(env):
    fn, public = load("marketing"), load("marketing-public")
    assert body_of(public_get(public, "/m/offer", {"locationId": LOC_A})) == {"offer": None}
    save_offer(env, fn)
    out = public_get(public, "/m/offer", {"locationId": LOC_A})
    assert out["headers"]["Cache-Control"] == "public, max-age=120"
    card = body_of(out)["offer"]
    assert card["percent"] == 15 and card["pages"] == "menu" and "imageMenuItemId" not in card
    assert save_offer(env, fn, headline="10 % rabatt")["statusCode"] == 400  # says 10 %, gives 15 %
    assert body_of(save_offer(env, fn, headline="{procent} % rabatt"))["headline"] == "{procent} % rabatt"
    assert body_of(public_get(public, "/m/offer", {"locationId": LOC_A}))["offer"]["headline"] == "15 % rabatt"
    save_offer(env, fn, activeTo="2020-01-01")
    assert body_of(public_get(public, "/m/offer", {"locationId": LOC_A})) == {"offer": None}
    assert body_of(public_get(public, "/m/offer", {"locationId": "unknown"})) == {"offer": None}


def code_in(mail):
    return re.search(r"[A-Z2-9]{4}-[A-Z2-9]{4}", mail["text"]).group(0)


def test_signup_is_pending_until_confirmed_and_mails_one_code_per_person(env):
    fn, public = load("marketing"), load("marketing-public")
    save_offer(env, fn)
    assert signup(public, consent=False)["statusCode"] == 400
    assert signup(public, email="nope")["statusCode"] == 400
    out = signup(public)
    assert out["statusCode"] == 200 and body_of(out) == {"sent": True}
    sub = subscriber(env, "ny@example.se")
    # Double opt-in: nothing is sent to a pending address by campaigns.
    assert sub["status"] == "pending" and sub["pendingConsent"]["version"] == "welcome_offer_v1"
    assert "Bistro Linnea" in sub["pendingConsent"]["text"]
    (mail,) = outbox(env)
    code = code_in(mail)
    assert mail["to"] == "ny@example.se" and "15 %" in mail["subject"] and mail["listUnsubscribe"]
    assert f"kod={code}" in mail["html"] and "/m/s/" in mail["html"]
    item = env.table.get_item(Key=offer.code_key(TENANT_A, code))["Item"]
    assert item["percent"] == 15 and item["minOrderOre"] == 20000 and item["status"] == "issued"
    assert item["ownerKey"] == offer.owner_key(TENANT_A, "ny@example.se")
    # The same person (Gmail-style alias) gets the same code again, never a second one.
    signup(public, email="NY+extra@example.se")
    assert len(outbox(env)) == 2 and code_in(outbox(env)[1]) == code
    codes = env.table.query(KeyConditionExpression="PK = :p AND begins_with(SK, :c)",
                            ExpressionAttributeValues={":p": offer.code_pk(TENANT_A), ":c": "CODE#"})["Items"]
    assert len(codes) == 1
    assert body_of(admin(fn, "GET", "/marketing/offer"))["stats"]["signups"] == 1
    check = public_post(public, "/m/code", {"locationId": LOC_A, "code": code.lower().replace("-", " ")})
    assert body_of(check) == {"code": code, "percent": 15, "minOrderOre": 20000, "expiresAt": item["expiresAt"]}
    assert public_post(public, "/m/code", {"locationId": LOC_A, "code": "AAAA-BBBB"})["statusCode"] == 404

    # Confirming: GET shows a button, POST confirms.
    token = re.search(r"/m/s/([^\"<\s]+)", mail["html"]).group(1)
    page = public.handler({"routeKey": "GET /m/s/{token}", "pathParameters": {"token": token},
                           "requestContext": {"http": {"method": "GET"}}}, None)
    assert page["statusCode"] == 200 and subscriber(env, "ny@example.se")["status"] == "pending"
    public.handler({"routeKey": "POST /m/s/{token}", "pathParameters": {"token": token},
                    "requestContext": {"http": {"method": "POST"}}}, None)
    sub = subscriber(env, "ny@example.se")
    assert sub["status"] == "subscribed" and sub["consentSource"] == "welcome_offer"
    assert sub["consentVersion"] == "welcome_offer_v1" and "pendingConsent" not in sub


def test_signup_never_resubscribes_or_reveals_anything(env):
    fn, public, sync = load("marketing"), load("marketing-public"), load("marketing-sync")
    save_offer(env, fn)
    sync.handler(stream(RES_TABLE, booking(customerEmail="gone@example.se")), None)
    env.table.update_item(Key=m.sub_key(TENANT_A, m.subscriber_id(TENANT_A, "gone@example.se")),
                          UpdateExpression="SET #s = :u", ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":u": "unsubscribed"})
    assert body_of(signup(public, email="gone@example.se")) == {"sent": True}
    assert subscriber(env, "gone@example.se")["status"] == "pending"  # not subscribed by a stranger
    env.table.update_item(Key=m.sub_key(TENANT_A, m.subscriber_id(TENANT_A, "bad@example.se")),
                          UpdateExpression="SET #s = :b", ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":b": "bounced"})
    before = len(outbox(env))
    assert body_of(signup(public, email="bad@example.se")) == {"sent": True}
    assert len(outbox(env)) == before  # bounced: no mail at all


def test_used_code_blocks_a_new_one(env):
    fn, public, sync = load("marketing"), load("marketing-public"), load("marketing-sync")
    save_offer(env, fn)
    signup(public)
    code = code_in(outbox(env)[-1])
    paid = {"tenantId": TENANT_A, "locationId": LOC_A, "orderId": "o1", "paymentStatus": "paid",
            "totalAmount": 25500, "createdAt": "2026-10-19T10:00:00Z", "timezone": "Europe/Stockholm",
            "date": "2026-10-19", "customerEmail": "ny@example.se", "customerName": "Nils Ny",
            "discount": {"code": code, "percent": 15, "amount": 4500, "subtotal": 30000}}
    sync.handler(stream(ORDER_TABLE, paid, {**paid, "paymentStatus": "processing"}), None)
    assert env.table.get_item(Key=offer.code_key(TENANT_A, code))["Item"]["status"] == "redeemed"
    stats = body_of(admin(fn, "GET", "/marketing/offer"))["stats"]
    assert stats["redeemed"] == 1 and stats["revenueOre"] == 25500 and stats["discountOre"] == 4500
    # Using the code confirms the sign-up.
    assert subscriber(env, "ny@example.se")["status"] == "subscribed"
    # A retried stream record counts nothing twice.
    sync.handler(stream(ORDER_TABLE, paid, {**paid, "paymentStatus": "processing"}), None)
    assert body_of(admin(fn, "GET", "/marketing/offer"))["stats"]["redeemed"] == 1
    orders = [r for r in stats_rows(env) if r.get("orders")]
    assert sum(int(r["orders"]) for r in orders) == 1
    # Signing up again: same answer, an email explaining it, no new code.
    assert body_of(signup(public)) == {"sent": True}
    assert "redan använt" in outbox(env)[0]["text"]  # newest first
    assert public_post(public, "/m/code", {"locationId": LOC_A, "code": code})["statusCode"] == 409


def test_signup_needs_a_live_offer_and_counts_views(env):
    fn, public = load("marketing"), load("marketing-public")
    assert signup(public)["statusCode"] == 404
    save_offer(env, fn)
    assert track(public, {"locationId": LOC_A, "type": "offer"})["statusCode"] == 204
    assert body_of(admin(fn, "GET", "/marketing/offer"))["stats"]["views"] == 1
    assert stats_rows(env) == []  # a card view is not a visit


def test_paid_order_with_opt_in_subscribes(env):
    sync = load("marketing-sync")
    paid = {"tenantId": TENANT_A, "locationId": LOC_A, "orderId": "o9", "paymentStatus": "paid",
            "totalAmount": 10000, "createdAt": "2026-10-19T10:00:00Z", "timezone": "Europe/Stockholm",
            "date": "2026-10-19", "customerEmail": "orda@example.se", "customerName": "Orda Ordersson",
            "marketingOptIn": True}
    sync.handler(stream(ORDER_TABLE, paid, {**paid, "paymentStatus": "processing"}), None)
    sub = subscriber(env, "orda@example.se")
    assert sub["status"] == "subscribed" and sub["consentSource"] == "order" and sub["lastVisit"] == "2026-10-19"
    no = {**paid, "orderId": "o10", "customerEmail": "nej@example.se", "marketingOptIn": False}
    sync.handler(stream(ORDER_TABLE, no, {**no, "paymentStatus": "processing"}), None)
    assert subscriber(env, "nej@example.se") is None


def test_owner_template_cannot_use_the_code_block():
    with pytest.raises(ValueError):
        m.validate_template({"name": "x", "subject": "y", "blocks": [{"type": "code", "code": "AAAA-BBBB"}]})


def test_discount_math_rounds_per_unit():
    lines = [{"unitPrice": 12500, "quantity": 2, "total": 25000, "vatRate": 12},
             {"unitPrice": 999, "quantity": 3, "total": 2997, "vatRate": 25}]
    out, total, saved = offer.apply_discount(lines, 15)
    assert [(line["listPrice"], line["unitPrice"], line["total"]) for line in out] == [(12500, 10625, 21250),
                                                                                    (999, 849, 2547)]
    assert total == 23797 and saved == 25000 + 2997 - 23797
    assert offer.normalize_code(" abcd-efgh ") == "ABCD-EFGH" and offer.normalize_code("ABCD-EFG0") is None
    assert offer.canonical_email("A.n.N.a+x@GoogleMail.com") == "anna@gmail.com"
    assert offer.canonical_email("anna.b+tag@example.se") == "anna.b@example.se"
