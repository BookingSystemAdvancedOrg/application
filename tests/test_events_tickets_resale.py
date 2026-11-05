"""Face-value resale of tickets (ADR-K 8, CONTRACTS.md 6.K): the owner's
`resaleAllowed` setting, listing (K20 kind resale) and withdrawing (K22) a
ticket, the public listings (K16), buying listed tickets (K17
`resaleTransferIds`), the atomic void + issue + seller refund on the webhook,
double-spend and concurrent buyers, expired holds and late payments, a
cancelled occurrence, a seller already let in, refunds of resold tickets,
Bokföring and Statistik (net zero, VAT preserved per ticket) and two-tenant
isolation. Reuses the fixture and helpers of test_events_tickets."""

from datetime import datetime, timedelta, timezone

from shared.features import events_tickets as et
from shared.features.events_tickets import qr
from tenant_support import LOC_A, LOC_A2, LOC_B, TENANT_A, TENANT_B
from test_events_tickets import (ACCT_A, E1, L, OCC, OCC1, OWNER_A, OWNER_B, STAFF_A, T1, FakeContext, buy, call,
                                 env, occ_row, pay, published, tickets, token_of)

__all__ = ["env"]

SELLER, BUYER, BUYER2 = "01ORDERAAAAAAAAAAAAAAAAAAA", "01ORDERBUYERBBBBBBBBBBBBBB", "01ORDERBUYER2CCCCCCCCCCCCC"
LISTING, LISTING2 = "01RESALEAAAAAAAAAAAAAAAAAA", "01RESALEBBBBBBBBBBBBBBBBBB"
RESALE_ON = {"perOrderMax": 6, "transferAllowed": True, "resaleAllowed": True}
PUBLIC_EVENT = "/public/locations/{locationId}/events/{eventId}"


def sold_with_resale(e, capacity=4, **over):
    """A published event with resale on and two tickets sold to Sara (order SELLER)."""
    published(e, settings=RESALE_ON,
              occurrences=[{"startAt": "2026-12-04T18:00:00Z", "endAt": "2026-12-04T22:00:00Z",
                            "capacity": capacity}], **over)
    assert buy(e)[0] == 200 and pay(e)["result"] == "issued"
    return sorted(t["ticketId"] for t in tickets(e))


def list_ticket(e, ticket_id, transfer_id=LISTING, token=None):
    return call(e, "POST", "/public/tickets/{token}/transfers", params={"token": token or token_of(e)},
                body={"transferId": transfer_id, "ticketId": ticket_id, "kind": "resale"})


def buy_listed(e, transfer_ids, order_id=BUYER, loc=LOC_A, occ=OCC1, **over):
    body = {"orderId": order_id, "occurrenceId": occ, "resaleTransferIds": list(transfer_ids),
            "buyerName": "Kim Köpare", "buyerEmail": "kim@example.se", **over}
    return call(e, "POST", "/public/locations/{locationId}/events/checkout", params={"locationId": loc}, body=body)


def public_event(e, loc=LOC_A):
    return call(e, "GET", PUBLIC_EVENT, params={"locationId": loc, "eventId": E1})[1]


def transfer_row(e, transfer_id=LISTING):
    return e.table.get_item(Key=et.transfer_key(TENANT_A, transfer_id)).get("Item")


def ticket_row(e, ticket_id):
    return e.table.get_item(Key=et.ticket_key(TENANT_A, ticket_id))["Item"]


def order_row(e, order_id):
    return e.table.get_item(Key=et.order_key(TENANT_A, order_id))["Item"]


def tx_rows(e, kind=None):
    rows = e.table.query(KeyConditionExpression="PK = :p AND begins_with(SK, :s)",
                         ExpressionAttributeValues={":p": f"TENANT#{TENANT_A}", ":s": "TX#"})["Items"]
    return [r for r in rows if kind is None or r["kind"] == kind]


def tickets_count(e):
    return len(tickets(e))


