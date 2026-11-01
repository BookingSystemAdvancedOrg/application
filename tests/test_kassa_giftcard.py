"""Gift cards in Kassa (CONTRACTS.md 9.2 kassa.giftcard, AP12): a bill
payment {kind: "giftcard", paymentId} only with giftCards and kassa on,
verified and undone through the gift-cards feature. Until the control
system port has a voucher payment means (ControlSystem.voucher_method) the
payment itself is refused with 501, before any card is debited."""

import pytest

from error_body import err
from hooks_fake import install_hook, no_hooks
from shared import control_system as cs
from shared import kassa, kassa_receipt
from shared import tenant as shared_tenant
from tenant_support import LOC_A, LOC_B, STAFF_A_SUB, TENANT_A, TENANT_B, TENANT_TABLE
from test_kassa import bill, call, env, journal, pay, receipts_of, reg_row, register  # env: fixture

PID = "e" * 32


def giftcards_on(env, tenant_id=TENANT_A):
    env["res"].Table(TENANT_TABLE).update_item(
        Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"},
        UpdateExpression="SET entitlements.features.giftCards = :t", ExpressionAttributeValues={":t": True})
    shared_tenant.reset_caches()


def voucher_ok(monkeypatch):
    """The future: the control system port reports gift cards as vouchers."""
    monkeypatch.setattr(cs.ControlSystem, "voucher_method", "voucher")


def verify(amount, balance=15000):
    def answer(req):
        if req["op"] == "verify":
            return {"ok": True, "amountOre": amount, "cardId": "card1", "cardLast4": "1234",
                    "balanceAfterOre": balance}
        return {"ok": True, "amountOre": amount, "cardId": "card1", "cardLast4": "1234",
                "balanceAfterOre": balance + amount}
    return answer


def gift(env, bill_id, payment_id=PID, **kw):
    return call(env, "POST", f"bills/{bill_id}/payments", body={"kind": "giftcard", "paymentId": payment_id}, **kw)


def test_off_is_refused_without_a_hook_call(env, monkeypatch):
    calls = install_hook(monkeypatch, "gift-cards", "kassa.giftcard", verify(10000))
    b = bill(env, register(env))
    assert gift(env, b["billId"]) == (403, err("feature_not_in_plan"))
    assert calls == []


def test_verify_adds_the_payment_and_a_retry_is_idempotent(env, monkeypatch):
    voucher_ok(monkeypatch)
    giftcards_on(env)
    calls = install_hook(monkeypatch, "gift-cards", "kassa.giftcard", verify(10000))
    b = bill(env, register(env))
    status, payment = gift(env, b["billId"], role="staff_user", sub=STAFF_A_SUB)
    assert status == 201
    assert payment == {"paymentId": PID, "method": "Presentkort", "amount": 10000, "last4": "1234",
                       "balanceAfter": 15000, "status": "succeeded"}
    assert gift(env, b["billId"]) == (201, payment)
    assert [c["op"] for c in calls] == ["verify"]
    assert calls[0] == {"hook": "kassa.giftcard", "hookVersion": 1, "tenantId": TENANT_A, "op": "verify",
                        "locationId": LOC_A, "billId": b["billId"], "paymentId": PID}
    view = call(env, "GET", f"bills/{b['billId']}")[1]
    assert view["paid"] == 10000 and view["remaining"] == b["total"] - 10000 and view["locked"] is True
    stored = env["res"].Table("test-kassa").get_item(Key=kassa.bill_key(LOC_A, b["billId"]))["Item"]
    assert stored["giftCards"][0]["cardId"] == "card1" and stored["giftCards"][0]["method"] == "Presentkort"


@pytest.mark.parametrize("answer, status, code", [
    (lambda r: {"ok": False, "status": 409, "code": "card_expired"}, 409, "card_expired"),
    (lambda r: 1 / 0, 503, "gift_card_unavailable"),
])
def test_refused_or_failing_feature(env, monkeypatch, answer, status, code):
    voucher_ok(monkeypatch)
    giftcards_on(env)
    install_hook(monkeypatch, "gift-cards", "kassa.giftcard", answer)
    b = bill(env, register(env))
    assert gift(env, b["billId"]) == (status, err(code))
    assert call(env, "GET", f"bills/{b['billId']}")[1]["paid"] == 0


def test_not_deployed_is_503(env, monkeypatch):
    voucher_ok(monkeypatch)
    giftcards_on(env)
    no_hooks(monkeypatch)
    b = bill(env, register(env))
    assert gift(env, b["billId"]) == (503, err("gift_card_unavailable"))


def test_more_than_the_rest_is_reversed_and_refused(env, monkeypatch):
    voucher_ok(monkeypatch)
    giftcards_on(env)
    calls = install_hook(monkeypatch, "gift-cards", "kassa.giftcard", verify(10 ** 6))
    b = bill(env, register(env))
    assert gift(env, b["billId"]) == (409, err("amount_too_large"))
    assert [c["op"] for c in calls] == ["verify", "reverse"]


