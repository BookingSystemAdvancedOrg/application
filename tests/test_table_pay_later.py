"""Pay-later QR table orders: placing (straight to the kitchen, unpaid, on
the table's open bill, caps and limits), the guest bill routes (view, pay
with the phone, lock, takeover, resume, settle by webhook and by polling,
mismatch, refund backstop), the staff routes (tabs, pay link, paid outside
Booqy, write-off, cancel unpaid) and pickup staying exactly as before."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from boto3.dynamodb.conditions import Key

from error_body import err
from shared import order_payments, stripe_client, table_bills
from shared import orders as o
from test_orders import (ACCOUNT, LOC_A, NOW, ORDER_TABLE, POLICY, TENANT_A, TODAY, WEEK, body_of,  # noqa: F401
                         env, menu_item, MENU_TABLE, notify, order_record, paid_session, post_order, set_location, set_status,
                         staff_event, stored, stripe_event, webhook)

CODE = ("t-7", "Bord 7")
CODE_3 = ("t-3", "Fönsterbordet")
PIZZA_COLA = 2 * 12500 + 3550


def code_of(table):
    return o.table_code(TENANT_A, LOC_A, *table)


def place(env, code=CODE, **extra):
    return post_order(env, mode="table", tableCode=code_of(code), payLater=True, **extra)


def placed(env, **extra):
    response = place(env, **extra)
    assert response["statusCode"] == 201, response
    return body_of(response)


def tab(env, table_id="t-7", service_date=TODAY):
    return table_bills.load_tab(LOC_A, service_date, table_id)


def guest_get(env, token, order=None, path_bill=None):
    route = "/locations/{locationId}/table-bill" + ("/{billId}" if path_bill else "")
    event = {"routeKey": f"GET {route}", "pathParameters": {"locationId": LOC_A, **(
        {"billId": path_bill} if path_bill else {})}, "headers": {"X-Order-Token": token}}
    if order:
        event["queryStringParameters"] = {"order": order}
    return env["m"]["get"].handler(event, None)


def guest_pay(env, token, order=None, **body):
    event = {"routeKey": "POST /locations/{locationId}/table-bill/pay", "pathParameters": {"locationId": LOC_A},
             "headers": {"X-Order-Token": token}, "body": json.dumps(body),
             "requestContext": {"http": {"method": "POST", "sourceIp": "203.0.113.9"}}}
    if order:
        event["queryStringParameters"] = {"order": order}
    return env["m"]["create"].handler(event, None)


def staff(env, method, suffix, **kw):
    return env["m"]["staff"].handler(staff_event(method, suffix, **kw), None)


def at(env, moment):
    env["monkeypatch"].setattr(o, "now_utc", lambda: moment)


def session_calls(env):
    return [c for c in env["stripe"].calls if c["path"] == "/v1/checkout/sessions"]


def bill_session(env, **extra):
    return dict(env["stripe"].session, payment_status="paid", status="complete", payment_intent="pi_bill",
                **extra)


def start_paying(env, created):
    body = body_of(guest_pay(env, created["statusToken"], order=created["orderId"],
                             expectedAmount=int(tab(env)["openTotal"])))
    assert "billId" in body, body
    return body


# --- placing ------------------------------------------------------------------------------------

def test_pay_later_order_goes_to_the_kitchen_unpaid(env):
    created = placed(env, email=None)
    assert created["payLater"] is True and created["number"] == 1 and created["serviceDate"] == TODAY
    assert "clientSecret" not in created and "checkoutUrl" not in created
    assert env["stripe"].calls == []
    item = stored(env, created["orderId"])
    assert item["status"] == "confirmed" and item["paymentStatus"] == "unpaid" and item["payLater"] is True
    assert item["number"] == 1 and item["confirmedAt"] == "2026-09-08T12:00:00Z"
    assert item["readyEstimate"] == "2026-09-08T12:20:00Z" and item["stripeAccountId"] == ACCOUNT
    for absent in ("ttl", "notice", "checkoutSessionId", "customerEmail", "paymentMethod"):
        assert absent not in item
    assert item["history"][0]["payLater"] is True
    bill = tab(env)
    assert bill["openCount"] == 1 and bill["openTotal"] == PIZZA_COLA and "ttl" not in bill
    assert table_bills.entries(bill)[created["orderId"]]["number"] == 1
    # The kitchen list shows it (it is confirmed).
    listed = body_of(env["m"]["staff"].handler(staff_event("GET", query={"date": TODAY}), None))
    assert [x["orderId"] for x in listed["orders"]] == [created["orderId"]]
    assert listed["orders"][0]["paymentStatus"] == "unpaid" and listed["orders"][0]["payLater"] is True


def test_pay_later_needs_name_and_phone_but_not_email(env):
    assert body_of(place(env, phone=None, email=None)) == err("phone is required")
    assert body_of(place(env, phone=None)) == err("phone is required")
    assert place(env, email="")["statusCode"] == 201


def test_codes_are_not_for_table_orders(env):
    assert body_of(place(env, discountCode="WELCOME10")) == err("codes_not_for_table")
    assert body_of(place(env, giftCardCode="GIFT-1234")) == err("codes_not_for_table")


def test_pay_later_is_only_for_table_orders(env):
    assert body_of(post_order(env, payLater=True)) == err("payLater is only for table orders")
    assert body_of(post_order(env, mode="table", tableCode=code_of(CODE), payLater="yes")) == \
        err("payLater must be true or false")


def test_table_order_from_an_old_site_still_pays_first(env):
    created = body_of(post_order(env, mode="table", tableCode=code_of(CODE)))
    assert created["checkoutUrl"].startswith("https://checkout.stripe.com/")
    item = stored(env, created["orderId"])
    assert item["status"] == "pending_payment" and "payLater" not in item and tab(env) is None


def test_bill_caps_at_8_orders(env):
    for _ in range(8):
        placed(env)
    response = place(env)
    assert response["statusCode"] == 409 and body_of(response) == err("table_bill_full")
    assert tab(env)["openCount"] == 8
    assert place(env, code=CODE_3)["statusCode"] == 201  # another table has its own bill


def test_bill_caps_at_5000_kr(env):
    big = [{"menuItemId": "pizza", "quantity": 20}]  # 2 500 kr
    placed(env, items=big)
    placed(env, items=big)
    assert tab(env)["openTotal"] == 500_000
    assert body_of(place(env, items=[{"menuItemId": "cola", "quantity": 1}])) == err("table_bill_full")


def test_one_phone_places_at_most_8_table_orders_a_day(env):
    env["res"].create_table(TableName="test-guard", KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
                            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
                            BillingMode="PAY_PER_REQUEST")
    env["monkeypatch"].setenv("REQUEST_GUARD_TABLE_NAME", "test-guard")
    codes = [CODE, CODE_3, ("t1", "1"), ("t5", "5")]
    for n in range(8):
        assert place(env, code=codes[n % 4])["statusCode"] == 201
    response = place(env, code=CODE_3)
    assert response["statusCode"] == 429 and body_of(response) == err("too_many_requests")


def test_service_day_runs_past_midnight_while_open():
    late = {"timezone": "Europe/Stockholm",
            "businessHours": {d: [{"opensAt": "18:00", "closesAt": "01:00"}] for d in WEEK}}
    after_midnight = datetime(2026, 9, 8, 22, 30, tzinfo=timezone.utc)  # 00:30 on the 9th in Stockholm
    assert o.service_date(late, after_midnight) == "2026-09-08"
    assert o.service_date(late, datetime(2026, 9, 9, 0, 30, tzinfo=timezone.utc)) == "2026-09-09"  # closed
    assert o.service_date(late, datetime(2026, 9, 8, 17, 0, tzinfo=timezone.utc)) == "2026-09-08"


def test_order_placed_after_midnight_joins_the_evenings_bill(env):
    set_location(env, businessHours={d: [{"opensAt": "18:00", "closesAt": "01:00"}] for d in WEEK})
    at(env, datetime(2026, 9, 8, 22, 30, tzinfo=timezone.utc))
    created = placed(env)
    item = stored(env, created["orderId"])
    assert item["date"] == "2026-09-09" and item["serviceDate"] == "2026-09-08"
    assert tab(env, service_date="2026-09-08")["openCount"] == 1


# --- the guest's bill --------------------------------------------------------------------------

def test_guest_sees_the_whole_bill_without_personal_data(env):
    first = placed(env)
    placed(env, name="Bertil Ek", phone="070-999 99 99", items=[{"menuItemId": "wine", "quantity": 1}])
    response = guest_get(env, first["statusToken"], order=first["orderId"])
    view = body_of(response)
    assert view["tableTitle"] == "Bord 7" and view["status"] == "open" and view["count"] == 2
    assert view["amount"] == PIZZA_COLA + 9500 and view["minChargeOre"] == 300
    assert [x["number"] for x in view["orders"]] == [1, 2]
    assert {"rate": 25, "gross": 9500, "net": 7600, "vat": 1900} in view["vat"]
    raw = response["body"]
    for private in ("Anna", "Bertil", "070", "anna@example.se", "customer"):
        assert private not in raw
    status = body_of(env["m"]["get"].handler({
        "routeKey": "GET /locations/{locationId}/order-status/{orderId}",
        "pathParameters": {"locationId": LOC_A, "orderId": first["orderId"]},
        "headers": {"X-Order-Token": first["statusToken"]}}, None))
    assert status["payLater"] is True and status["bill"] == {"amount": view["amount"], "count": 2, "status": "open"}


def test_wrong_tokens_answer_not_found(env):
    first = placed(env)
    assert guest_get(env, "nope", order=first["orderId"])["statusCode"] == 404
    assert guest_get(env, first["statusToken"])["statusCode"] == 404
    legacy = body_of(post_order(env, mode="table", tableCode=code_of(CODE)))
    assert guest_get(env, legacy["statusToken"], order=legacy["orderId"])["statusCode"] == 404
    assert guest_pay(env, "L.x.y", expectedAmount=1)["statusCode"] == 404


def test_pay_starts_one_checkout_for_the_whole_bill(env):
    first = placed(env)
    placed(env)
    paid = start_paying(env, first)
    (call,) = session_calls(env)
    assert call["key"] == f"bill-checkout-{paid['billId']}" and call["account"] == ACCOUNT
    params = call["params"]
    assert [li["price_data"]["product_data"]["name"] for li in params["line_items"]] == [
        "Beställning #1", "Beställning #2"]
    assert params["metadata"]["kind"] == "table_bill" and params["metadata"]["billId"] == paid["billId"]
    assert params["ui_mode"] == "elements" and "/notan/" + LOC_A in params["return_url"]
    assert f"nota={paid['billId']}#" in params["return_url"]
    assert paid["amount"] == 2 * PIZZA_COLA and paid["count"] == 2 and paid["clientSecret"]
    lock = tab(env)["paying"]
    assert lock["billId"] == paid["billId"] and lock["amount"] == 2 * PIZZA_COLA
    bill = table_bills.load_bill(LOC_A, paid["billId"])
    assert bill["status"] == "paying" and bill["checkoutSessionId"] == "cs_test_1" and bill["ttl"] > 0


def test_amount_changed_answers_with_the_fresh_bill(env):
    first = placed(env)
    response = guest_pay(env, first["statusToken"], order=first["orderId"], expectedAmount=100)
    body = body_of(response)
    assert response["statusCode"] == 409 and body["error"] == "bill_changed"
    assert body["bill"]["amount"] == PIZZA_COLA and session_calls(env) == []


def test_someone_paying_blocks_for_two_minutes_then_can_be_taken_over(env):
    first = placed(env)
    paid = start_paying(env, first)
    again = guest_pay(env, first["statusToken"], order=first["orderId"], expectedAmount=PIZZA_COLA)
    assert again["statusCode"] == 409 and body_of(again)["error"] == "bill_being_paid"
    assert int(again["headers"]["Retry-After"]) > 0
    at(env, NOW + timedelta(seconds=121))
    taken = body_of(guest_pay(env, first["statusToken"], order=first["orderId"], expectedAmount=PIZZA_COLA))
    assert taken["billId"] != paid["billId"]
    assert any(c["path"] == "/v1/checkout/sessions/cs_test_1/expire" for c in env["stripe"].calls)
    assert table_bills.load_bill(LOC_A, paid["billId"])["status"] == "expired"
    assert tab(env)["paying"]["billId"] == taken["billId"]


def test_takeover_of_a_paid_session_settles_instead(env):
    first = placed(env)
    paid = start_paying(env, first)
    env["stripe"].session = bill_session(env)
    original = env["stripe"].__call__

    def no_expire(method, path, params=None, **kw):
        if path.endswith("/expire"):
            raise stripe_client.StripeError("checkout_session_complete", "complete")
        return original(method, path, params, **kw)
    env["monkeypatch"].setattr(stripe_client, "request", no_expire)
    at(env, NOW + timedelta(seconds=200))
    response = guest_pay(env, first["statusToken"], order=first["orderId"], expectedAmount=PIZZA_COLA)
    assert body_of(response) == err("bill_paid")
    assert stored(env, first["orderId"])["paymentStatus"] == "paid"
    assert table_bills.load_bill(LOC_A, paid["billId"])["status"] == "paid"


def test_same_device_resumes_its_session(env):
    first = placed(env)
    paid = start_paying(env, first)
    again = guest_pay(env, first["statusToken"], order=first["orderId"], expectedAmount=PIZZA_COLA,
                      resumeBillId=paid["billId"], resumeToken=paid["billToken"])
    assert again["statusCode"] == 200 and body_of(again)["clientSecret"] == paid["clientSecret"]
    assert len(session_calls(env)) == 1


def test_too_small_bill_is_paid_at_the_counter(env):
    env["res"].Table(MENU_TABLE).put_item(Item=menu_item(LOC_A, "mint", "Mint", "2"))
    first = placed(env, items=[{"menuItemId": "mint", "quantity": 1}])
    assert body_of(guest_pay(env, first["statusToken"], order=first["orderId"], expectedAmount=200)) == \
        err("bill_below_minimum")


def test_stripe_error_releases_the_lock(env):
    first = placed(env)
    env["stripe"].fail = stripe_client.StripeError("api_error", "down")
    response = guest_pay(env, first["statusToken"], order=first["orderId"], expectedAmount=PIZZA_COLA)
    assert response["statusCode"] == 503 and body_of(response) == err("payment_unavailable")
    assert "paying" not in tab(env) and tab(env)["openCount"] == 1


def test_empty_bill(env):
    first = placed(env)
    staff(env, "PUT", f"{first['orderId']}/status", body={"status": "cancelled"})
    assert body_of(guest_pay(env, first["statusToken"], order=first["orderId"], expectedAmount=0)) == \
        err("bill_empty")


def test_webhook_settles_the_bill_and_every_order(env):
    first = placed(env)
    second = placed(env)
    paid = start_paying(env, first)
    result = body_of(webhook(env, stripe_event("checkout.session.completed", bill_session(env))))["result"]
    assert result == "bill_paid"
    for created in (first, second):
        item = stored(env, created["orderId"])
        assert item["paymentStatus"] == "paid" and item["billId"] == paid["billId"]
        assert item["paymentIntentId"] == "pi_bill" and item["paidAt"] == "2026-09-08T12:00:00Z"
        assert item["status"] == "confirmed" and "notice" not in item  # kitchen untouched, no per-order receipt
    bill = table_bills.load_bill(LOC_A, paid["billId"])
    assert bill["status"] == "paid" and "ttl" not in bill and "notice" not in bill  # no e-mail given
    after = tab(env)
    assert after["openCount"] == 0 and after["openTotal"] == 0 and "paying" not in after and after["ttl"] > 0
    again = body_of(webhook(env, stripe_event("checkout.session.completed", bill_session(env))))["result"]
    assert again == "already_settled"


def test_payer_email_gets_the_bill_notice(env):
    first = placed(env)
    body_of(guest_pay(env, first["statusToken"], order=first["orderId"], expectedAmount=PIZZA_COLA,
                      email="Payer@Example.se"))
    webhook(env, stripe_event("checkout.session.completed", bill_session(env)))
    (bill,) = [b for b in env["res"].Table(ORDER_TABLE).scan()["Items"] if b["SK"].startswith("BILL#")]
    assert bill["payerEmail"] == "payer@example.se" and bill["notice"]["type"] == "bill_paid"


def test_payer_gets_one_receipt_for_the_bill(env, notify):
    module, ses, _ = notify
    first = placed(env)
    paid = body_of(guest_pay(env, first["statusToken"], order=first["orderId"], expectedAmount=PIZZA_COLA,
                             email="payer@example.se"))
    webhook(env, stripe_event("checkout.session.completed", bill_session(env)))
    bill = table_bills.load_bill(LOC_A, paid["billId"])
    assert module.handler({"Records": [order_record(bill)]}, None) == {"batchItemFailures": []}
    (mail,) = ses.calls
    assert mail["Destination"]["ToAddresses"] == ["payer@example.se"]
    assert mail["Message"]["Subject"]["Data"].startswith("Kvitto för notan vid Bord 7")
    assert "#1 Margherita" in mail["Message"]["Body"]["Text"]["Data"]


def test_guest_page_polling_settles_the_bill(env):
    first = placed(env)
    paid = start_paying(env, first)
    env["stripe"].session = bill_session(env)
    view = body_of(guest_get(env, paid["billToken"], path_bill=paid["billId"]))
    assert view["status"] == "paid" and view["amount"] == PIZZA_COLA and view["tableTitle"] == "Bord 7"
    assert stored(env, first["orderId"])["paymentStatus"] == "paid"
    assert guest_get(env, "wrong", path_bill=paid["billId"])["statusCode"] == 404


def test_order_placed_while_paying_goes_on_the_next_bill(env):
    first = placed(env)
    start_paying(env, first)
    later = placed(env)
    assert tab(env)["openCount"] == 2
    webhook(env, stripe_event("checkout.session.completed", bill_session(env)))
    after = tab(env)
    assert after["openCount"] == 1 and after["openTotal"] == PIZZA_COLA and "paying" not in after
    assert list(table_bills.entries(after)) == [later["orderId"]]
    assert stored(env, later["orderId"])["paymentStatus"] == "unpaid"


def test_expired_checkout_frees_the_bill(env):
    first = placed(env)
    paid = start_paying(env, first)
    session = dict(env["stripe"].session, status="expired")
    assert body_of(webhook(env, stripe_event("checkout.session.expired", session)))["result"] == "bill_failed"
    assert "paying" not in tab(env) and stored(env, first["orderId"])["paymentStatus"] == "unpaid"
    assert table_bills.load_bill(LOC_A, paid["billId"])["status"] == "expired"


def test_events_from_another_account_are_ignored(env):
    first = placed(env)
    start_paying(env, first)
    result = body_of(webhook(env, stripe_event("checkout.session.completed", bill_session(env),
                                               account="acct_other")))["result"]
    assert result == "ignored" and stored(env, first["orderId"])["paymentStatus"] == "unpaid"


def test_mismatch_settles_the_rest_and_flags_the_bill(env):
    first = placed(env)
    second = placed(env)
    paid = start_paying(env, first)
    # Should never happen while locked: one order is no longer unpaid.
    item = stored(env, second["orderId"])
    env["res"].Table(ORDER_TABLE).update_item(Key={"PK": item["PK"], "SK": item["SK"]},
                                              UpdateExpression="SET paymentStatus = :p",
                                              ExpressionAttributeValues={":p": "paid_outside"})
    webhook(env, stripe_event("checkout.session.completed", bill_session(env)))
    bill = table_bills.load_bill(LOC_A, paid["billId"])
    assert bill["status"] == "paid" and bill["mismatch"] == [second["orderId"]]
    assert stored(env, first["orderId"])["paymentStatus"] == "paid"
    after = tab(env)
    assert "paying" not in after and after["mismatch"]["orderIds"] == [second["orderId"]]
    assert list(table_bills.entries(after)) == [second["orderId"]]


def test_refund_on_a_bill_payment_is_recorded_on_the_bill(env):
    first = placed(env)
    paid = start_paying(env, first)
    webhook(env, stripe_event("checkout.session.completed", bill_session(env)))
    charge = {"payment_intent": "pi_bill", "amount_refunded": 1000,
              "metadata": {"kind": "table_bill", "billId": paid["billId"], "locationId": LOC_A}}
    assert body_of(webhook(env, stripe_event("charge.refunded", charge)))["result"] == "external"
    assert table_bills.load_bill(LOC_A, paid["billId"])["externalRefund"]["amount"] == 1000
    assert body_of(webhook(env, stripe_event("charge.refunded", charge)))["result"] == "already_recorded"


def test_owner_refunds_one_order_of_a_paid_bill(env):
    first = placed(env)
    start_paying(env, first)
    webhook(env, stripe_event("checkout.session.completed", bill_session(env)))
    response = staff(env, "POST", f"{first['orderId']}/refund", body={"amount": 5000})
    assert body_of(response)["paymentStatus"] == "partly_refunded"
    (call,) = [c for c in env["stripe"].calls if c["path"] == "/v1/refunds"]
    assert call["params"]["payment_intent"] == "pi_bill" and call["params"]["amount"] == 5000


# --- staff -------------------------------------------------------------------------------------

def test_staff_list_open_bills_oldest_first_with_stale_flag(env):
    placed(env, code=CODE_3)
    at(env, NOW + timedelta(minutes=5))
    placed(env)
    # A bill left from yesterday.
    env["res"].Table(ORDER_TABLE).put_item(Item={
        **table_bills.tab_key(LOC_A, "2026-09-07", "t5"), "recordType": "tableTab", "tenantId": TENANT_A,
        "locationId": LOC_A, "tableId": "t5", "tableLabel": "5", "serviceDate": "2026-09-07",
        "openCount": 0, "openTotal": 0, "paying": {"billId": "b" * 32, "orderIds": [], "amount": 0,
                                                    "at": "2026-09-07T20:00:00Z"}})
    body = body_of(staff(env, "GET", "tabs", role="staff_user"))
    assert body["serviceDate"] == TODAY
    titles = [t["title"] for t in body["tabs"]]
    assert titles == ["Fönsterbordet", "Bord 7", "Bord 5"]
    assert [t["stale"] for t in body["tabs"]] == [False, False, True]
    assert body["tabs"][0]["amount"] == PIZZA_COLA and body["tabs"][0]["orders"][0]["customerName"] == "Anna Svensson"


def test_pay_link_opens_the_bill_and_expires(env):
    first = placed(env)
    link = body_of(staff(env, "POST", f"tabs/{TODAY}/t-7/pay-link", body={}, role="staff_user"))
    assert link["amount"] == PIZZA_COLA and link["count"] == 1
    assert link["url"].startswith("https://dev123.cloudfront.net/notan/" + LOC_A + "?restaurang=roma#L.")
    token = link["url"].split("#", 1)[1]
    assert body_of(guest_get(env, token))["amount"] == PIZZA_COLA
    assert guest_pay(env, token, expectedAmount=PIZZA_COLA)["statusCode"] == 201
    at(env, NOW + timedelta(hours=13))
    assert guest_get(env, token)["statusCode"] == 404
    audits = audit_rows(env, first["orderId"])
    assert [a["action"] for a in audits] == ["pay_link"]


def audit_rows(env, order_id):
    return env["res"].Table(ORDER_TABLE).query(
        KeyConditionExpression=Key("PK").eq(f"LOCATION#{LOC_A}") & Key("SK").begins_with(f"AUDIT#ORDER#{order_id}#"),
    )["Items"]


def test_staff_mark_paid_outside_booqy(env):
    first = placed(env)
    second = placed(env)
    path = f"tabs/{TODAY}/t-7/paid-outside"
    assert body_of(staff(env, "POST", path, body={"method": "card", "expectedAmount": 1}, role="staff_user")) == \
        err("method must be card_terminal, cash, swish or other")
    assert body_of(staff(env, "POST", path, body={"method": "cash", "expectedAmount": 1}, role="staff_user")) == \
        err("bill_changed")
    body = body_of(staff(env, "POST", path, role="staff_user",
                         body={"method": "swish", "note": "Swish till 123", "expectedAmount": 2 * PIZZA_COLA}))
    assert body["tab"]["count"] == 0 and len(body["orders"]) == 2
    for created in (first, second):
        item = stored(env, created["orderId"])
        assert item["paymentStatus"] == "paid_outside" and item["paidAt"] == "2026-09-08T12:00:00Z"
        assert item["paidOutside"]["method"] == "swish" and item["paidOutside"]["note"] == "Swish till 123"
        assert item["status"] == "confirmed"
        assert [a["action"] for a in audit_rows(env, created["orderId"])] == ["paid_outside"]
    assert env["stripe"].calls == []
    status = body_of(env["m"]["get"].handler({
        "routeKey": "GET /locations/{locationId}/order-status/{orderId}",
        "pathParameters": {"locationId": LOC_A, "orderId": first["orderId"]},
        "headers": {"X-Order-Token": first["statusToken"]}}, None))
    assert status["paidOutside"] is True and "bill" not in status


def test_mark_paid_outside_one_order_only(env):
    first = placed(env)
    second = placed(env)
    staff(env, "POST", f"tabs/{TODAY}/t-7/paid-outside",
          body={"method": "cash", "orderIds": [first["orderId"]], "expectedAmount": PIZZA_COLA})
    assert list(table_bills.entries(tab(env))) == [second["orderId"]]


def test_mark_paid_outside_while_a_guest_pays_expires_the_payment_first(env):
    first = placed(env)
    paid = start_paying(env, first)
    body = body_of(staff(env, "POST", f"tabs/{TODAY}/t-7/paid-outside",
                         body={"method": "card_terminal", "expectedAmount": PIZZA_COLA}))
    assert body["orders"][0]["paymentStatus"] == "paid_outside"
    assert table_bills.load_bill(LOC_A, paid["billId"])["status"] == "expired"
    assert "paying" not in tab(env)


def test_write_off_is_for_the_owner(env):
    first = placed(env)
    path = f"tabs/{TODAY}/t-7/write-off"
    assert body_of(staff(env, "POST", path, body={"reason": "Gick", "expectedAmount": PIZZA_COLA},
                         role="staff_user")) == err("owner_only")
    assert body_of(staff(env, "POST", path, body={"reason": " ", "expectedAmount": PIZZA_COLA})) == \
        err("reason is required")
    body_of(staff(env, "POST", path, body={"reason": "Gästen gick utan att betala", "expectedAmount": PIZZA_COLA}))
    item = stored(env, first["orderId"])
    assert item["paymentStatus"] == "written_off" and item["writtenOff"]["reason"] == "Gästen gick utan att betala"
    assert "paidAt" not in item


def test_cancel_unpaid_order_refunds_nothing(env):
    first = placed(env)
    second = placed(env, email=None)
    body = body_of(set_status(env, first["orderId"], "cancelled", role="staff_user"))
    assert body["status"] == "cancelled" and body["paymentStatus"] == "void"
    assert env["stripe"].calls == []
    item = stored(env, first["orderId"])
    assert item["notice"]["type"] == "order_cancelled"
    assert list(table_bills.entries(tab(env))) == [second["orderId"]]
    assert [a["action"] for a in audit_rows(env, first["orderId"])] == ["cancelled_unpaid"]
    body_of(set_status(env, second["orderId"], "cancelled"))
    assert "notice" not in stored(env, second["orderId"])  # no e-mail given: nothing to send
    assert tab(env)["openCount"] == 0


def test_settled_outside_cannot_be_cancelled(env):
    first = placed(env)
    staff(env, "POST", f"tabs/{TODAY}/t-7/paid-outside", body={"method": "cash", "expectedAmount": PIZZA_COLA})
    assert body_of(set_status(env, first["orderId"], "cancelled")) == err("settled_outside_booqy")


def test_unpaid_order_can_be_served_and_completed(env):
    first = placed(env)
    for status in ("preparing", "ready", "completed"):
        assert body_of(set_status(env, first["orderId"], status))["status"] == status
    assert stored(env, first["orderId"])["paymentStatus"] == "unpaid"
    assert tab(env)["openCount"] == 1


# --- pickup unchanged ---------------------------------------------------------------------------

PICKUP_KEYS = {"PK", "SK", "orderId", "tenantId", "locationId", "date", "timezone", "mode", "lines",
               "totalAmount", "vat", "currency", "customerName", "customerEmail", "customerPhone", "language",
               "prepMinutes", "status", "paymentStatus", "stripeAccountId", "termsAcceptedAt", "createdAt",
               "updatedAt", "version", "history", "ttl", "checkoutSessionId"}


def test_pickup_order_is_written_exactly_as_before(env):
    created = body_of(post_order(env))
    item = stored(env, created["orderId"])
    assert set(item) == PICKUP_KEYS
    assert item["status"] == "pending_payment" and item["paymentStatus"] == "processing"
    assert created["checkoutUrl"] and tab(env) is None


def test_pickup_checkout_params_are_unchanged(env):
    now = NOW
    item = {"orderId": "a" * 32, "locationId": LOC_A, "tenantId": TENANT_A, "date": TODAY,
            "customerEmail": "anna@example.se", "language": "sv",
            "lines": [{"name": "Margherita", "unitPrice": 12500, "quantity": 2}]}
    order_payments.create_checkout(item, ACCOUNT, "https://s/ok", "https://s/no", now)
    (call,) = env["stripe"].calls
    meta = {"orderId": "a" * 32, "locationId": LOC_A, "tenantId": TENANT_A, "date": TODAY}
    assert call["key"] == f"checkout-{'a' * 32}" and call["params"] == {
        "mode": "payment",
        "line_items": [{"price_data": {"currency": "sek", "unit_amount": 12500,
                                       "product_data": {"name": "Margherita"}}, "quantity": 2}],
        "client_reference_id": "a" * 32, "customer_email": "anna@example.se",
        "wallet_options": {"link": {"display": "never"}}, "metadata": meta,
        "payment_intent_data": {"metadata": meta, "description": f"Beställning {TODAY} (aaaaaaaa)"},
        "expires_at": int((now + timedelta(minutes=31)).timestamp()),
        "locale": "sv", "success_url": "https://s/ok", "cancel_url": "https://s/no",
    }


def test_pickup_webhook_settle_is_unchanged(env):
    created = body_of(post_order(env))
    webhook(env, stripe_event("checkout.session.completed", paid_session(env, created["orderId"])))
    item = stored(env, created["orderId"])
    assert item["status"] == "confirmed" and item["paymentStatus"] == "paid" and item["notice"]["type"] == \
        "order_confirmed"
    assert "payLater" not in item and "billId" not in item and "paidAt" not in item
