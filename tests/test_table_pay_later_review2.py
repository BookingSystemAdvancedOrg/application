"""Pay-later table bills, second review: an open bill never expires, a full
dining room on one wifi is not blocked, Layout-only staff see no guest data,
money for a closed bill is never silent, and a mismatch settles in one
transaction (repairable after a crash)."""

import pytest
from botocore.exceptions import ClientError

from error_body import err
import logging

from shared import dynamo, table_bills, tenant
from shared import orders as o
from tenant_support import TENANT_TABLE
from test_orders import (LOC_A, NOW, ORDER_TABLE, TENANT_A, TODAY, body_of, env, post_order, set_status,  # noqa: F401
                         stored, stripe_event, webhook)
from test_table_pay_later import (CODE, CODE_3, PIZZA_COLA, bill_session, code_of, guest_pay, placed,  # noqa: F401
                                  start_paying, staff, tab)


def _guard_table(env):
    env["res"].create_table(TableName="test-guard", KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
                            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
                            BillingMode="PAY_PER_REQUEST")
    env["monkeypatch"].setenv("REQUEST_GUARD_TABLE_NAME", "test-guard")


# --- 1. an open bill never expires -------------------------------------------------------------

def test_only_an_empty_unlocked_unflagged_bill_gets_a_ttl(env):
    first = placed(env)
    second = placed(env)
    # Tidying an open bill (any path may call it) never sets a TTL.
    table_bills._tidy_tab(LOC_A, TODAY, "t-7", NOW)
    assert "ttl" not in tab(env)
    set_status(env, first["orderId"], "cancelled")
    assert "ttl" not in tab(env) and tab(env)["openCount"] == 1
    # Locked (a guest paying): no TTL either.
    start_paying(env, second)
    table_bills._tidy_tab(LOC_A, TODAY, "t-7", NOW)
    assert "ttl" not in tab(env)
    webhook(env, stripe_event("checkout.session.completed", bill_session(env)))
    assert tab(env)["openCount"] == 0 and tab(env)["ttl"] > 0
    # A new order on the emptied bill takes the TTL away again.
    placed(env)
    assert "ttl" not in tab(env)


# --- 2. one wifi for the whole room ------------------------------------------------------------

def test_a_full_room_on_one_ip_can_order_at_the_table(env):
    _guard_table(env)
    codes = [CODE, CODE_3, ("t1", "1"), ("t5", "5")]
    for n in range(24):
        response = post_order(env, mode="table", tableCode=code_of(codes[n % 4]), payLater=True,
                              phone=f"070-100 00 {n:02d}", email=None)
        assert response["statusCode"] == 201, (n, response)


def test_pickup_keeps_its_ip_limit(env):
    _guard_table(env)
    for n in range(10):
        assert post_order(env, phone=f"070-200 00 {n:02d}", email=f"g{n}@example.se")["statusCode"] == 201
    response = post_order(env, phone="070-200 00 99", email="g99@example.se")
    assert response["statusCode"] == 429 and body_of(response) == err("too_many_requests")


def test_a_full_room_on_one_ip_can_pay(env):
    _guard_table(env)
    first = placed(env)
    for _ in range(30):
        response = guest_pay(env, "wrong", order=first["orderId"], expectedAmount=PIZZA_COLA)
        assert response["statusCode"] == 404


# --- 3. Layout-only staff ----------------------------------------------------------------------

def _layout_only(env):
    table = env["res"].Table(TENANT_TABLE)
    table.update_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"}, UpdateExpression="SET staffAccess = :a",
                      ExpressionAttributeValues={":a": {"defaults": ["layout"]}})
    tenant._tenant_cache.clear()


def test_layout_only_staff_see_the_bills_without_guest_data(env):
    placed(env, notes="Nötallergi")
    _layout_only(env)
    response = staff(env, "GET", "tabs", role="staff_user")
    assert response["statusCode"] == 200
    (bill,) = body_of(response)["tabs"]
    assert bill["amount"] == PIZZA_COLA and bill["orders"][0]["number"] == 1
    assert bill["orders"][0]["lines"] and bill["orders"][0]["totalAmount"] == PIZZA_COLA
    for private in ("Anna", "070", "anna@example.se", "Nötallergi", "customer", "history"):
        assert private not in response["body"], private
    # They still can't act on it.
    assert staff(env, "POST", f"tabs/{TODAY}/t-7/paid-outside", role="staff_user",
                 body={"method": "cash", "expectedAmount": PIZZA_COLA})["statusCode"] == 403


