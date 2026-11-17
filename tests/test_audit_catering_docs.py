"""Audit fixes for catering documents and messages: the equipment-return
term and price header in the offer PDF (#27, #56), the cancellation text
per refund outcome (#47), history order (#65) and simple acceptance with a
provider that seals (#48)."""

import json

from shared import catering as c
from shared import catering_messages as m
from shared import signing
from test_catering import LOC_A, body_of, guest, load, env  # noqa: F401 - env fixture
from test_catering_signing import allow_acceptance, sent_offer, webhook

DOC = {"version": 1, "requestId": "r1", "seller": {"name": "Roma AB"}, "customer": {"name": "Anna"},
       "delivery": {"date": "2026-11-20", "time": "12:00", "fulfilment": "pickup"},
       "lines": [{"name": "Buffé", "unitPrice": 10000, "quantity": 2, "vatRate": 12}],
       "totals": {"total": 20000, "vat": 2143}, "validUntil": "2026-11-10"}


def render(**extra):
    return load("catering-document", "cat_doc_audit").render_html({**DOC, **extra}, {"address": "Götgatan 1"})


def test_offer_pdf_states_the_equipment_return_term():
    cleaned = render(equipmentReturn="cleaned", cleaningFee=0)
    assert "Disk och utrustning lämnas tillbaka rengjord." in cleaned
    free = render(equipmentReturn="not_cleaned", cleaningFee=0)
    assert "Disk och utrustning lämnas tillbaka utan rengöring." in free
    paid = render(equipmentReturn="not_cleaned", cleaningFee=50000)
    assert "Disk och utrustning lämnas tillbaka utan rengöring (rengöringsavgift enligt nedan)." in paid
    assert "lämnas tillbaka" not in render()  # older offers read as they did


def test_offer_pdf_price_header_uses_the_grave_accent():
    page = render()
    assert "À-pris" in page and "Á-pris" not in page


ITEM = {"customerName": "Anna Svensson", "deliveryDate": "2026-10-20", "deliveryTime": "12:00", "guests": 40,
        "fulfilment": "delivery", "offer": {"totals": {"total": 1000000}}, "cancelReason": "Sjukdom"}


def cancelled(note):
    item = {**ITEM, **({"refundNote": note} if note else {})}
    return (m.guest_email("catering_cancelled", item, {"name": "Roma"}, {"name": "Södermalm"}, None)[1],
            m.guest_sms("catering_cancelled", item, {"name": "Roma"}, {"name": "Södermalm"}, None))


def test_cancellation_text_follows_the_refund_outcome():
    email, sms = cancelled("refunded")
    assert "Betalningen återbetalas till samma betalsätt." in email and "återbetalas" in sms
    email, sms = cancelled("invoice_voided")
    assert "Fakturan är makulerad och ska inte betalas." in email and "makulerad" in sms
    assert "återbetalas" not in email + sms and "betalas tillbaka" not in email
    email, sms = cancelled("refund_manually")
    assert "Restaurangen betalar tillbaka det du har betalat." in email and "betalar tillbaka" in sms
    assert "återbetalas" not in email + sms
    for note in ("none", None):
        email, sms = cancelled(note)
        assert "återbetalas" not in email + sms and "betalas tillbaka" not in email + sms
        assert "makulerad" not in email + sms and "Se mejlet." in sms


def test_history_is_newest_first_across_webhook_entries(env):  # noqa: F811
    table = c.dynamo.table(c.history_table())
    for sk, at in (("AUDIT#2026-11-01T10:00:00Z#1", "2026-11-01T10:00:00Z"),
                   ("AUDIT#STRIPE#evt_1", "2026-11-01T09:00:00Z"),
                   ("AUDIT#2026-11-01T11:00:00Z#1", "2026-11-01T11:00:00Z"),
                   ("AUDIT#SIGNING#evt_2", "2026-11-01T10:30:00Z")):
        table.put_item(Item={"PK": "REQUEST#rx", "SK": sk, "action": sk, "at": at})
    assert [h["at"] for h in c.history("rx")] == ["2026-11-01T11:00:00Z", "2026-11-01T10:30:00Z",
                                                   "2026-11-01T10:00:00Z", "2026-11-01T09:00:00Z"]


def _scrive_like(env):  # noqa: F811
    """A provider whose document is canceled once the guest accepts: it never seals."""
    def not_ready(order):
        raise signing.EvidenceNotReady("canceled")
    env.monkeypatch.setattr(signing, "fetch_evidence", not_ready)


def test_simple_acceptance_does_not_wait_for_a_provider_seal(env):  # noqa: F811
    allow_acceptance(env)
    _scrive_like(env)
    rid, token = sent_offer(env)
    view = body_of(guest(env, "POST", "/accept", rid, token, {"name": "Anna Svensson", "acceptTerms": True}))
    assert view["status"] == "signed"
    item = c.load(LOC_A, rid)
    assert not item["signing"].get("sealPending")
    order = item["signing"]["order"]
    response = webhook(env, rid, order["callbackToken"], {"eventId": "evt-after-accept"})
    assert response["statusCode"] == 200, response


def test_a_stuck_seal_after_simple_acceptance_clears_on_the_next_callback(env):  # noqa: F811
    allow_acceptance(env)
    rid, token = sent_offer(env)
    guest(env, "POST", "/accept", rid, token, {"name": "Anna Svensson", "acceptTerms": True})
    item = c.load(LOC_A, rid)
    c.dynamo.table(c.requests_table()).update_item(
        Key=c.request_key(LOC_A, rid), UpdateExpression="SET signing.sealPending = :t",
        ExpressionAttributeValues={":t": True})
    _scrive_like(env)
    response = webhook(env, rid, item["signing"]["order"]["callbackToken"], {"eventId": "evt-stuck"})
    assert response["statusCode"] == 200, response
    assert json.loads(response["body"])["result"] == "handled"
    assert not c.load(LOC_A, rid)["signing"].get("sealPending")
