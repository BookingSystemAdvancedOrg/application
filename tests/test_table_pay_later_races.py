"""Pay-later table bills: races found in review. The payment lock must hold
the exact bill the guest saw, and a bill flagged for the owner must not
expire before the owner has checked it."""

from shared import table_bills
from shared import orders as o
from test_orders import LOC_A, NOW, ORDER_TABLE, TODAY, body_of, env, set_status, stored, stripe_event, webhook  # noqa: F401
from test_table_pay_later import (PIZZA_COLA, bill_session, guest_pay, placed, session_calls, staff,  # noqa: F401
                                  start_paying, tab)


def test_lock_refuses_a_bill_that_changed_to_the_same_count_and_total(env):
    """One order cancelled and another of the same price placed while the
    guest's pay request is under way: count and total are unchanged, but the
    orders are not. The guest must not be charged for the cancelled order
    while the new one stays on the bill."""
    first = placed(env)
    second = placed(env)
    real_new_bill = table_bills._new_bill
    seen = {}

    def concurrent(ctx, location_id, tab_, orders, amount, account, email, now):
        if not seen:
            seen["third"] = None
            assert body_of(set_status(env, second["orderId"], "cancelled"))["paymentStatus"] == "void"
            seen["third"] = placed(env)
        return real_new_bill(ctx, location_id, tab_, orders, amount, account, email, now)

    env["monkeypatch"].setattr(table_bills, "_new_bill", concurrent)
    response = guest_pay(env, first["statusToken"], order=first["orderId"], expectedAmount=2 * PIZZA_COLA)
    body = body_of(response)
    assert response["statusCode"] == 409 and body["error"] == "bill_changed", body
    assert session_calls(env) == []
    after = tab(env)
    assert "paying" not in after
    assert set(table_bills.entries(after)) == {first["orderId"], seen["third"]["orderId"]}
    # Paying again with the fresh bill locks the orders that are really on it.
    paid = body_of(guest_pay(env, first["statusToken"], order=first["orderId"], expectedAmount=2 * PIZZA_COLA))
    assert set(tab(env)["paying"]["orderIds"]) == {first["orderId"], seen["third"]["orderId"]}
    assert paid["amount"] == 2 * PIZZA_COLA


def test_flagged_bill_is_kept_until_the_owner_has_checked_it(env):
    first = placed(env)
    paid = start_paying(env, first)
    # Should never happen while locked: the order left the bill some other way.
    item = stored(env, first["orderId"])
    table = env["res"].Table(ORDER_TABLE)
    table.update_item(Key={"PK": item["PK"], "SK": item["SK"]}, UpdateExpression="SET paymentStatus = :p",
                      ExpressionAttributeValues={":p": "paid_outside"})
    key = table_bills.tab_key(LOC_A, TODAY, "t-7")
    table.update_item(Key=key, UpdateExpression="SET openCount = :z, openTotal = :z REMOVE #e",
                      ExpressionAttributeNames={"#e": table_bills.ENTRY + first["orderId"]},
                      ExpressionAttributeValues={":z": 0})
    webhook(env, stripe_event("checkout.session.completed", bill_session(env)))
    assert table_bills.load_bill(LOC_A, paid["billId"])["mismatch"] == [first["orderId"]]
    flagged = tab(env)
    assert flagged["mismatch"]["orderIds"] == [first["orderId"]] and "paying" not in flagged
    # Any later tidy-up (an empty bill normally expires in 3 days) keeps it.
    table_bills._tidy_tab(LOC_A, TODAY, "t-7", NOW)
    assert "ttl" not in tab(env)
    listed = body_of(staff(env, "GET", "tabs"))["tabs"]
    assert [t["tableId"] for t in listed] == ["t-7"] and listed[0]["mismatch"]["billId"] == paid["billId"]
    # Once the owner has checked it, the empty bill expires like any other.
    assert staff(env, "POST", f"tabs/{TODAY}/t-7/mismatch-checked", body={})["statusCode"] == 200
    after = tab(env)
    assert "mismatch" not in after and after["ttl"] > 0
    assert o.PAY_OUTSIDE == stored(env, first["orderId"])["paymentStatus"]