def test_bestallningar_staff_and_owner_still_see_who_ordered(env):
    placed(env)
    for role in ("staff_user", "owner_user"):
        (bill,) = body_of(staff(env, "GET", "tabs", role=role))["tabs"]
        assert bill["orders"][0]["customerName"] == "Anna Svensson"
        assert bill["orders"][0]["customerPhone"]


# --- 8. money for a closed bill ----------------------------------------------------------------

class _Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_payment_for_a_closed_bill_is_flagged_for_the_owner(env):
    first = placed(env)
    paid = start_paying(env, first)
    # The bill was closed (expired) and the order then paid another way.
    expired = dict(env["stripe"].session, status="expired")
    assert body_of(webhook(env, stripe_event("checkout.session.expired", expired)))["result"] == "bill_failed"
    staff(env, "POST", f"tabs/{TODAY}/t-7/paid-outside", body={"method": "cash", "expectedAmount": PIZZA_COLA})
    errors = _Errors()
    logging.getLogger().addHandler(errors)
    try:
        result = body_of(webhook(env, stripe_event("checkout.session.completed", bill_session(env))))["result"]
    finally:
        logging.getLogger().removeHandler(errors)
    assert result == "bill_paid_after_close"
    assert any("paid_after_close" in m for m in errors.messages)
    bill = table_bills.load_bill(LOC_A, paid["billId"])
    assert bill["status"] == "paid" and "ttl" not in bill and bill["paymentIntentId"] == "pi_bill"
    assert bill["mismatch"] == [first["orderId"]] and bill["paidAfterClose"]["previousStatus"] == "expired"
    assert stored(env, first["orderId"])["paymentStatus"] == "paid_outside"  # nothing settled twice
    flagged = tab(env)
    assert flagged["mismatch"]["billId"] == paid["billId"] and flagged["mismatch"]["amount"] == PIZZA_COLA
    assert "ttl" not in flagged
    listed = body_of(staff(env, "GET", "tabs"))["tabs"]
    assert [t["mismatch"]["billId"] for t in listed] == [paid["billId"]]
    # The same event again: recorded once.
    again = body_of(webhook(env, stripe_event("checkout.session.completed", bill_session(env))))["result"]
    assert again == "already_settled"


def test_payment_for_a_closed_bill_whose_table_record_expired_is_still_flagged(env):
    first = placed(env)
    paid = start_paying(env, first)
    webhook(env, stripe_event("checkout.session.expired", dict(env["stripe"].session, status="expired")))
    set_status(env, first["orderId"], "cancelled")
    env["res"].Table(ORDER_TABLE).delete_item(Key=table_bills.tab_key(LOC_A, TODAY, "t-7"))  # TTL removed it
    assert body_of(webhook(env, stripe_event("checkout.session.completed", bill_session(env))))["result"] == \
        "bill_paid_after_close"
    flagged = tab(env)
    assert flagged["mismatch"]["billId"] == paid["billId"] and flagged["tenantId"] == TENANT_A
    assert flagged["openCount"] == 0 and flagged["tableLabel"] == "Bord 7"
    (listed,) = body_of(staff(env, "GET", "tabs"))["tabs"]
    assert listed["title"] == "Bord 7"


# --- 9. mismatch in one transaction ------------------------------------------------------------

def _make_mismatch(env):
    first = placed(env)
    second = placed(env)
    paid = start_paying(env, first)
    item = stored(env, second["orderId"])
    env["res"].Table(ORDER_TABLE).update_item(Key={"PK": item["PK"], "SK": item["SK"]},
                                              UpdateExpression="SET paymentStatus = :p",
                                              ExpressionAttributeValues={":p": "paid_outside"})
    return first, second, paid