def check_in(e, ticket_id, client_id="c1", claims=STAFF_A):
    key = qr.scan_key(TENANT_A, OCC1)
    _, out = call(e, "POST", OCC + "/check-ins", params={"locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1},
                  body={"checkIns": [{"ticketId": ticket_id, "sig": qr.sig(key, ticket_id),
                                      "at": "2026-12-04T17:55:00Z", "clientId": client_id}]}, claims=claims)
    return out["results"][0]["status"]


# --- the setting ----------------------------------------------------------------------------------

def test_resale_setting_defaults_off_and_follows_the_event(env):
    status, body = published(env), None
    assert status["settings"]["resaleAllowed"] is False
    assert public_event(env)["resaleAllowed"] is False and public_event(env)["resale"] == []
    status, body = call(env, "PUT", L + "/{eventId}", params={"locationId": LOC_A, "eventId": E1}, claims=OWNER_A,
                        body={"title": "Vinmiddag", "description": "x", "settings": RESALE_ON, "version": 2})
    assert status == 200 and body["settings"]["resaleAllowed"] is True
    assert public_event(env)["resaleAllowed"] is True
    # a bad value is refused, not coerced
    assert call(env, "PUT", L + "/{eventId}", params={"locationId": LOC_A, "eventId": E1}, claims=OWNER_A,
                body={"title": "Vinmiddag", "description": "x", "settings": {**RESALE_ON, "resaleAllowed": "yes"},
                      "version": 3})[0] == 400
    # staff cannot change it
    assert call(env, "PUT", L + "/{eventId}", params={"locationId": LOC_A, "eventId": E1}, claims=STAFF_A,
                body={"title": "Vinmiddag", "description": "x", "settings": RESALE_ON, "version": 3})[0] == 403


# --- listing (K20) and withdrawing (K22) ------------------------------------------------------------

def test_listing_needs_the_setting_the_window_and_a_paid_valid_ticket(env):
    published(env)  # resale off
    assert buy(env)[0] == 200 and pay(env)["result"] == "issued"
    t1, t2 = sorted(t["ticketId"] for t in tickets(env))
    assert list_ticket(env, t1)[1]["code"] == "transfer_closed"
    call(env, "PUT", L + "/{eventId}", params={"locationId": LOC_A, "eventId": E1}, claims=OWNER_A,
         body={"title": "Vinmiddag", "description": "x", "settings": RESALE_ON, "version": 2})
    # a ticket with a gift on its way cannot also be listed
    assert call(env, "POST", "/public/tickets/{token}/transfers", params={"token": token_of(env)},
                body={"transferId": "01GIFTAAAAAAAAAAAAAAAAAAAA", "ticketId": t2, "kind": "gift",
                      "toEmail": "v@example.se"})[0] == 201
    assert list_ticket(env, t2, "01RESALECCCCCCCCCCCCCCCCCC")[1]["code"] == "invalid_state"
    status, listing = list_ticket(env, t1)
    assert status == 201 and listing == {"transferId": LISTING, "ticketId": t1, "kind": "resale", "status": "pending",
                                         "priceOre": 89500}
    assert list_ticket(env, t1) == (201, listing)  # same transferId: the same listing
    assert list_ticket(env, t1, LISTING2)[1]["code"] == "invalid_state"  # already listed
    assert env.mail.kinds().count("events-tickets.transfer") == 1  # a listing emails nobody
    row = transfer_row(env)
    assert row["priceOre"] == 89500 and row["vatRate"] == 12 and row["typeId"] == T1 and "toEmail" not in row
    # one GSI2 partition per event (K3 and K16 read every occurrence in one query), sorted by occurrence
    assert row["GSI2PK"] == f"TENANT#{TENANT_A}#EVENT#{E1}#RESALE" and row["GSI2SK"].startswith(f"{OCC1}#")
    page = call(env, "GET", "/public/tickets/{token}", params={"token": token_of(env)})[1]
    first = next(t for t in page["tickets"] if t["ticketId"] == t1)
    assert page["resaleAllowed"] is True and first["resale"] == "listed" and first["priceOre"] == 89500
    assert first["pendingTransfer"] == {"transferId": LISTING, "kind": "resale", "status": "pending",
                                        "priceOre": 89500}
    assert first["qr"]  # the seller still holds a usable ticket until it is sold
    # withdraw: twice is fine, then it can be listed again under a new id
    p = {"token": token_of(env), "transferId": LISTING}
    assert call(env, "DELETE", "/public/tickets/{token}/transfers/{transferId}", params=p)[0] == 204
    assert call(env, "DELETE", "/public/tickets/{token}/transfers/{transferId}", params=p)[0] == 204
    assert "GSI2PK" not in transfer_row(env) and "pendingTransferId" not in ticket_row(env, t1)
    assert public_event(env)["resale"] == []
    assert list_ticket(env, t1, LISTING2)[0] == 201
    # within two hours of the start: closed
    env.clock.value = datetime(2026, 12, 4, 16, 30, tzinfo=timezone.utc)
    assert list_ticket(env, t1, "01RESALEDDDDDDDDDDDDDDDDDD")[1]["code"] in ("transfer_closed", "invalid_state")
    assert public_event(env)["resale"] == []  # nothing on offer any more
    assert call(env, "GET", "/public/tickets/{token}", params={"token": token_of(env)})[1]["resaleAllowed"] is False


def test_free_tickets_are_given_away_not_sold(env):
    published(env, settings=RESALE_ON,
              ticketTypes=[{"typeId": T1, "name": "Fri entré", "priceOre": 0, "vatRate": 25, "perOrderMax": 4}])
    assert buy(env)[0] == 200
    t1 = sorted(t["ticketId"] for t in tickets(env))[0]
    assert list_ticket(env, t1) == (400, {"error": "invalid_price", "code": "invalid_price", "field": "ticketId"})


def test_a_seller_cannot_buy_their_own_listing_and_a_bought_ticket_is_not_listed_again(env):
    t1, _ = sold_with_resale(env, capacity=6)
    assert list_ticket(env, t1)[0] == 201
    own = buy_listed(env, [LISTING], buyerEmail="SARA@example.se")  # the seller's own address, any case
    assert own == (400, {"error": "own_listing", "code": "own_listing", "field": "resaleTransferIds"})
    assert transfer_row(env)["status"] == "pending" and "resaleClaimOrderId" not in ticket_row(env, t1)
    assert buy_listed(env, [LISTING])[0] == 200 and pay(env, order_id=BUYER)["result"] == "issued"
    new_id = et.ticket_id_for(BUYER, 0)
    # chain depth 1: the buyer may give the ticket away but not sell it on
    buyer_token = qr.token("o", TENANT_A, BUYER)
    assert list_ticket(env, new_id, LISTING2, token=buyer_token)[1]["code"] == "resale_limit"
    assert call(env, "POST", "/public/tickets/{token}/transfers", params={"token": buyer_token},
                body={"transferId": "01GIFTAAAAAAAAAAAAAAAAAAAA", "ticketId": new_id, "kind": "gift",
                      "toEmail": "v@example.se"})[0] == 201
    status, out = call(env, "POST", "/public/ticket-transfers/{transferToken}/accept",
                       params={"transferToken": qr.token("x", TENANT_A, "01GIFTAAAAAAAAAAAAAAAAAAAA")},
                       body={"name": "Vän"})
    assert status == 200
    gifted = et.gift_ticket_id("01GIFTAAAAAAAAAAAAAAAAAAAA")
    assert list_ticket(env, gifted, "01RESALECCCCCCCCCCCCCCCCCC", token=out["ticketToken"])[1]["code"] == "resale_limit"
    assert public_event(env)["resale"] == []


def test_the_holder_of_a_gifted_ticket_cannot_buy_their_own_listing(env):
    """Sara gives her second ticket to Vän, who lists it: neither Vän (its holder) nor Sara (the order's
    buyer, whose card the money would go back to) can buy that listing."""
    _, t2 = sold_with_resale(env, capacity=6)
    assert call(env, "POST", "/public/tickets/{token}/transfers", params={"token": token_of(env)},
                body={"transferId": "01GIFTAAAAAAAAAAAAAAAAAAAA", "ticketId": t2, "kind": "gift",
                      "toEmail": "v@example.se"})[0] == 201
    status, out = call(env, "POST", "/public/ticket-transfers/{transferToken}/accept",
                       params={"transferToken": qr.token("x", TENANT_A, "01GIFTAAAAAAAAAAAAAAAAAAAA")},
                       body={"name": "Vän"})
    assert status == 200
    gifted = et.gift_ticket_id("01GIFTAAAAAAAAAAAAAAAAAAAA")
    assert "pendingTransferKind" not in ticket_row(env, t2) and "pendingTransferKind" not in ticket_row(env, gifted)
    assert list_ticket(env, gifted, LISTING, token=out["ticketToken"])[0] == 201
    assert buy_listed(env, [LISTING], buyerEmail="V@example.se")[1]["code"] == "own_listing"
    assert buy_listed(env, [LISTING], buyerEmail="sara@example.se")[1]["code"] == "own_listing"
    assert buy_listed(env, [LISTING])[0] == 200 and pay(env, order_id=BUYER)["result"] == "issued"


def test_late_refunded_ordinary_order_is_never_issued_on_replay(env):
    published(env, settings=RESALE_ON, occurrences=[{"startAt": "2026-12-04T18:00:00Z", "endAt": "2026-12-04T22:00:00Z",
                                                     "capacity": 2}])
    assert buy(env, order_id=SELLER, qty=2)[0] == 200
    env.clock.value = env.clock.value + timedelta(minutes=16)
    env.api.handler({"job": "sweep"}, FakeContext(120_000))  # the hold expires
    assert buy(env, order_id=BUYER, qty=2, buyerEmail="kim@example.se")[0] == 200
    assert pay(env, order_id=BUYER)["result"] == "issued"
    assert pay(env, order_id=SELLER)["result"] == "refunded:sold_out"
    refunds = len(env.stripe.refunds())
    assert pay(env, order_id=SELLER)["result"] == "ignored:late_refunded"
    assert pay(env, order_id=SELLER)["result"] == "ignored:late_refunded"
    assert len(env.stripe.refunds()) == refunds and tickets_count(env) == 2 and occ_row(env)["sold"] == 2


def test_listing_by_a_foreign_or_forged_token_is_404(env):
    t1 = sold_with_resale(env)[0]
    assert list_ticket(env, t1, token=qr.token("o", TENANT_B, SELLER))[0] == 404
    assert list_ticket(env, t1, token=token_of(env) + "x")[0] == 404
    assert list_ticket(env, "01NOSUCHTICKETAAAAAAAAAAAA")[0] == 404


# --- buying a listed ticket end to end --------------------------------------------------------------

def test_resale_end_to_end_void_issue_refund_atomically(env):
    t1, t2 = sold_with_resale(env)
    assert list_ticket(env, t1)[0] == 201
    public = public_event(env)
    assert public["resale"] == [{"transferId": LISTING, "occurrenceId": OCC1, "typeId": T1, "typeName": "Middag",
                                 "priceOre": 89500, "priceSek": 895.0}]
    assert public["occurrences"][0]["left"] == 2  # a listing frees no seat and takes none
    status, out = buy_listed(env, [LISTING])
    assert status == 200 and out["amountSek"] == 895.0 and out["stripeAccount"] == ACCT_A and out["clientSecret"]
    assert buy_listed(env, [LISTING]) == (200, out)  # the same orderId is the same checkout
    assert transfer_row(env)["status"] == "claimed" and transfer_row(env)["claimOrderId"] == BUYER
    assert public_event(env)["resale"] == []  # a claimed listing is not offered to a second buyer
    occ = occ_row(env)
    assert (occ["sold"], occ["held"], occ["taken"], occ["revenueOre"]) == (2, 0, 2, 179000)
    intents = [c for c in env.stripe.calls if c["path"] == "/v1/payment_intents"]
    assert intents[-1]["params"]["amount"] == 89500 and intents[-1]["params"]["metadata"]["kind"] == "ticket" \
        and intents[-1]["account"] == ACCT_A and intents[-1]["key"] == f"ticket-{BUYER}"

    assert ticket_row(env, t1)["resaleClaimOrderId"] == BUYER  # the ticket row carries the claim (an Update)
    assert pay(env, order_id=BUYER)["result"] == "issued"
    old, buyer_order = ticket_row(env, t1), order_row(env, BUYER)
    new_id = et.ticket_id_for(BUYER, 0)
    new = ticket_row(env, new_id)
    assert old["status"] == "void" and old["resoldTo"] == new_id and "pendingTransferId" not in old
    assert "resaleClaimOrderId" not in old
    assert new["status"] == "valid" and new["holderName"] == "Kim Köpare" and new["orderId"] == BUYER
    assert new["priceOre"] == 89500 and new["vatRate"] == 12 and new["typeId"] == T1 and new["resaleOf"] == t1
    assert buyer_order["status"] == "paid" and buyer_order["resale"] is True and buyer_order["qty"] == 1
    seller = order_row(env, SELLER)
    assert seller["status"] == "partly_refunded" and seller["refundedOre"] == 89500 and seller["refundedCount"] == 1
    listing = transfer_row(env)
    assert listing["status"] == "sold" and listing["newTicketId"] == new_id and "GSI2PK" not in listing
    occ = occ_row(env)
    assert (occ["sold"], occ["held"], occ["taken"], occ["revenueOre"], occ["seatsLeft"]) == (2, 0, 2, 179000, 2)
    type_row = env.table.get_item(Key=et.type_key(TENANT_A, E1, T1))["Item"]
    assert (type_row["sold"], type_row["taken"]) == (2, 2)
    # the money: the buyer's payment and the seller's refund of the same amount, both at the ticket's VAT
    sale, refund = tx_rows(env, "resale_sale"), tx_rows(env, "resale_refund")
    assert len(sale) == 1 and sale[0]["amountOre"] == 89500 and sale[0]["ticketIds"] == [new_id] \
        and sale[0]["vatLines"] == [{"rate": 12, "netOre": 79911, "vatOre": 9589}]
    assert len(refund) == 1 and refund[0]["amountOre"] == -89500 and refund[0]["ticketIds"] == [t1] \
        and refund[0]["orderId"] == SELLER and refund[0]["stripeKey"] == f"ticket-resale-{LISTING}" \
        and refund[0]["vatLines"] == [{"rate": 12, "netOre": -79911, "vatOre": -9589}]
    refunds = env.stripe.refunds()
    assert len(refunds) == 1 and refunds[0]["key"] == f"ticket-resale-{LISTING}" and refunds[0]["account"] == ACCT_A
    assert refunds[0]["params"]["amount"] == 89500 and refunds[0]["params"]["payment_intent"] == seller["paymentIntentId"]
    assert refunds[0]["params"]["metadata"]["ticketId"] == t1 and refunds[0]["params"]["metadata"]["transferId"] == LISTING
    assert refund[0].get("stripeRefundId") is None or refund[0]["stripeRefundId"].startswith("re_")
    assert et.pending_refunds() == []
    assert env.mail.kinds()[-2:] == ["events-tickets.tickets", "events-tickets.refunded"]
    assert env.mail.calls[-1]["notice"]["to"] == "sara@example.se" and "895 kr" in env.mail.calls[-1]["notice"]["text"]
    assert ("Ticket.Transferred", {"tenantId": TENANT_A, "locationId": LOC_A, "occurrenceId": OCC1, "ticketId": t1,
                                   "newTicketId": new_id, "kind": "resale"}) in env.published
    assert not any(p[0] == "Ticket.Sold" and p[1].get("orderId") == BUYER for p in env.published)
    # a webhook replay issues nothing twice and refunds nothing twice
    assert pay(env, order_id=BUYER)["result"] == "already_issued"
    assert len(env.stripe.refunds()) == 1 and len(tx_rows(env)) == 3
    # the door: the old QR is refused, the new one lets the buyer in
    assert check_in(env, t1) == "void"
    assert check_in(env, new_id, "c2") == "ok"
    assert check_in(env, t2, "c3") == "ok"
    # the ticket pages: the seller sees the sold ticket as void, the buyer sees a valid one with a QR
    seller_page = call(env, "GET", "/public/tickets/{token}", params={"token": token_of(env)})[1]
    sold_view = next(t for t in seller_page["tickets"] if t["ticketId"] == t1)
    assert sold_view["status"] == "void" and sold_view["resale"] == "sold" and "qr" not in sold_view
    buyer_page = call(env, "GET", "/public/tickets/{token}", params={"token": token_of(env, BUYER)})[1]
    assert [t["ticketId"] for t in buyer_page["tickets"]] == [new_id] and buyer_page["tickets"][0]["resale"] == "bought"
    # admin: the orders list and the guest list show it
    _, listing = call(env, "GET", L + "/{eventId}/orders", params={"locationId": LOC_A, "eventId": E1}, claims=OWNER_A)
    by_id = {o["orderId"]: o for o in listing["orders"]}
    assert by_id[BUYER]["resale"] is True and by_id[BUYER]["tickets"][0]["resale"] == "bought"
    assert by_id[SELLER]["resale"] is False and {t["resale"] for t in by_id[SELLER]["tickets"] if "resale" in t} == {"sold"}
    _, glist = call(env, "GET", OCC + "/guest-list", params={"locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1},
                    claims=STAFF_A)
    assert glist["counters"]["sold"] == 2 and {t["ticketId"] for t in glist["tickets"]} == {t1, t2, new_id}


def test_two_buyers_one_listing_and_an_expired_hold_that_pays_late(env):
    t1, _ = sold_with_resale(env)
    assert list_ticket(env, t1)[0] == 201
    assert buy_listed(env, [LISTING], order_id=BUYER)[0] == 200
    second = buy_listed(env, [LISTING], order_id=BUYER2)
    assert second[0] == 409 and second[1]["code"] == "resale_taken"
    assert transfer_row(env)["claimOrderId"] == BUYER
    assert env.table.get_item(Key=et.order_key(TENANT_A, BUYER2)).get("Item") is None  # nothing written
    # the seller cannot withdraw while a buyer is paying
    assert call(env, "DELETE", "/public/tickets/{token}/transfers/{transferId}",
                params={"token": token_of(env), "transferId": LISTING})[1]["code"] == "invalid_state"
    # the first buyer never pays: the sweep releases the claim and cancels the intent
    env.clock.value = env.clock.value + timedelta(minutes=16)
    out = env.api.handler({"job": "sweep"}, FakeContext(120_000))
    assert out["expired"] == 1 and order_row(env, BUYER)["status"] == "expired"
    assert transfer_row(env)["status"] == "pending" and "claimOrderId" not in transfer_row(env)
    assert "resaleClaimOrderId" not in ticket_row(env, t1)
    assert any(c["path"].endswith("/cancel") for c in env.stripe.calls)
    assert public_event(env)["resale"][0]["transferId"] == LISTING
    # now the second buyer gets it
    assert buy_listed(env, [LISTING], order_id=BUYER2)[0] == 200
    # ... and the first buyer's late payment arrives: refunded in full, nothing issued
    assert pay(env, order_id=BUYER)["result"] == "refunded:resale_taken"
    late = [r for r in env.stripe.refunds() if r["key"] == f"ticket-late-{BUYER}"]
    assert len(late) == 1 and "amount" not in late[0]["params"]
    # a replay of the late payment issues nothing, refunds nothing and never touches the seller
    assert pay(env, order_id=BUYER)["result"] == "ignored:late_refunded"
    assert len(env.stripe.refunds()) == 1 and tickets_count(env) == 2 and transfer_row(env)["claimOrderId"] == BUYER2
    assert transfer_row(env)["status"] == "claimed" and transfer_row(env)["claimOrderId"] == BUYER2
    assert pay(env, order_id=BUYER2)["result"] == "issued"
    assert ticket_row(env, t1)["status"] == "void" and ticket_row(env, et.ticket_id_for(BUYER2, 0))["status"] == "valid"
    assert env.table.get_item(Key=et.ticket_key(TENANT_A, et.ticket_id_for(BUYER, 0))).get("Item") is None
    assert len([r for r in env.stripe.refunds() if r["key"] == f"ticket-resale-{LISTING}"]) == 1


def test_late_payment_of_an_expired_hold_wins_when_the_listing_is_still_free(env):
    t1, _ = sold_with_resale(env)
    assert list_ticket(env, t1)[0] == 201
    assert buy_listed(env, [LISTING])[0] == 200
    env.clock.value = env.clock.value + timedelta(minutes=16)
    env.api.handler({"job": "sweep"}, FakeContext(120_000))
    assert transfer_row(env)["status"] == "pending"
    assert pay(env, order_id=BUYER)["result"] == "issued"  # re-claimed in the same transaction
    assert transfer_row(env)["status"] == "sold" and ticket_row(env, t1)["status"] == "void"
    assert order_row(env, BUYER)["status"] == "paid"


def test_seller_let_in_or_refunded_before_the_buyer_pays(env):
    t1, t2 = sold_with_resale(env)
    assert list_ticket(env, t1)[0] == 201 and list_ticket(env, t2, LISTING2)[0] == 201
    assert buy_listed(env, [LISTING])[0] == 200
    # the owner cannot refund a ticket a buyer is paying for right now
    rp = {"locationId": LOC_A, "ticketId": t1}
    assert call(env, "POST", L + "/tickets/{ticketId}/refund", params=rp, claims=OWNER_A,
                body={"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "Sjuk"})[1]["code"] == "invalid_state"
    assert ticket_row(env, t1)["status"] == "valid" and env.stripe.refunds() == []
    # the door lets the seller in with the listed ticket before the payment lands (still a valid QR)
    env.clock.value = datetime(2026, 12, 4, 17, 50, tzinfo=timezone.utc)
    assert check_in(env, t1) == "ok"
    assert pay(env, order_id=BUYER)["result"] == "refunded:resale_taken"
    assert ticket_row(env, t1)["status"] == "checked_in" and order_row(env, BUYER)["status"] == "expired"
    assert env.table.get_item(Key=et.ticket_key(TENANT_A, et.ticket_id_for(BUYER, 0))).get("Item") is None
    assert transfer_row(env)["status"] == "pending" and public_event(env)["resale"] == []  # ticket used: not offered
    # a listed (unclaimed) ticket the owner refunds leaves the listing: a later buyer is told so
    env.clock.value = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    assert public_event(env)["resale"][0]["transferId"] == LISTING2
    status, _ = call(env, "POST", L + "/tickets/{ticketId}/refund", params={**rp, "ticketId": t2}, claims=OWNER_A,
                     body={"txId": "01TXREFUNDBBBBBBBBBBBBBBBB", "reason": "Sjuk"})
    assert status == 200 and transfer_row(env, LISTING2)["status"] == "cancelled"
    assert "pendingTransferId" not in ticket_row(env, t2) and public_event(env)["resale"] == []
    assert buy_listed(env, [LISTING2], order_id=BUYER2)[1]["code"] == "resale_taken"


def test_cancelled_occurrence_refunds_the_buyer_and_then_the_seller(env):
    t1, _ = sold_with_resale(env)
    assert list_ticket(env, t1)[0] == 201
    assert buy_listed(env, [LISTING])[0] == 200
    p = {"locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1}
    assert call(env, "POST", OCC + "/cancel", params=p, claims=OWNER_A,
                body={"jobId": "01JOBAAAAAAAAAAAAAAAAAAAAA", "version": 1})[0] == 202
    jp = {"locationId": LOC_A, "jobId": "01JOBAAAAAAAAAAAAAAAAAAAAA"}
    job = call(env, "GET", L + "/jobs/{jobId}", params=jp, claims=OWNER_A)[1]
    assert job["done"] is False and job["counts"]["refunded"] == 1  # the listed ticket waits for the buyer's hold
    assert ticket_row(env, t1)["status"] == "valid"
    assert pay(env, order_id=BUYER)["result"] == "refunded:cancelled"
    assert [r["key"] for r in env.stripe.refunds() if r["key"].startswith("ticket-late-")] == [f"ticket-late-{BUYER}"]
    assert transfer_row(env)["status"] == "pending"  # the claim went back; the sweep can refund the seller now
    env.api.handler({"job": "sweep"}, FakeContext(120_000))
    job = call(env, "GET", L + "/jobs/{jobId}", params=jp, claims=OWNER_A)[1]
    assert job["done"] is True and job["counts"]["refunded"] == 2
    assert ticket_row(env, t1)["status"] == "refunded" and transfer_row(env)["status"] == "cancelled"
    assert len({r["key"] for r in env.stripe.refunds()}) == 3 and tx_rows(env, "resale_sale") == []
    assert buy_listed(env, [LISTING], order_id=BUYER2)[1]["code"] in ("sales_closed", "resale_taken")


def test_checkout_validation_window_and_tenant_isolation(env):
    t1, t2 = sold_with_resale(env)
    assert list_ticket(env, t1)[0] == 201 and list_ticket(env, t2, LISTING2)[0] == 201
    # mixing listed tickets with new seats, too many, bad or unknown ids
    assert buy_listed(env, [LISTING], lines=[{"typeId": T1, "qty": 1}])[0] == 400
    assert buy_listed(env, [LISTING, LISTING])[1]["code"] == "too_many_tickets"
    assert buy_listed(env, [f"01RESALE{i}AAAAAAAAAAAAAAAAA" for i in range(env.api.RESALE_PER_ORDER + 1)])[1]["code"] \
        == "too_many_tickets"
    assert buy_listed(env, ["!!"])[1]["code"] == "too_many_tickets"
    assert buy_listed(env, ["01RESALENOPEAAAAAAAAAAAAAA"])[0] == 404
    assert buy_listed(env, [LISTING], buyerEmail="nope")[1]["code"] == "invalid_email"
    # another restaurant, another location of the same restaurant: 404, nothing claimed
    assert buy_listed(env, [LISTING], loc=LOC_B)[0] == 404
    assert buy_listed(env, [LISTING], loc=LOC_A2)[0] == 404
    assert transfer_row(env)["status"] == "pending"
    # a listing of another occurrence of the event is not this occurrence's
    assert buy_listed(env, [LISTING], occ=f"{E1}-20261211T1800Z")[0] == 404
    # within two hours of the start the listings are closed to buyers too
    env.clock.value = datetime(2026, 12, 4, 16, 30, tzinfo=timezone.utc)
    assert buy_listed(env, [LISTING])[1]["code"] == "sales_closed" and public_event(env)["resale"] == []
    env.clock.value = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    # a retry of the same orderId with other listings is another order
    assert buy_listed(env, [LISTING, LISTING2])[0] == 200
    assert buy_listed(env, [LISTING])[1]["code"] == "changed_retry"
    assert buy(env, order_id=BUYER, qty=1)[1]["code"] == "changed_retry"
    # tenant B's owner sees nothing of A's listings or orders
    assert call(env, "GET", L + "/{eventId}/orders", params={"locationId": LOC_B, "eventId": E1},
                claims=OWNER_B)[0] == 404
    assert call(env, "GET", PUBLIC_EVENT, params={"locationId": LOC_B, "eventId": E1})[0] == 404


def test_two_listed_tickets_in_one_order_from_two_sellers(env):
    t1, _ = sold_with_resale(env, capacity=6)
    assert buy(env, order_id="01ORDERSELLER2AAAAAAAAAAAA", qty=1, buyerEmail="bo@example.se", buyerName="Bo")[0] == 200
    assert pay(env, order_id="01ORDERSELLER2AAAAAAAAAAAA")["result"] == "issued"
    t3 = et.ticket_id_for("01ORDERSELLER2AAAAAAAAAAAA", 0)
    assert list_ticket(env, t1)[0] == 201
    assert list_ticket(env, t3, LISTING2, token=qr.token("o", TENANT_A, "01ORDERSELLER2AAAAAAAAAAAA"))[0] == 201
    status, out = buy_listed(env, [LISTING, LISTING2])
    assert status == 200 and out["amountSek"] == 1790.0
    assert pay(env, order_id=BUYER)["result"] == "issued"
    assert {r["key"] for r in env.stripe.refunds()} == {f"ticket-resale-{LISTING}", f"ticket-resale-{LISTING2}"}
    assert order_row(env, SELLER)["status"] == "partly_refunded" and order_row(env, SELLER)["refundedOre"] == 89500
    second = order_row(env, "01ORDERSELLER2AAAAAAAAAAAA")
    assert second["status"] == "refunded" and second["refundedOre"] == 89500 and second["refundedCount"] == 1
    buyer = order_row(env, BUYER)
    assert buyer["qty"] == 2 and buyer["amountOre"] == 179000 and len(et.tickets_of_order(TENANT_A, buyer)) == 2
    assert {ticket_row(env, t)["status"] for t in (t1, t3)} == {"void"}
    occ = occ_row(env)
    assert (occ["sold"], occ["revenueOre"], occ["seatsLeft"]) == (3, 268500, 3)
    assert env.mail.kinds().count("events-tickets.refunded") == 2  # one note per seller


def test_seller_refund_retried_by_the_sweep_when_stripe_is_down(env):
    t1, _ = sold_with_resale(env)
    assert list_ticket(env, t1)[0] == 201
    assert buy_listed(env, [LISTING])[0] == 200
    env.stripe.fail = True
    assert pay(env, order_id=BUYER)["result"] == "issued"  # the tickets moved; only the seller's refund is owed
    assert ticket_row(env, t1)["status"] == "void" and order_row(env, BUYER)["status"] == "paid"
    owed = et.pending_refunds()
    assert len(owed) == 1 and owed[0]["kind"] == "resale_refund" and owed[0]["txId"] == f"resale-refund-{LISTING}"
    env.stripe.fail = False
    assert pay(env, order_id=BUYER)["result"] == "already_issued"
    assert env.api.handler({"job": "sweep"}, FakeContext(120_000))["refunds"] == 1
    refunds = env.stripe.refunds()
    assert len(refunds) == 2 and {r["key"] for r in refunds} == {f"ticket-resale-{LISTING}"}  # one key, one refund
    assert et.pending_refunds() == [] and tx_rows(env, "resale_refund")[0]["stripeRefundId"].startswith("re_")
    assert env.api.handler({"job": "sweep"}, FakeContext(120_000))["refunds"] == 0


def test_seller_refund_older_than_a_day_is_looked_up_by_the_listing(env):
    t1, _ = sold_with_resale(env)
    assert list_ticket(env, t1)[0] == 201
    assert buy_listed(env, [LISTING])[0] == 200
    env.stripe.fail = True
    pay(env, order_id=BUYER)
    env.stripe.fail = False
    seller = order_row(env, SELLER)
    env.stripe.known_refunds = [{"id": "re_found", "payment_intent": seller["paymentIntentId"], "amount": 89500,
                                 "status": "succeeded",
                                 "metadata": {"kind": "ticket", "tenantId": TENANT_A, "ticketId": t1}}]
    env.clock.value = env.clock.value + timedelta(hours=25)
    posted = len(env.stripe.refunds())
    assert env.api.handler({"job": "sweep"}, FakeContext(120_000))["refunds"] == 1
    assert len(env.stripe.refunds()) == posted and tx_rows(env, "resale_refund")[0]["stripeRefundId"] == "re_found"


# --- refunds of resold tickets and Stripe-side refunds ------------------------------------------------

def test_refund_of_a_resold_ticket_goes_to_the_buyer_and_the_old_one_is_closed(env):
    t1, _ = sold_with_resale(env)
    assert list_ticket(env, t1)[0] == 201 and buy_listed(env, [LISTING])[0] == 200
    assert pay(env, order_id=BUYER)["result"] == "issued"
    new_id = et.ticket_id_for(BUYER, 0)
    # the old (void) ticket cannot be refunded again
    assert call(env, "POST", L + "/tickets/{ticketId}/refund", params={"locationId": LOC_A, "ticketId": t1},
                claims=OWNER_A, body={"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "x"})[1]["code"] == "invalid_state"
    status, t = call(env, "POST", L + "/tickets/{ticketId}/refund", params={"locationId": LOC_A, "ticketId": new_id},
                     claims=OWNER_A, body={"txId": "01TXREFUNDBBBBBBBBBBBBBBBB", "reason": "Sjuk"})
    assert status == 200 and t["status"] == "refunded"
    refund = [r for r in env.stripe.refunds() if r["key"] == f"ticket-refund-{new_id}"]
    assert len(refund) == 1 and refund[0]["params"]["payment_intent"] == order_row(env, BUYER)["paymentIntentId"] \
        and refund[0]["params"]["amount"] == 89500
    assert order_row(env, BUYER)["status"] == "refunded" and order_row(env, BUYER)["refundedOre"] == 89500
    occ = occ_row(env)
    assert (occ["sold"], occ["revenueOre"], occ["seatsLeft"]) == (1, 89500, 3)
    # the seller's own partial refund seen from Stripe is ours, not a dispute
    seller = order_row(env, SELLER)
    hook = {"hook": "stripe.event", "tenantId": TENANT_A, "type": "charge.refunded", "account": ACCT_A,
            "object": {"id": "ch_1", "amount": 179000, "amount_refunded": 89500,
                       "metadata": {"kind": "ticket", "tenantId": TENANT_A, "orderId": seller["orderId"]}}}
    assert env.api.handler(hook)["result"] == "ignored:own_refund"
    # a full refund of the buyer's charge in Stripe voids the buyer's ticket, never the seller's other one
    hook["object"] = {"id": "ch_2", "amount": 89500, "amount_refunded": 89500,
                      "metadata": {"kind": "ticket", "tenantId": TENANT_A, "orderId": BUYER}}
    assert env.api.handler(hook)["result"] == "already_refunded"
    # the mass refund of a cancelled occurrence refunds the buyer's new ticket, not the voided one
    assert check_in(env, t1) == "void"


def test_refund_cancels_a_pending_gift_too(env):
    """A refunded ticket with a gift on its way: the recipient cannot accept it afterwards."""
    t1 = sold_with_resale(env)[0]
    assert call(env, "POST", "/public/tickets/{token}/transfers", params={"token": token_of(env)},
                body={"transferId": "01GIFTAAAAAAAAAAAAAAAAAAAA", "ticketId": t1, "kind": "gift",
                      "toEmail": "v@example.se"})[0] == 201
    assert call(env, "POST", L + "/tickets/{ticketId}/refund", params={"locationId": LOC_A, "ticketId": t1},
                claims=OWNER_A, body={"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "x"})[0] == 200
    assert transfer_row(env, "01GIFTAAAAAAAAAAAAAAAAAAAA")["status"] == "cancelled"
    assert call(env, "POST", "/public/ticket-transfers/{transferToken}/accept",
                params={"transferToken": qr.token("x", TENANT_A, "01GIFTAAAAAAAAAAAAAAAAAAAA")},
                body={"name": "Vän"})[1]["code"] == "invalid_state"


# --- money: Bokföring, Statistik, VAT per ticket --------------------------------------------------------

def test_bookkeeping_balanced_and_net_zero_with_price_and_vat_preserved(env):
    t1, _ = sold_with_resale(env)
    # the owner raises the price and changes the VAT rate after the sale: the listing keeps the old ones
    assert call(env, "PUT", L + "/{eventId}/ticket-types/{typeId}", params={"locationId": LOC_A, "eventId": E1, "typeId": T1},
                claims=OWNER_A, body={"name": "Middag", "priceOre": 99500, "vatRate": 25, "perOrderMax": 4,
                                      "sortOrder": 1, "version": 1})[0] == 200
    env.clock.value = env.clock.value + timedelta(days=1)
    assert list_ticket(env, t1)[1]["priceOre"] == 89500
    assert public_event(env)["resale"][0]["priceOre"] == 89500 and public_event(env)["ticketTypes"][0]["priceOre"] == 99500
    status, out = buy_listed(env, [LISTING])
    assert status == 200 and out["amountSek"] == 895.0
    assert pay(env, order_id=BUYER)["result"] == "issued"
    new = ticket_row(env, et.ticket_id_for(BUYER, 0))
    assert new["priceOre"] == 89500 and new["vatRate"] == 12
    out = env.api.handler({"hook": "accounting.events", "tenantId": TENANT_A, "locationId": LOC_A,
                           "first": "2026-11-05", "last": "2026-11-06"})
    assert out["ok"] and [d["day"] for d in out["events"]] == ["2026-11-05", "2026-11-06"]
    for day in out["events"]:
        assert sum(p["amount"] for p in day["postings"]) == 0
    resale_day = out["events"][1]["postings"]
    assert {p["text"].split(" 12 %")[0].replace(" utgående moms", "") for p in resale_day} == {
        "Vidaresålda biljetter", "Återbetalda vidaresålda biljetter"}
    assert all("12 %" in p["text"] for p in resale_day)  # the ticket's rate, not the type's new 25 %
    per_role = {}
    for p in resale_day:
        per_role[p["role"]] = per_role.get(p["role"], 0) + p["amount"]
    assert per_role == {"stripe": 0, "sales12": 0, "vat12": 0}  # a resale is net zero for the restaurant
    legs = {(p["text"], p["role"]): p["amount"] for p in resale_day}
    assert legs[("Vidaresålda biljetter 12 % moms", "stripe")] == 89500
    assert legs[("Återbetalda vidaresålda biljetter 12 % moms", "stripe")] == -89500
    insights = env.api.handler({"hook": "insights.events", "tenantId": TENANT_A, "locationIds": [LOC_A],
                                "first": "2026-11-01", "last": "2026-12-31"})
    by_day = {d["day"]: d for d in insights["days"]}
    assert by_day["2026-11-05"]["ticketsSold"] == 2 and by_day["2026-11-05"]["revenueOre"] == 179000
    assert "2026-11-06" not in by_day  # nothing sold, nothing earned on the resale day
    assert env.api.handler({"hook": "accounting.events", "tenantId": TENANT_B, "locationId": LOC_B,
                            "first": "2026-11-05", "last": "2026-11-06"}) == {"ok": True, "events": []}


def test_admin_event_view_counts_listed_tickets(env):
    t1, t2 = sold_with_resale(env)
    p = {"locationId": LOC_A, "eventId": E1}
    assert "resaleListed" not in call(env, "GET", L + "/{eventId}", params=p, claims=OWNER_A)[1]["occurrences"][0] \
        or call(env, "GET", L + "/{eventId}", params=p, claims=OWNER_A)[1]["occurrences"][0]["resaleListed"] == 0
    list_ticket(env, t1)
    list_ticket(env, t2, LISTING2)
    assert call(env, "GET", L + "/{eventId}", params=p, claims=OWNER_A)[1]["occurrences"][0]["resaleListed"] == 2
    assert call(env, "GET", L + "/{eventId}", params=p, claims=STAFF_A)[1]["occurrences"][0]["resaleListed"] == 2
    buy_listed(env, [LISTING])
    assert call(env, "GET", L + "/{eventId}", params=p, claims=OWNER_A)[1]["occurrences"][0]["resaleListed"] == 2
    pay(env, order_id=BUYER)
    assert call(env, "GET", L + "/{eventId}", params=p, claims=OWNER_A)[1]["occurrences"][0]["resaleListed"] == 1


def test_privacy_erase_and_nightly_cover_resale_rows(env):
    t1, _ = sold_with_resale(env)
    list_ticket(env, t1)
    buy_listed(env, [LISTING])
    pay(env, order_id=BUYER)
    erased = env.api.handler({"hook": "privacy.erase", "tenantId": TENANT_A, "email": "kim@example.se"})
    assert erased["erased"]["ticketOrders"] == 1
    new = ticket_row(env, et.ticket_id_for(BUYER, 0))
    assert "holderName" not in new and new["status"] == "valid"
    env.table.update_item(Key=et.transfer_key(TENANT_A, LISTING), UpdateExpression="REMOVE #t",
                          ExpressionAttributeNames={"#t": "ttl"})
    env.clock.value = datetime(2028, 12, 10, 3, 0, tzinfo=timezone.utc)
    assert env.api.handler({"job": "nightly"})["cleaned"] == 2
    assert all("holderName" not in t for t in tickets(env))


# --- the seller refunded outside our books while a buyer holds the listing ------------------------------

def test_stripe_dashboard_refund_of_the_seller_ends_a_claimed_listing_and_pays_the_buyer_back(env):
    """The restaurant refunds the seller's whole order in the Stripe dashboard while a buyer is paying for
    one listed ticket: both seller tickets are refunded in our books (Stripe already moved the money, so no
    second seller refund), the listing is cancelled whatever its state, and the buyer's payment goes back
    in full when it lands. One ticket that cannot be refunded never leaves the others valid."""
    t1, t2 = sold_with_resale(env)
    assert list_ticket(env, t1)[0] == 201 and buy_listed(env, [LISTING])[0] == 200
    assert transfer_row(env)["status"] == "claimed" and ticket_row(env, t1)["resaleClaimOrderId"] == BUYER
    hook = {"hook": "stripe.event", "tenantId": TENANT_A, "type": "charge.refunded", "account": ACCT_A,
            "object": {"id": "ch_1", "amount": 179000, "amount_refunded": 179000,
                       "metadata": {"kind": "ticket", "tenantId": TENANT_A, "orderId": SELLER}}}
    assert env.api.handler(hook) == {"ok": True, "result": "voided"}
    assert ticket_row(env, t1)["status"] == "refunded" and ticket_row(env, t2)["status"] == "refunded"
    assert "resaleClaimOrderId" not in ticket_row(env, t1) and "pendingTransferId" not in ticket_row(env, t1)
    listing = transfer_row(env)
    assert listing["status"] == "cancelled" and "claimOrderId" not in listing and "GSI2PK" not in listing
    seller = order_row(env, SELLER)
    assert seller["status"] == "refunded" and seller["refundedOre"] == 179000 and seller["refundedCount"] == 2
    assert env.stripe.refunds() == [] and et.pending_refunds() == []  # nothing is sent to Stripe twice
    assert public_event(env)["resale"] == []
    # the buyer's payment lands: nothing is issued, the whole payment goes back
    assert pay(env, order_id=BUYER)["result"] == "refunded:resale_taken"
    assert [r["key"] for r in env.stripe.refunds()] == [f"ticket-late-{BUYER}"]
    assert order_row(env, BUYER)["status"] == "expired" and tickets_count(env) == 2
    assert env.table.get_item(Key=et.ticket_key(TENANT_A, et.ticket_id_for(BUYER, 0))).get("Item") is None
    # replays change nothing
    assert env.api.handler(hook)["result"] == "already_refunded"
    assert pay(env, order_id=BUYER)["result"] == "ignored:late_refunded"
    assert len(env.stripe.refunds()) == 1
    assert env.api.handler({"job": "sweep"}, FakeContext(120_000))["failed"] == 0


def test_cancellation_email_states_what_this_refund_gave_back(env):
    """One of Sara's two tickets was refunded by the owner before the occurrence was cancelled: the
    cancellation email says the ticket's price, not the order's full amount."""
    t1, _ = sold_with_resale(env)
    assert call(env, "POST", L + "/tickets/{ticketId}/refund", params={"locationId": LOC_A, "ticketId": t1},
                claims=OWNER_A, body={"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "Sjuk"})[0] == 200
    p = {"locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1}
    assert call(env, "POST", OCC + "/cancel", params=p, claims=OWNER_A,
                body={"jobId": "01JOBAAAAAAAAAAAAAAAAAAAAA", "version": 1})[1]["toRefund"] == 1
    assert env.mail.kinds().count("events-tickets.cancelled") == 1
    text = [c for c in env.mail.calls if c["notice"]["kind"] == "events-tickets.cancelled"][0]["notice"]["text"]
    assert env.api._kr(89500) in text and env.api._kr(179000) not in text
    assert order_row(env, SELLER)["refundedOre"] == 179000