@pytest.mark.parametrize("body", [{"kind": "giftcard", "paymentId": "nope"}, {"kind": "giftcard"},
                                  {"kind": "giftcard", "paymentId": PID, "amount": 5}])
def test_validation(env, monkeypatch, body):
    giftcards_on(env)
    install_hook(monkeypatch, "gift-cards", "kassa.giftcard", verify(10000))
    b = bill(env, register(env))
    assert call(env, "POST", f"bills/{b['billId']}/payments", body=body)[0] == 400


def test_other_tenant_or_location_is_404(env, monkeypatch):
    giftcards_on(env)
    giftcards_on(env, TENANT_B)
    calls = install_hook(monkeypatch, "gift-cards", "kassa.giftcard", verify(10000))
    b = bill(env, register(env))
    assert gift(env, b["billId"], tenant_id=TENANT_B, location_id=LOC_B)[0] == 404
    assert gift(env, b["billId"], role="staff_user", sub=STAFF_A_SUB, location_id=LOC_B)[0] == 404
    assert calls == []


def test_training_bill_takes_no_gift_card(env, monkeypatch):
    voucher_ok(monkeypatch)
    giftcards_on(env)
    install_hook(monkeypatch, "gift-cards", "kassa.giftcard", verify(10000))
    rid = register(env)
    assert call(env, "POST", f"registers/{rid}/training", body={"on": True})[0] == 200
    b = bill(env, rid)
    assert gift(env, b["billId"]) == (409, err("training_no_payment"))


def test_undo_before_the_receipt_reverses(env, monkeypatch):
    voucher_ok(monkeypatch)
    giftcards_on(env)
    calls = install_hook(monkeypatch, "gift-cards", "kassa.giftcard", verify(10000))
    b = bill(env, register(env))
    gift(env, b["billId"])
    status, undone = call(env, "POST", f"bills/{b['billId']}/payments/{PID}/undo", body={})
    assert status == 200 and undone["status"] == "reversed" and undone["balanceAfter"] == 25000
    assert [c["op"] for c in calls] == ["verify", "reverse"]
    assert call(env, "GET", f"bills/{b['billId']}")[1]["paid"] == 0


def test_without_a_voucher_means_the_payment_is_refused_before_the_card_is_debited(env, monkeypatch):
    giftcards_on(env)
    calls = install_hook(monkeypatch, "gift-cards", "kassa.giftcard", verify(10000))
    rid = register(env)
    b = bill(env, rid)
    assert gift(env, b["billId"]) == (501, err("gift_card_receipt_not_supported"))
    assert calls == []
    assert call(env, "GET", f"bills/{b['billId']}")[1]["paid"] == 0
    # the rest of the till is untouched: a card sale on the same bill still closes it
    pay(env, b["billId"], b["total"], "a" * 32)
    assert call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "print"})[0] == 201
    assert kassa.verify_chain(journal(env, rid)) is None


def test_with_a_voucher_means_the_receipt_reports_the_gift_card(env, monkeypatch):
    voucher_ok(monkeypatch)
    giftcards_on(env)
    install_hook(monkeypatch, "gift-cards", "kassa.giftcard", verify(10000))
    seen = {}
    original = cs.SimulatedControlSystem.sign

    def spy(self, register, data):
        seen["payments"] = data.payments
        return original(self, register, data)
    monkeypatch.setattr(cs.SimulatedControlSystem, "sign", spy)
    rid = register(env)
    b = bill(env, rid)
    gift(env, b["billId"])
    pay(env, b["billId"], b["total"] - 10000, "a" * 32)
    assert call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "print"})[0] == 201
    assert {"method": "voucher", "amount": 10000, "last4": "1234", "funding": None} in seen["payments"]
    assert any(p["method"] == "card" for p in seen["payments"])
    assert kassa.verify_chain(journal(env, rid)) is None
    assert "lockId" not in reg_row(env, rid)


def test_journal_chain_still_verifies_after_gift_card_undo_and_a_card_sale(env, monkeypatch):
    voucher_ok(monkeypatch)
    giftcards_on(env)
    install_hook(monkeypatch, "gift-cards", "kassa.giftcard", verify(10000))
    rid = register(env)
    b = bill(env, rid)
    gift(env, b["billId"])
    call(env, "POST", f"bills/{b['billId']}/payments/{PID}/undo", body={})
    pay(env, b["billId"], b["total"], "a" * 32)
    assert call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "print"})[0] == 201
    assert kassa.verify_chain(journal(env, rid)) is None


def test_receipt_line_for_a_gift_card():
    row = kassa_receipt._card({"method": "Presentkort", "last4": "1234", "balanceAfter": 15000})
    assert row == "Presentkort ****1234, kvar 150,00 kr"
    assert kassa_receipt._card({"last4": "4242", "brand": "visa"}) == "Kort Visa **** 4242"
