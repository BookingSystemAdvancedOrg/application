"""tenant-account: Booqy's own subscription as the owner sees it (card on
file, open invoices, adding a card through Stripe Checkout on the PLATFORM
account) and accepting Allmänna villkor för restauranger."""

import json

import pytest

from test_tenant_account import app, body_of, make_event  # noqa: F401 (fixture)
from tenant_support import OWNER_A_SUB, TENANT_A


@pytest.fixture
def stripe_calls(app, monkeypatch):  # noqa: F811
    module, _ = app
    calls = []
    answers = {"invoices": []}

    def fake(method, path, params=None, *, account=None, idempotency_key=None):
        calls.append((method, path, params, account, idempotency_key))
        if path == "/v1/checkout/sessions":
            return {"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1"}
        if path == "/v1/invoices":
            return {"data": answers["invoices"]}
        raise AssertionError(path)

    monkeypatch.setattr(module.stripe_client, "request", fake)
    return calls, answers


def _billing(tenants, **fields):
    tenants.update_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"}, UpdateExpression="SET billing = :b",
                        ExpressionAttributeValues={":b": fields})


def test_get_tenant_tells_the_owner_whether_a_card_is_charged(app):  # noqa: F811
    module, tenants = app
    body = body_of(module.handler(make_event(), None))
    assert body["billing"] == {"customer": False, "hasCard": False, "cardBrand": None, "cardLast4": None,
                               "collection": "invoice", "preference": "card", "daysUntilDue": 10, "pastDue": False}
    _billing(tenants, customerId="cus_A", defaultPaymentMethod="pm_1", cardBrand="visa", cardLast4="4242")
    body = body_of(module.handler(make_event(), None))
    assert body["billing"]["collection"] == "card" and body["billing"]["cardLast4"] == "4242"
    assert body["billing"]["daysUntilDue"] is None and "defaultPaymentMethod" not in json.dumps(body)
    _billing(tenants, customerId="cus_A", collectionPreference="invoice")
    assert body_of(module.handler(make_event(), None))["billing"]["daysUntilDue"] == 30
    # staff see nothing about Booqy's invoices
    assert body_of(module.handler(make_event(role="staff_user"), None))["billing"] is None


def test_card_session_is_a_setup_checkout_on_the_platform_account(app, stripe_calls):  # noqa: F811
    module, tenants = app
    calls, _ = stripe_calls
    response = module.handler(make_event("POST", "/tenant/billing/card-session"), None)
    assert response["statusCode"] == 409 and body_of(response)["error"] == "no_billing_customer"
    _billing(tenants, customerId="cus_A")
    response = module.handler(make_event("POST", "/tenant/billing/card-session"), None)
    assert response["statusCode"] == 200 and body_of(response)["url"] == "https://checkout.stripe.com/c/cs_1"
    (method, path, params, account, key) = calls[0]
    assert method == "POST" and account is None                              # never the connected account
    assert params["mode"] == "setup" and params["customer"] == "cus_A" and params["locale"] == "sv"
    assert params["success_url"] == "https://admin.example.se/installningar?kort=klart"
    assert key.startswith(f"card-session-{TENANT_A}-")
    staff = module.handler(make_event("POST", "/tenant/billing/card-session", role="staff_user"), None)
    assert staff["statusCode"] == 403


def test_open_invoices_with_their_payment_links(app, stripe_calls):  # noqa: F811
    module, tenants = app
    calls, answers = stripe_calls
    response = module.handler(make_event("GET", "/tenant/billing"), None)
    assert response["statusCode"] == 200 and body_of(response)["invoices"] == [] and calls == []
    _billing(tenants, customerId="cus_A")
    answers["invoices"] = [
        {"id": "in_1", "number": "F-0001", "amount_remaining": 123750, "due_date": 1_700_000_000,
         "collection_method": "send_invoice", "hosted_invoice_url": "https://invoice.stripe.com/i/1",
         "metadata": {"tenantId": TENANT_A}, "description": "Booqy, 1 dec till 31 dec 2026"},
        {"id": "in_2", "number": "F-0002", "amount_remaining": 6000, "due_date": 4_000_000_000,
         "collection_method": "send_invoice", "metadata": {"tenantId": "someone-else"}},
    ]
    body = body_of(module.handler(make_event("GET", "/tenant/billing"), None))
    assert body["invoices"] == [{"number": "F-0001", "amountDueSek": 1237.5, "dueDate": "2023-11-14", "overdue": True,
                                 "collection": "invoice", "url": "https://invoice.stripe.com/i/1",
                                 "description": "Booqy, 1 dec till 31 dec 2026"}]
    assert calls[0][2] == {"customer": "cus_A", "status": "open", "limit": 20}


def test_stripe_down_is_503(app, monkeypatch):  # noqa: F811
    module, tenants = app
    _billing(tenants, customerId="cus_A")

    def down(*_a, **_k):
        raise module.stripe_client.StripeUnavailable("x")
    monkeypatch.setattr(module.stripe_client, "request", down)
    assert module.handler(make_event("GET", "/tenant/billing"), None)["statusCode"] == 503


def test_owner_accepts_the_restaurant_terms(app):  # noqa: F811
    module, tenants = app
    assert body_of(module.handler(make_event(), None))["terms"]["upToDate"] is False
    old = module.handler(make_event("POST", "/tenant/terms/accept", body={"version": "1999"}), None)
    assert old["statusCode"] == 409 and body_of(old)["currentVersion"] == module.terms.CURRENT_VERSION
    response = module.handler(make_event("POST", "/tenant/terms/accept",
                                         body={"version": module.terms.CURRENT_VERSION}), None)
    assert response["statusCode"] == 200 and body_of(response)["terms"]["upToDate"] is True
    rows = tenants.query(KeyConditionExpression="PK = :p", ExpressionAttributeValues={":p": f"TERMS#{TENANT_A}"})["Items"]
    assert len(rows) == 1 and rows[0]["version"] == module.terms.CURRENT_VERSION
    assert rows[0]["acceptedBy"]["sub"] == OWNER_A_SUB
    staff = module.handler(make_event("POST", "/tenant/terms/accept", role="staff_user",
                                      body={"version": module.terms.CURRENT_VERSION}), None)
    assert staff["statusCode"] == 403
