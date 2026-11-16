"""Audit #50: giving an order's gift card part back when the card is void
answers ok (nothing to credit) instead of failing forever."""

import pytest

from shared.features import gift_cards as gc
from tenant_support import LOC_A, TENANT_A
from test_gift_cards import env, hook, issue  # noqa: F401 (env: fixture)


@pytest.mark.parametrize("op", ["reverse", "refund"])
def test_reverse_on_a_void_card_answers_ok_and_leaves_the_card(env, op):
    code, card_id = issue(env, amount=250)
    reserve = hook(env, "checkout.giftcard", tenantId=TENANT_A, op="reserve", locationId=LOC_A, orderId="ord1",
                   code=code, totalOre=10000)
    assert reserve["ok"] and reserve["debitedOre"] == 10000
    gc.table().update_item(Key=gc.card_key(TENANT_A, card_id), UpdateExpression="SET #s = :v",
                           ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":v": "void"})
    before = gc.get_card(TENANT_A, card_id)
    answer = hook(env, "checkout.giftcard", tenantId=TENANT_A, op=op, locationId=LOC_A, orderId="ord1")
    assert answer == {"ok": True, "reversedOre": 0, "code": "card_void"}
    after = gc.get_card(TENANT_A, card_id)
    assert after["status"] == "void" and after["balanceOre"] == before["balanceOre"]
    assert gc.get_tx(TENANT_A, card_id, f"{op}-order-ord1") is None
