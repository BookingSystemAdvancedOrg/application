"""Staff change a booking's party size (PATCH, mark-arrived): the card
guarantee follows the party (same card, restamped per-person fees), a
booking that grows past the card threshold gets cardNeeded (never PENDING,
never a released table), the guest is told once (fee sentence in sv/en), and
the whole change is one version-guarded write."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from shared import messages
from shared import reservations as shared_r
from shared import tenant as shared_tenant
from test_guarantee import (LOC_A, LOCATION_TABLE, RESERVATION_TABLE, TENANT_A, book, cancel, confirm,  # noqa: F401
                            confirmed_booking, g, staff_status, stored)
from test_reservations import DAY, OWNER_A, body_of, edit, env, free_tables_at, location_item, walk_in  # noqa: F401
from tenant_support import TENANT_TABLE, tenant_row


def set_policy(env, **fields):
    policy = {"enabled": True, "minPartySize": 2, "noShowFeePerPerson": 200,
              "lateCancelFeePerPerson": 100, "cancelCutoffHours": 24, **fields}
    env["res"].Table(LOCATION_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"},
        UpdateExpression="SET guarantee = :g",
        ExpressionAttributeValues={":g": {k: Decimal(v) if isinstance(v, int) and not isinstance(v, bool) else v
                                          for k, v in policy.items()}})
    shared_tenant.reset_caches()


def no_card_booking(env, party=3, tables=("table-b",), **policy):
    """A booking the policy did not ask a card for (party under minPartySize)."""
    set_policy(env, **policy)
    response, body = book(env, party, tables)
    assert response["statusCode"] == 201 and body["status"] == "reserved", body
    return body["reservationId"]


def patch_item(env, rid, **attrs):
    sets = ", ".join(f"{k} = :{k}" for k in attrs)
    env["res"].Table(RESERVATION_TABLE).update_item(
        Key={"PK": f"LOCATION#{LOC_A}", "SK": f"RESERVATION#{DAY}#{rid}"},
        UpdateExpression=f"SET {sets}", ExpressionAttributeValues={f":{k}": v for k, v in attrs.items()})


# --- card on file: the snapshot follows the party ---------------------------------------

def test_increase_with_card_restamps_the_fees_and_keeps_the_card(g):
    rid, _ = confirmed_booking(g, party=3)
    before = stored(g, rid)
    assert before["guarantee"]["noShowFee"] == 60000
    response = edit(g, rid, {"partySize": 4})
    assert response["statusCode"] == 200
    body = body_of(response)
    assert body["partyChange"] == {
        "from": 3, "to": 4, "noShowFee": 80000, "lateCancelFee": 40000,
        "previousNoShowFee": 60000, "previousLateCancelFee": 30000, "currency": "sek",
        "cardOnFile": True, "cardRequired": False, "cardLinkSent": False, "cardWaived": False,
        "notified": True, "cancelCutoffHours": 24}
    after = stored(g, rid)
    assert after["partySize"] == 4
    assert after["guarantee"]["noShowFee"] == 80000 and after["guarantee"]["lateCancelFee"] == 40000
    assert after["guarantee"]["noShowFeePerPerson"] == 200 and after["guarantee"]["cancelCutoffHours"] == 24
    assert after["stripePaymentMethodId"] == before["stripePaymentMethodId"] and after["status"] == "reserved"
    assert "cardNeeded" not in after
    assert "partyChange" not in stored(g, rid)  # response only


def test_decrease_with_card_restamps_down(g):
    rid, _ = confirmed_booking(g, party=3)
    body = body_of(edit(g, rid, {"partySize": 2}))
    assert body["partyChange"]["noShowFee"] == 40000 and body["partyChange"]["previousNoShowFee"] == 60000
    assert stored(g, rid)["guarantee"]["lateCancelFee"] == 20000


def test_decrease_below_the_threshold_keeps_card_and_snapshot(g):
    set_policy(g, minPartySize=3)
    rid, _ = confirmed_booking(g, party=3)
    body = body_of(edit(g, rid, {"partySize": 2}))
    assert body["partyChange"]["cardOnFile"] and not body["partyChange"]["cardRequired"]
    after = stored(g, rid)
    assert after["stripePaymentMethodId"] and after["guarantee"]["noShowFee"] == 40000


def test_restamp_uses_the_stored_amounts_not_the_live_policy(g):
    rid, _ = confirmed_booking(g, party=3)
    set_policy(g, noShowFeePerPerson=999, lateCancelFeePerPerson=999, cancelCutoffHours=2)
    body = body_of(edit(g, rid, {"partySize": 4}))
    assert body["partyChange"]["noShowFee"] == 80000 and body["partyChange"]["cancelCutoffHours"] == 24
    assert stored(g, rid)["guarantee"]["cancelCutoffHours"] == 24


def test_a_snapshot_without_per_person_amounts_is_derived_from_its_totals(g):
    rid, _ = confirmed_booking(g, party=3)
    patch_item(g, rid, guarantee={"currency": "sek", "noShowFee": Decimal(60000),
                                  "lateCancelFee": Decimal(30000), "cancelCutoffHours": Decimal(24)})
    assert body_of(edit(g, rid, {"partySize": 4}))["partyChange"]["noShowFee"] == 80000
    assert stored(g, rid)["guarantee"]["noShowFeePerPerson"] == 200


def test_history_records_party_and_fees_by_actor(g):
    rid, _ = confirmed_booking(g, party=3)
    edit(g, rid, {"partySize": 4})
    entry = stored(g, rid)["history"][-1]
    assert entry["action"] == "edited" and entry["by"] and entry["fields"] == ["partySize"]
    assert (entry["previousPartySize"], entry["partySize"]) == (3, 4)
    assert (entry["previousNoShowFee"], entry["noShowFee"]) == (60000, 80000)
    assert (entry["previousLateCancelFee"], entry["lateCancelFee"]) == (30000, 40000)


def test_a_started_or_finished_charge_refuses_the_edit(g):
    rid, _ = confirmed_booking(g, party=3)
    patch_item(g, rid, payment={"kind": "no_show", "status": "pending", "amount": Decimal(60000),
                                "attempt": Decimal(1)})
    response = edit(g, rid, {"partySize": 4})
    assert response["statusCode"] == 409 and body_of(response)["error"] == "charge_started"
    assert stored(g, rid)["partySize"] == 3 and stored(g, rid)["guarantee"]["noShowFee"] == 60000
    patch_item(g, rid, payment={"kind": "late_cancel", "status": "succeeded", "amount": Decimal(30000),
                                "attempt": Decimal(1)})
    assert body_of(edit(g, rid, {"partySize": 2}))["error"] == "charge_started"


def test_a_charge_starting_during_the_edit_loses_the_race(g, monkeypatch):
    rid, _ = confirmed_booking(g, party=3)
    real = shared_r.transact

    def charge_first(items):
        patch_item(g, rid, payment={"kind": "no_show", "status": "pending", "amount": Decimal(60000),
                                    "attempt": Decimal(1)})
        return real(items)

    monkeypatch.setattr(shared_r, "transact", charge_first)
    response = edit(g, rid, {"partySize": 4})
    assert response["statusCode"] == 409 and body_of(response)["error"] == "changed_retry"
    assert stored(g, rid)["partySize"] == 3 and stored(g, rid)["guarantee"]["noShowFee"] == 60000


def test_a_concurrent_edit_loses_with_changed_retry_and_nothing_is_half_written(g, monkeypatch):
    rid, _ = confirmed_booking(g, party=3)
    real = shared_r.transact

    def other_edit_first(items):
        patch_item(g, rid, version=Decimal(int(stored(g, rid).get("version", 0)) + 1))
        return real(items)

    monkeypatch.setattr(shared_r, "transact", other_edit_first)
    response = edit(g, rid, {"partySize": 4})
    assert response["statusCode"] == 409 and body_of(response)["error"] == "changed_retry"
    after = stored(g, rid)
    assert after["partySize"] == 3 and after["guarantee"]["noShowFee"] == 60000


def test_a_repeated_identical_patch_is_a_no_op(g):
    rid, _ = confirmed_booking(g, party=3)
    first = body_of(edit(g, rid, {"partySize": 4}))
    snapshot = stored(g, rid)
    again = edit(g, rid, {"partySize": 4})
    assert again["statusCode"] == 200 and "partyChange" not in body_of(again)
    after = stored(g, rid)
    assert after["version"] == snapshot["version"] and after["notice"]["id"] == snapshot["notice"]["id"]
    assert len(after["history"]) == len(snapshot["history"]) and first["partyChange"]["notified"]


# --- notice ---------------------------------------------------------------------------------

def test_a_party_change_sets_the_changed_notice(g):
    rid, _ = confirmed_booking(g, party=3)
    old = stored(g, rid)["notice"]["id"]
    edit(g, rid, {"partySize": 4})
    notice = stored(g, rid)["notice"]
    assert notice["type"] == "changed" and notice["id"] != old


def test_notify_guest_false_sets_no_notice(g):
    rid, _ = confirmed_booking(g, party=3)
    body = body_of(edit(g, rid, {"partySize": 4, "notifyGuest": False}))
    assert body["partyChange"]["notified"] is False
    assert "notice" not in stored(g, rid)


def test_a_guest_without_contact_details_is_not_notified(g):
    rid = no_card_booking(g, party=2, minPartySize=9)
    g["res"].Table(RESERVATION_TABLE).update_item(
        Key={"PK": f"LOCATION#{LOC_A}", "SK": f"RESERVATION#{DAY}#{rid}"},
        UpdateExpression="REMOVE customerEmail, customerPhone, notice")
    body = body_of(edit(g, rid, {"partySize": 3}))
    assert body["partyChange"]["notified"] is False and "notice" not in stored(g, rid)


def test_party_and_time_in_one_request_make_one_notice(g):
    rid, _ = confirmed_booking(g, party=3)
    old = stored(g, rid)["notice"]["id"]
    body = body_of(edit(g, rid, {"partySize": 4, "startTime": "20:00"}))
    assert body["partyChange"]["notified"] is True
    notice = stored(g, rid)["notice"]
    assert notice["type"] == "changed" and notice["id"] != old


def test_a_time_only_change_has_no_party_change(g):
    rid, _ = confirmed_booking(g, party=3)
    body = body_of(edit(g, rid, {"startTime": "20:00"}))
    assert "partyChange" not in body and stored(g, rid)["guarantee"]["noShowFee"] == 60000


def _text_item(g, rid):
    return dict(stored(g, rid))


def test_the_changed_texts_carry_the_fee_sentence_in_swedish_and_english(g):
    rid, _ = confirmed_booking(g, party=3)
    edit(g, rid, {"partySize": 4})
    item = _text_item(g, rid)
    tenant, location = tenant_row(TENANT_A), location_item(TENANT_A, LOC_A)
    sv = messages.guest_sms("changed", item, tenant, location, "https://x.se/m")
    assert "Avgift vid uteblivet besök: 800 kr." in sv and "4 personer" in sv
    en = messages.guest_sms("changed", {**item, "language": "en"}, tenant, location, "https://x.se/m")
    assert "No-show fee: 800 kr." in en
    _, text, html = messages.guest_email("changed", item, tenant, location, "https://x.se/m")
    assert "Avgift vid uteblivet besök: 800 kr." in text and "Avgift vid uteblivet besök: 800 kr." in html
    _, text_en, _ = messages.guest_email("changed", {**item, "language": "en"}, tenant, location, None)
    assert "No-show fee: 800 kr." in text_en


def test_no_fee_sentence_without_fees_and_not_on_other_notices(g):
    rid = no_card_booking(g, party=2, minPartySize=9)
    item = _text_item(g, rid)
    tenant, location = tenant_row(TENANT_A), location_item(TENANT_A, LOC_A)
    assert "uteblivet" not in messages.guest_sms("changed", item, tenant, location, None)
    assert messages.fee_sentence(item) == ""


def test_the_fee_sentence_is_only_for_the_changed_notice(g):
    rid, _ = confirmed_booking(g, party=3)
    item = _text_item(g, rid)
    tenant, location = tenant_row(TENANT_A), location_item(TENANT_A, LOC_A)
    assert "Avgift vid uteblivet besök" not in messages.guest_sms("reminder", item, tenant, location, None)
    assert "Avgift vid uteblivet besök" in messages.guest_sms("changed", item, tenant, location, None)


# --- no card: crossing the threshold -----------------------------------------------------------

def test_growing_past_the_threshold_sets_card_needed_and_keeps_the_table(g):
    rid = no_card_booking(g, party=3, minPartySize=4)
    body = body_of(edit(g, rid, {"partySize": 4}))
    change = body["partyChange"]
    assert change["cardRequired"] is True and change["cardLinkSent"] is False and change["cardOnFile"] is False
    assert change["noShowFee"] == 80000 and change["lateCancelFee"] == 40000
    assert change["previousNoShowFee"] == 0 and change["cardWaived"] is False and change["notified"] is True
    item = stored(g, rid)
    assert item["status"] == "reserved" and "pendingExpiresAt" not in item and "guarantee" not in item
    needed = item["cardNeeded"]
    assert needed["partySize"] == 4 and needed["noShowFee"] == 80000 and needed["lateCancelFee"] == 40000
    assert needed["currency"] == "sek" and needed["cutoffHours"] == 24 and needed["since"]
    assert body["cardNeeded"]["noShowFee"] == 80000
    assert "table-b" not in free_tables_at(g, "18:00")  # the guest keeps the table
    assert [h["action"] for h in item["history"][-2:]] == ["edited", "card_needed"]
    assert g["stripe"].calls == [] or all(c["path"] != "/v1/checkout/sessions" for c in g["stripe"].calls)


def test_the_changed_text_states_no_fee_while_the_card_is_missing(g):
    # Nothing can be charged without a card, so the guest is not told a fee.
    rid = no_card_booking(g, party=3, minPartySize=4)
    edit(g, rid, {"partySize": 4})
    item = stored(g, rid)
    assert "cardNeeded" in item and messages.fee_sentence(item) == ""
    tenant, location = tenant_row(TENANT_A), location_item(TENANT_A, LOC_A)
    assert "uteblivet" not in messages.guest_sms("changed", item, tenant, location, None)
    _, text, html = messages.guest_email("changed", item, tenant, location, None)
    assert "uteblivet" not in text and "uteblivet" not in html


def test_a_pending_booking_with_a_snapshot_refuses_a_party_change(g):
    _, body = book(g, 3)
    rid = body["reservationId"]
    before = stored(g, rid)
    assert before["status"] == "pending" and before["guarantee"]["noShowFee"] == 60000
    response = edit(g, rid, {"partySize": 2})
    assert response["statusCode"] == 409 and body_of(response)["error"] == "pending_card"
    after = stored(g, rid)
    assert after["partySize"] == 3 and after["guarantee"] == before["guarantee"]
    assert after.get("version") == before.get("version")
    assert edit(g, rid, {"name": "Anna S"})["statusCode"] == 200  # other edits stay allowed


def test_waive_card_alone_clears_an_existing_card_needed(g):
    rid = no_card_booking(g, party=3, minPartySize=4)
    edit(g, rid, {"partySize": 4})
    assert "cardNeeded" in stored(g, rid)
    response = edit(g, rid, {"waiveCard": True})
    assert response["statusCode"] == 200 and "cardNeeded" not in body_of(response)
    item = stored(g, rid)
    assert "cardNeeded" not in item and item["history"][-1]["action"] == "guarantee_waived"
    assert item["partySize"] == 4 and item["status"] == "reserved"


def test_waive_card_with_a_party_change_clears_an_existing_card_needed(g):
    rid = no_card_booking(g, party=3, minPartySize=4)
    edit(g, rid, {"partySize": 4})
    body = body_of(edit(g, rid, {"partySize": 5, "tableIds": ["table-c"], "waiveCard": True}))
    assert body["partyChange"]["cardWaived"] is True and body["partyChange"]["cardRequired"] is False
    assert "cardNeeded" not in stored(g, rid)


def test_a_status_change_loses_to_an_edit_that_committed_after_it_read(g, monkeypatch):
    rid, _ = confirmed_booking(g, party=3)
    g["clock"].value = datetime(2026, 9, 20, 16, 30, tzinfo=timezone.utc)
    real = shared_r.transact

    def edit_first(items):
        patch_item(g, rid, version=Decimal(int(stored(g, rid).get("version", 0)) + 1))
        return real(items)

    monkeypatch.setattr(shared_r, "transact", edit_first)
    response = staff_status(g, rid, "no_show")
    assert response["statusCode"] == 409 and body_of(response)["error"] == "changed_retry"
    item = stored(g, rid)
    assert item["status"] == "reserved" and "payment" not in item
    assert all(c["path"] != "/v1/payment_intents" for c in g["stripe"].calls)


def test_waiving_the_card_skips_card_needed_and_is_logged(g):
    rid = no_card_booking(g, party=3, minPartySize=4)
    body = body_of(edit(g, rid, {"partySize": 4, "waiveCard": True}))
    change = body["partyChange"]
    assert change["cardRequired"] is False and change["cardWaived"] is True and change["noShowFee"] == 0
    item = stored(g, rid)
    assert "cardNeeded" not in item and item["history"][-1]["action"] == "guarantee_waived"
    assert "uteblivet" not in messages.guest_sms("changed", item, tenant_row(TENANT_A),
                                                 location_item(TENANT_A, LOC_A), None)


def test_waive_card_must_be_a_boolean_and_is_no_edit_on_its_own(g):
    rid = no_card_booking(g, party=3, minPartySize=4)
    assert edit(g, rid, {"partySize": 4, "waiveCard": "yes"})["statusCode"] == 400
    assert edit(g, rid, {"waiveCard": True})["statusCode"] == 400  # nothing to waive


def test_waive_card_is_ignored_when_no_card_is_needed(g):
    rid, _ = confirmed_booking(g, party=3)
    body = body_of(edit(g, rid, {"partySize": 4, "waiveCard": True}))
    assert body["partyChange"]["cardWaived"] is False
    assert all(h["action"] != "guarantee_waived" for h in stored(g, rid)["history"])


def test_staying_under_the_threshold_needs_no_card(g):
    rid = no_card_booking(g, party=2, minPartySize=4)
    body = body_of(edit(g, rid, {"partySize": 3}))
    assert body["partyChange"]["cardRequired"] is False and body["partyChange"]["noShowFee"] == 0
    assert "cardNeeded" not in stored(g, rid)


def test_card_needed_follows_later_party_changes(g):
    rid = no_card_booking(g, party=3, minPartySize=4)
    edit(g, rid, {"partySize": 4})
    since = stored(g, rid)["cardNeeded"]["since"]
    body = body_of(edit(g, rid, {"partySize": 4, "tableIds": ["table-c"]}))  # no party change: untouched
    assert "partyChange" not in body
    body = body_of(edit(g, rid, {"partySize": 5, "tableIds": ["table-c"]}))
    assert body["partyChange"]["noShowFee"] == 100000 and body["partyChange"]["cardRequired"] is True
    assert stored(g, rid)["cardNeeded"]["since"] == since
    body = body_of(edit(g, rid, {"partySize": 3}))
    assert body["partyChange"]["cardRequired"] is False and "cardNeeded" not in stored(g, rid)


def test_a_walk_in_never_needs_a_card(g):
    set_policy(g, minPartySize=4)
    g["clock"].value = datetime(2026, 9, 20, 16, 30, tzinfo=timezone.utc)  # 18:30 local
    response = walk_in(g)
    assert response["statusCode"] == 201, response
    rid = body_of(response)["reservationId"]
    body = body_of(edit(g, rid, {"partySize": 4}))
    assert body["partyChange"]["cardRequired"] is False and "cardNeeded" not in stored(g, rid)


def test_without_a_stripe_account_no_card_is_needed(g):
    rid = no_card_booking(g, party=3, minPartySize=4)
    g["res"].Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A))  # no Stripe account
    shared_tenant.reset_caches()
    body = body_of(edit(g, rid, {"partySize": 4}))
    assert body["partyChange"]["cardRequired"] is False and "cardNeeded" not in stored(g, rid)


def test_the_staff_view_carries_card_needed_but_the_guest_view_does_not(g):
    rid = no_card_booking(g, party=3, minPartySize=4)
    edit(g, rid, {"partySize": 4})
    item = stored(g, rid)
    assert shared_r.staff_view(item)["cardNeeded"]["noShowFee"] == 80000
    assert "cardNeeded" not in shared_r.guest_view(item)


# --- tables ---------------------------------------------------------------------------------------

def test_a_party_that_fits_the_same_table_keeps_it(g):
    rid, _ = confirmed_booking(g, party=3)
    body = body_of(edit(g, rid, {"partySize": 4}))
    assert body["tableIds"] == ["table-b"] and "table-b" not in free_tables_at(g, "18:00")


def test_a_party_needing_a_bigger_table_moves_in_the_same_write(g):
    rid, _ = confirmed_booking(g, party=3)
    refused = edit(g, rid, {"partySize": 5})
    assert refused["statusCode"] == 400 and stored(g, rid)["guarantee"]["noShowFee"] == 60000
    body = body_of(edit(g, rid, {"partySize": 5, "tableIds": ["table-c"]}))
    assert body["tableIds"] == ["table-c"] and body["partyChange"]["noShowFee"] == 100000
    after = stored(g, rid)
    assert after["guarantee"]["noShowFee"] == 100000 and after["seats"] == 6
    assert "table-b" in free_tables_at(g, "18:00") and "table-c" not in free_tables_at(g, "18:00")


def test_a_failed_table_check_leaves_the_fees_untouched(g):
    rid, _ = confirmed_booking(g, party=3)
    other = edit(g, rid, {"partySize": 5, "tableIds": ["table-a"]})  # 2 seats
    assert other["statusCode"] == 400
    assert stored(g, rid)["guarantee"]["noShowFee"] == 60000 and stored(g, rid)["partySize"] == 3


def test_staff_status_after_a_party_change_charges_the_new_amount(g):
    rid, _ = confirmed_booking(g, party=3)
    edit(g, rid, {"partySize": 4})
    g["clock"].value = g["clock"].value.replace(day=20, hour=17)  # after start + grace
    response = staff_status(g, rid, "no_show")
    assert response["statusCode"] == 200
    charge = [c for c in g["stripe"].calls if c["path"] == "/v1/payment_intents"]
    assert charge and charge[-1]["params"]["amount"] == 80000


# --- the guest's late-cancel consent is bound to the amount shown -------------------------------

def test_late_cancel_consent_for_an_old_amount_asks_again(g):
    rid, token = confirmed_booking(g, party=3)
    start = shared_r.parse_iso(stored(g, rid)["bookedFor"])
    g["clock"].value = start - __import__("datetime").timedelta(hours=5)
    ask = cancel(g, rid, token)
    assert ask["statusCode"] == 409 and body_of(ask)["fee"] == 30000
    edit(g, rid, {"partySize": 4})  # staff change the party while the dialog is open
    again = cancel(g, rid, token, acceptFee=True, fee=30000)
    assert again["statusCode"] == 409 and body_of(again)["error"] == "fee_applies"
    assert body_of(again)["fee"] == 40000
    assert stored(g, rid)["status"] == "reserved" and all(
        c["path"] != "/v1/payment_intents" for c in g["stripe"].calls)
    done = cancel(g, rid, token, acceptFee=True, fee=40000)
    assert done["statusCode"] == 200 and body_of(done)["fee"]["amount"] == 40000


def test_late_cancel_fee_field_must_be_a_whole_number(g):
    rid, token = confirmed_booking(g, party=3)
    assert cancel(g, rid, token, acceptFee=True, fee="300")["statusCode"] == 400


def test_a_guest_cancel_loses_to_an_edit_that_committed_after_it_read(g, monkeypatch):
    rid, token = confirmed_booking(g, party=3)
    real = shared_r.transact

    def edit_first(items):
        patch_item(g, rid, version=Decimal(int(stored(g, rid).get("version", 0)) + 1))
        return real(items)

    monkeypatch.setattr(shared_r, "transact", edit_first)
    response = cancel(g, rid, token)
    assert response["statusCode"] == 409 and body_of(response)["error"] == "changed_retry"
    assert stored(g, rid)["status"] == "reserved" and "table-b" not in free_tables_at(g, "18:00")