def test_mismatch_after_a_crash_is_repaired_by_the_next_try(env):
    first, second, paid = _make_mismatch(env)
    real_client = dynamo.client
    real_table = table_bills._table
    armed = {"on": False, "fired": False}

    def boom():
        armed["fired"] = True
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "crash"}}, "Write")

    class Client:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def transact_write_items(self, **kw):
            text = repr(kw)
            if armed["on"] and not armed["fired"] and "TABLETAB#" in text and "paying" in text:
                boom()
            return self._inner.transact_write_items(**kw)

    class Table:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def update_item(self, **kw):
            if armed["on"] and not armed["fired"] and "TABLETAB#" in str(kw.get("Key")) and \
                    "REMOVE" in kw.get("UpdateExpression", ""):
                boom()
            return self._inner.update_item(**kw)

    real_mismatch = table_bills._settle_mismatch

    def mismatch(*a, **kw):
        armed["on"] = True
        return real_mismatch(*a, **kw)

    env["monkeypatch"].setattr(dynamo, "client", lambda: Client(real_client()))
    env["monkeypatch"].setattr(table_bills, "_table", lambda: Table(real_table()))
    env["monkeypatch"].setattr(table_bills, "_settle_mismatch", mismatch)
    with pytest.raises(ClientError):
        table_bills.on_session_event("checkout.session.completed", bill_session(env),
                                     table_bills.load_bill(LOC_A, paid["billId"])["stripeAccountId"], NOW)
    assert armed["fired"]
    # The crash left nothing half written: the bill is still paying and holds the lock.
    assert table_bills.load_bill(LOC_A, paid["billId"])["status"] == "paying"
    assert tab(env)["paying"]["billId"] == paid["billId"]
    # The webhook's retry repairs it.
    assert body_of(webhook(env, stripe_event("checkout.session.completed", bill_session(env))))["result"] == \
        "bill_paid"
    bill = table_bills.load_bill(LOC_A, paid["billId"])
    assert bill["status"] == "paid" and bill["mismatch"] == [second["orderId"]]  # first is not "missed"
    after = tab(env)
    assert "paying" not in after and after["mismatch"]["orderIds"] == [second["orderId"]]
    assert after["mismatch"]["amount"] == PIZZA_COLA
    assert list(table_bills.entries(after)) == [second["orderId"]] and after["openCount"] == 1
    assert stored(env, first["orderId"])["paymentStatus"] == "paid"


def test_mismatch_writes_bill_and_table_together(env):
    _, second, paid = _make_mismatch(env)
    real_client = dynamo.client
    seen = []

    class Client:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def transact_write_items(self, **kw):
            seen.append(sorted(op["Update"]["Key"]["SK"]["S"].split("#")[0] for op in kw["TransactItems"]
                               if "Update" in op))
            return self._inner.transact_write_items(**kw)

    env["monkeypatch"].setattr(dynamo, "client", lambda: Client(real_client()))
    webhook(env, stripe_event("checkout.session.completed", bill_session(env)))
    assert ["BILL", "TABLETAB"] in seen
    assert table_bills.load_bill(LOC_A, paid["billId"])["status"] == "paid"


def test_mismatch_while_a_new_order_arrives_still_settles(env):
    first, second, paid = _make_mismatch(env)
    real_update = table_bills._mismatch_tab_update
    state = {"done": False}

    def racing(*a, **kw):
        op = real_update(*a, **kw)
        if not state["done"]:
            state["done"] = True
            placed(env)  # bumps the table's version between read and write
        return op

    env["monkeypatch"].setattr(table_bills, "_mismatch_tab_update", racing)
    webhook(env, stripe_event("checkout.session.completed", bill_session(env)))
    after = tab(env)
    assert "paying" not in after and after["openCount"] == 2
    assert table_bills.load_bill(LOC_A, paid["billId"])["status"] == "paid"


def test_paid_after_close_is_not_used_for_a_bill_still_paying(env):
    first = placed(env)
    paid = start_paying(env, first)
    assert body_of(webhook(env, stripe_event("checkout.session.completed", bill_session(env))))["result"] == \
        "bill_paid"
    assert "paidAfterClose" not in table_bills.load_bill(LOC_A, paid["billId"])
    assert o.PAY_PAID == stored(env, first["orderId"])["paymentStatus"]
