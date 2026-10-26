"""The fiskaly SIGN SE adapter (shared/control_system_fiskaly.py) against a
fake fiskaly (tests/fiskaly_fake.py) that checks every request body with
fiskaly's published OpenAPI spec: tokens and re-authentication, every status
code, retries with backoff on 429 / 5xx / timeouts, refusals, receipts,
returns, copies, training, enrolment of a register, the secret, and a full
sale through Kassa with fiskaly. No network.

An opt-in run against fiskaly's TEST environment is
test_fiskaly_test_environment at the bottom: it runs only with
KASSA_FISKALY_LIVE_TEST=1 and AWS credentials that can read the secret."""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
import pytest
from moto import mock_aws

from fiskaly_fake import TEST_KEY, TEST_SECRET, FakeFiskaly, code, validate
from shared import control_system as cs
from shared import control_system_fiskaly as fk
from shared import dynamo as shared_dynamo
from shared import kassa_receipt, print_ticket
from test_card_payments import location_item
from test_kassa import LOC_A, TENANT_A, TENANT_TABLE, bill, call, env, journal, pay, register, sale  # noqa: F401

CREDS = {"key": TEST_KEY, "secret": TEST_SECRET, "env": "TEST"}
TEST_SERVER = "ISTCSKSVERIFY0001"     # kontrollserverns ID in fiskaly TEST
# compliance.data of a receipt in fiskaly TEST, 2026-10-26 (a TEST code, not a secret)
TEST_RECEIPT_DATA = ("ISTCSKSVERIFY0001,NIKLXBBLGMPOTG6DDPXHVTI5TA7SGART;6LRFC2MVYYVNH5N3TYJT5PGQR6LSDXKV2KRIN6VSBITSYR2M4IY"
                     "NTQXN4KHOHRHJZCAIMNH2KQCX2===,STATUS_OK,Kvitto får skrivas ut")
AT = datetime(2026, 10, 25, 14, 3, tzinfo=ZoneInfo("Europe/Stockholm"))
REF = {"provider": "fiskaly", "env": "TEST", "unitId": "unit-1", "systemId": "0190a9d0-0000-7000-8000-000000000001"}
REGISTER = {"registerId": "r" * 32, "tenantId": "t1", "locationId": "l1", "label": "Kassa 1",
            "serialNumber": "BQ0123456789ABC", "designation": "SE01BQ0000000001", "csRef": REF}
COMPANY = {"name": "Roma AB", "org": "5566778899", "address": "Götgatan 1, 116 21 Stockholm", "restaurant": "Roma"}


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def fresh():
    fk._tokens.clear()
    fk._units.clear()
    fk._servers.clear()
    fk._secret.update(value=None, at=0.0)
    yield
    fk._tokens.clear()
    fk._units.clear()
    fk._secret.update(value=None, at=0.0)


def make(fake=None, creds=CREDS):
    """The adapter on a fake fiskaly where REGISTER's restaurant (unit-1)
    already has its own key (as after enrolment)."""
    fake = fake or FakeFiskaly()
    key, secret = fake.add_unit("unit-1")
    fk._units[("TEST", "t1")] = fk.Creds(key, secret, env="TEST", unit_id="unit-1")
    clock = Clock()
    return fk.FiskalyControlSystem(transport=fake, sleep=clock.sleep, clock=clock, creds=dict(creds)), fake, clock


def receipt(kind=cs.NORMAL, **extra):
    base = {"kind": kind, "at": AT, "org_number": "5566778899", "register_label": "SE01BQ0000000001",
            "address": "Götgatan 1, 116 21 Stockholm", "serial": 7, "user": "anna@roma.se"}
    if "returns_by_rate" not in extra:
        base.update(sales_by_rate={12: 38750}, vat_by_rate={12: 4152},
                    lines=({"text": "Pasta carbonara", "quantity": 2, "total": 29800, "rate": 12, "vat": 3193},
                           {"text": "Dagens soppa", "quantity": 1, "total": 8950, "rate": 12, "vat": 959}),
                    payments=({"method": "card", "amount": 38750, "last4": "4242", "funding": "debit"},))
    base.update(extra)
    return cs.ReceiptData(**base)


def records(fake):
    return [c["body"]["content"] for c in fake.posted("/records")]


# --- status (INTENTION::OPENING) ----------------------------------------------------------

def test_status_is_an_opening_record_and_the_token_is_reused():
    system, fake, _ = make()
    first = system.status(REGISTER)
    second = system.status(REGISTER)
    assert first == cs.Status(ok=True, code=cs.STATUS_OK, provider="fiskaly", serial=TEST_SERVER)
    assert second.ok and fake.tokens == 1
    opening = records(fake)[0]
    assert opening == {"type": "INTENTION", "system": {"id": REF["systemId"]}, "operation": {"type": "OPENING"}}
    call_ = fake.posted("/records")[0]
    assert call_["headers"]["X-Scope-Identifier"] == "unit-1" and call_["headers"]["Authorization"] == "Bearer jwt-u1"


@pytest.mark.parametrize("number,name", [(1, cs.STATUS_OK), (2, cs.STATUS_NO_CONTACT), (3, cs.STATUS_CONTACT_AGAIN),
                                         (4, cs.STATUS_NOT_CONFIGURED), (5, cs.STATUS_CALL_MANUFACTURER),
                                         (6, cs.STATUS_REPORT_24H), (7, cs.STATUS_REPORT_48H)])
@pytest.mark.parametrize("where", ["name", "compliance", "logs"])
def test_every_status_code_maps_to_skatteverkets_name(number, name, where):
    fake = FakeFiskaly()
    fake.status, fake.status_in = number, where
    system, _, _ = make(fake)
    status = system.status(REGISTER)
    assert status.code == name and status.ok is (number == 1)


@pytest.mark.parametrize("value,expected", [("STATUS_OK", 1), ("NO_CONTACT_WITH_KS", 2), ("2", 2), (7, 7),
                                            ("ANMAL_FEL_48H", 7), ("REPORT_ERROR_24H", 6), (True, None), ("x", None)])
def test_status_values_fiskaly_may_use(value, expected):
    assert fk.status_code({"compliance": {"status": value}}) == expected


def test_status_without_a_register_enrolment_is_not_configured():
    system, fake, _ = make()
    assert system.status({**REGISTER, "csRef": None}).code == cs.STATUS_NOT_CONFIGURED
    assert system.status({**REGISTER, "csRef": {**REF, "env": "LIVE"}}).code == cs.STATUS_NOT_CONFIGURED
    assert fake.calls == []


def test_status_never_raises_when_fiskaly_is_down():
    fake = FakeFiskaly()
    fake.fail = [("/records", 503)] * 10
    system, _, clock = make(fake)
    status = system.status(REGISTER)
    assert status == cs.Status(ok=False, code=cs.STATUS_NO_CONTACT, provider="fiskaly", serial=TEST_SERVER)
    assert clock.now <= fk.STATUS_BUDGET


def test_opening_as_fiskaly_test_answers_it():
    """fiskaly TEST, 2026-10-26: the status is compliance.data "STATUS_OK"."""
    record = {"id": "r1", "type": "INTENTION::OPENING", "mode": "FINISHED", "state": "COMPLETED",
              "compliance": {"data": "STATUS_OK"},
              "transmission": {"request": {"content": "PD94"}, "response": {"content": "PD94"}},
              "journal": {"signature": "abc", "signed_at": "2026-10-26T18:00:00Z"}}
    assert fk.status_code(record) == 1 and fk.outcome(record) == cs.STATUS_OK
    assert fk.control_code(record) is None
    assert fk.outcome({**record, "compliance": {"data": "NO_CONTACT_WITH_KS"}}) == cs.STATUS_NO_CONTACT


def test_sign_never_waits_for_the_open_intention():
    """fiskaly keeps INTENTION::TRANSACTION PROCESSING / ACCEPTED until its
    TRANSACTION arrives: polling it would hang until the budget ends."""
    system, fake, clock = make()
    registration = system.sign(REGISTER, receipt())
    intention_id = registration.ref["intentionId"]
    assert fake.records[intention_id]["state"] == "ACCEPTED"
    assert not [c for c in fake.calls if c["method"] == "GET" and intention_id in c["path"]]
    assert fake.open_intentions == set() and clock.sleeps == []
    copy = system.sign(REGISTER, receipt(cs.COPY, serial=1, original_ref=registration.ref))
    assert copy.control_code == "" and fake.open_intentions == set()


def test_a_rejected_intention_stops_the_sale():
    system, fake, _ = make()
    original = fake._record

    def rejecting(content):
        record = original(content)
        if content["type"] == "INTENTION":
            record.update(state="REJECTED", mode="FINISHED", logs=[{"severity": "ERROR", "message": "bad"}])
        return record

    fake._record = rejecting
    with pytest.raises(cs.Rejected):
        system.sign(REGISTER, receipt())
    assert fake.posted("/records")[-1]["body"]["content"]["type"] == "INTENTION"


# --- keys, secrets and tokens never show ----------------------------------------------------

def test_no_repr_shows_a_key_secret_or_token():
    creds = fk.Creds("the-key", "the-secret", env="TEST", unit_id="u1")
    token = fk.Token("the-bearer", 0, "group")
    things = [creds, token, fk.Hidden(key="the-key", secret="the-secret"),
              fk.HiddenBytes(b'{"secret": "the-secret"}'),
              fk.Headers({"Authorization": "Bearer the-bearer", "X-Api-Version": "2026-06-01"}),
              {("TEST", creds.fingerprint()): token}, {("TEST", "t1"): creds},
              fk._hide({"content": {"credentials": {"key": "the-key", "secret": "the-secret"}}})]
    for thing in things:
        for text in (repr(thing), str(thing)):
            assert "the-key" not in text and "the-secret" not in text and "the-bearer" not in text, text
    assert "u1" in repr(creds) and "X-Api-Version" in repr(things[4])


def _traceback_text(exc):
    import traceback
    # From the adapter down: the test's own frame holds the values it looks for.
    return "".join(traceback.TracebackException(type(exc), exc, exc.__traceback__.tb_next,
                                                capture_locals=True).format())


def test_tracebacks_with_locals_show_no_key_secret_or_token(kassa_table):
    system, fake, _ = make()
    fk._units.clear()
    new = {k: v for k, v in REGISTER.items() if k not in ("csRef", "designation")}
    fake.fail = [("/taxpayers", 422)]          # after the unit's key was made and stored
    with pytest.raises(cs.Rejected) as caught:
        system.register_register(new, COMPANY)
    unit_key, unit_secret = next(iter(fake.unit_keys.items()))
    text = _traceback_text(caught.value)
    secrets_ = [TEST_KEY, TEST_SECRET, unit_key, unit_secret[0], *fake.bearers]
    assert not [x for x in secrets_ if x in text]
    fake.fail = [("/records", 500)] * 10
    with pytest.raises(cs.Unreachable) as caught:
        system.sign({**REGISTER, "csRef": {**REF, "unitId": fk.load_unit("t1")["unitId"]}}, receipt())
    text = _traceback_text(caught.value)
    assert not [x for x in [TEST_KEY, TEST_SECRET, unit_key, unit_secret[0], *fake.bearers] if x in text]
    assert "'Authorization': '<hidden>'" in text and "key=<hidden>" in text   # the frames were there


# --- HTTP: 401, 429, 5xx, timeouts, refusals ----------------------------------------------

def test_401_gets_a_new_token_once_and_retries():
    system, fake, _ = make()
    assert system.status(REGISTER).ok
    fake.expired.add("jwt-u1")
    assert system.status(REGISTER).ok
    assert fake.tokens == 2
    assert [c["headers"].get("Authorization") for c in fake.posted("/records")] == [
        "Bearer jwt-u1", "Bearer jwt-u1", "Bearer jwt-u2"]


def test_a_refused_key_is_not_configured_and_never_echoes_the_secret():
    system, fake, _ = make()
    fk._units[("TEST", "t1")].secret = "wrong-secret"
    with pytest.raises(cs.Unreachable) as caught:
        system.sign(REGISTER, receipt())
    assert caught.value.status == cs.STATUS_NOT_CONFIGURED
    assert "wrong-secret" not in repr(caught.value) and TEST_SECRET not in repr(caught.value)
    assert system.status(REGISTER).code == cs.STATUS_NOT_CONFIGURED


def test_unit_calls_use_the_restaurants_own_key_never_the_group_key():
    system, fake, _ = make()
    system.sign(REGISTER, receipt())
    unit_calls = [c for c in fake.calls if c["path"].startswith("/records")]
    assert unit_calls and all(c["headers"]["Authorization"].startswith("Bearer jwt-u") for c in unit_calls)
    tokens = [c["body"]["content"]["key"] for c in fake.posted("/tokens")]
    assert tokens == ["unit-key-1"]
    # fiskaly answers the GROUP token 405 inside a UNIT, scope header or not
    group = fk.FiskalyControlSystem(transport=fake, creds=dict(CREDS))
    with pytest.raises(cs.Rejected) as caught:
        group._call("GET", "/systems/sys-1", scope="unit-1", deadline=group._clock() + 5)
    assert "405 E_METHOD_NOT_ALLOWED" in str(caught.value) and "non-unit organization" in str(caught.value)


def test_a_register_without_its_restaurants_key_is_not_configured():
    system, fake, _ = make()
    fk._units.clear()
    assert system.status(REGISTER).code == cs.STATUS_NOT_CONFIGURED
    assert fake.posted("/records") == []


def test_429_and_5xx_back_off_exponentially_then_succeed():
    fake = FakeFiskaly()
    fake.fail = [("/records", 429), ("/records", 502)]
    system, _, clock = make(fake)
    registration = system.sign(REGISTER, receipt())
    assert len(registration.control_code) == 113
    assert len(clock.sleeps) == 2 and 0.3 <= clock.sleeps[0] <= 0.7 and 0.7 <= clock.sleeps[1] <= 1.3
    keys = [c["headers"]["X-Idempotency-Key"] for c in fake.posted("/records")[:3]]
    assert keys[0] == keys[1] == keys[2], "a retry reuses the idempotency key"


def test_retry_after_is_respected():
    fake = FakeFiskaly()
    system, _, clock = make(fake)
    original = fake.__call__

    def with_header(method, url, headers, body, timeout):
        if not clock.sleeps and url.endswith("/records"):
            fake.calls.append({"method": method, "path": "/records", "headers": headers, "body": None})
            return 429, {"Retry-After": "3"}, {}
        return original(method, url, headers, body, timeout)

    system._transport = with_header
    system.sign(REGISTER, receipt())
    assert clock.sleeps[0] == 3.0


def test_5xx_and_timeouts_end_in_unreachable_within_the_budget():
    fake = FakeFiskaly()
    fake.fail = [("/records", "timeout"), ("/records", 500), ("/records", 503), ("/records", 504), ("/records", 500)]
    system, _, clock = make(fake)
    with pytest.raises(cs.Unreachable) as caught:
        system.sign(REGISTER, receipt())
    assert caught.value.status == cs.STATUS_NO_CONTACT
    assert len(fake.posted("/records")) == fk.RETRIES and clock.now < fk.SIGN_BUDGET


@pytest.mark.parametrize("status", [400, 404, 422])
def test_a_refused_request_is_rejected_without_retry(status):
    fake = FakeFiskaly()
    fake.fail = [("/records", status)]
    system, _, clock = make(fake)
    with pytest.raises(cs.Rejected):
        system.sign(REGISTER, receipt())
    assert len(fake.posted("/records")) == 1 and clock.sleeps == []


def test_a_record_still_processing_is_polled():
    fake = FakeFiskaly()
    fake.processing = 2
    system, _, clock = make(fake)
    registration = system.sign(REGISTER, receipt())
    assert len(registration.control_code) == 113
    assert [c["method"] for c in fake.calls if c["path"].startswith("/records/")] == ["GET", "GET", "GET"]
    assert clock.sleeps == [fk.POLL_SECONDS] * 3


# --- receipts ---------------------------------------------------------------------------

def test_sale_is_an_intention_then_a_receipt_with_lines_vat_and_card():
    system, fake, _ = make()
    registration = system.sign(REGISTER, receipt())
    intention, transaction = records(fake)
    assert intention["operation"] == {"type": "TRANSACTION",
                                      "details": {"creators": [{"type": "PERSON", "label": "anna@roma.se"}]}}
    assert transaction["record"]["id"] == registration.ref["intentionId"]
    op = transaction["operation"]
    assert op["document"] == {"number": "7", "series": "KVITTO", "issued_at": "2026-10-25T14:03:00+01:00",
                              "operation_date": "2026-10-25"}
    pasta, soup = op["entries"]
    # 31,93 kr VAT doesn't split evenly over 2: the line is one unit.
    assert pasta["type"] == "SALE" and pasta["data"]["text"] == "2 x Pasta carbonara"
    assert pasta["data"]["unit"] == {"quantity": "1", "price": {"inclusive": "298.00", "exclusive": "266.07"}}
    assert soup["data"]["text"] == "Dagens soppa" and soup["data"]["value"] == {"base": "79.91"}
    assert pasta["data"]["vat"] == {"type": "VAT_RATE", "code": "REDUCED_1", "percentage": "12.00", "amount": "31.93",
                                    "exclusive": "266.07", "inclusive": "298.00"}
    assert op["breakdown"] == [{"type": "VAT_RATE", "code": "REDUCED_1", "percentage": "12.00", "amount": "41.52",
                                "exclusive": "345.98", "inclusive": "387.50"}]
    assert op["totals"] == {"vat": {"amount": "41.52", "exclusive": "345.98", "inclusive": "387.50"}}
    assert op["payments"] == [{"type": "CARD", "kind": "DEBIT", "number": "4242",
                               "details": {"amount": "387.50", "currency": "SEK", "date": "2026-10-25"}}]
    assert registration.control_code == code(1) and registration.serial == TEST_SERVER
    assert registration.ref == {"provider": "fiskaly", "recordId": registration.ref["recordId"],
                                "intentionId": registration.ref["intentionId"]}


def test_quantity_and_unit_price_when_exact_to_the_ore():
    op = fk.operation(receipt(lines=({"text": "Kaffe", "quantity": 3, "total": 9000, "rate": 12, "vat": 964},),
                              sales_by_rate={12: 9000}, vat_by_rate={12: 964}))
    assert op["entries"][0]["data"]["unit"]["quantity"] == "1" and op["entries"][0]["data"]["text"] == "3 x Kaffe"
    op = fk.operation(receipt(lines=({"text": "Kaffe", "quantity": 3, "total": 9000, "rate": 12, "vat": 963},),
                              sales_by_rate={12: 9000}, vat_by_rate={12: 963}))
    assert op["entries"][0]["data"]["text"] == "Kaffe"
    assert op["entries"][0]["data"]["unit"] == {"quantity": "3", "price": {"inclusive": "30.00", "exclusive": "26.79"}}
    assert validate("ReceiptTransaction", op) == []


def test_return_uses_positive_return_entries_and_refers_to_the_sale():
    system, fake, _ = make()
    data = receipt(returns_by_rate={12: 14900}, vat_by_rate={12: 1596}, original_serial=3,
                   lines=({"text": "Pasta carbonara", "quantity": 1, "total": 14900, "rate": 12, "vat": 1596},),
                   payments=({"method": "card", "amount": 14900, "last4": "4242", "funding": "credit"},))
    system.sign(REGISTER, data)
    op = records(fake)[1]["operation"]
    assert [e["type"] for e in op["entries"]] == ["RETURN"]
    assert op["entries"][0]["data"]["vat"]["inclusive"] == "149.00"
    assert op["document"]["references"] == {"preceding_document": {"number": "3"}}
    assert op["payments"][0]["kind"] == "CREDIT" and op["payments"][0]["details"]["amount"] == "149.00"


def test_copy_is_the_duplicate_record_alone():
    """fiskaly TEST: INTENTION::DUPLICATE of the original TRANSACTION::RECEIPT
    record is the whole copy (status OK, no code of its own)."""
    system, fake, _ = make()
    original = system.sign(REGISTER, receipt())
    sale_receipt = records(fake)[1]
    copy = system.sign(REGISTER, receipt(cs.COPY, serial=1, original_serial=7, original_at=AT,
                                         original_ref=original.ref))
    assert len(records(fake)) == 3
    duplicate = records(fake)[2]
    assert duplicate["operation"] == {"type": "DUPLICATE", "record": {"id": original.ref["recordId"]}, "number": "1",
                                      "reason": "Kvittokopia", "details": {
                                          "creators": [{"type": "PERSON", "label": "anna@roma.se"}]}}
    assert fake.records[original.ref["recordId"]]["type"] == "TRANSACTION::RECEIPT"
    assert sale_receipt["type"] == "TRANSACTION"
    assert copy.control_code == "" and copy.serial == TEST_SERVER and copy.message == "Kvitto får skrivas ut"


def test_a_receipt_after_a_duplicate_is_refused_like_fiskaly():
    system, fake, _ = make()
    original = system.sign(REGISTER, receipt())
    system.sign(REGISTER, receipt(cs.COPY, serial=1, original_ref=original.ref))
    duplicate_id = [r for r in fake.records.values() if r["type"] == "INTENTION::DUPLICATE"][0]["id"]
    with pytest.raises(cs.Rejected) as caught:
        system._call("POST", "/records", {"content": {"type": "TRANSACTION", "record": {"id": duplicate_id},
                                                      "operation": fk.operation(receipt())}},
                     unit=fk._units[("TEST", "t1")], deadline=system._clock() + 5)
    assert "405" in str(caught.value) and "INTENTION::TRANSACTION" in str(caught.value)


def test_a_copy_refused_by_status_is_no_copy():
    system, fake, _ = make()
    original = system.sign(REGISTER, receipt())
    fake.status = 3
    with pytest.raises(cs.Unreachable):
        system.sign(REGISTER, receipt(cs.COPY, serial=1, original_ref=original.ref))


@pytest.mark.parametrize("kind", [cs.NORMAL])
def test_a_sale_or_return_with_an_empty_code_is_rejected(kind):
    system, fake, _ = make()
    original = fake._record

    def empty_code(content):
        record = original(content)
        if content["type"] == "TRANSACTION":
            record["compliance"] = {"data": f"{TEST_SERVER},,STATUS_OK,Kvitto får skrivas ut"}
        return record

    fake._record = empty_code
    with pytest.raises(cs.Rejected):
        system.sign(REGISTER, receipt(kind))
    with pytest.raises(cs.Rejected):
        system.sign(REGISTER, receipt(returns_by_rate={12: 8950}, vat_by_rate={12: 959}, original_serial=1,
                                      lines=({"text": "Soppa", "quantity": 1, "total": 8950, "rate": 12, "vat": 959},)))


def test_copy_without_the_original_record_is_rejected():
    system, _, _ = make()
    with pytest.raises(cs.Rejected):
        system.sign(REGISTER, receipt(cs.COPY, serial=1, original_ref=None))


def test_training_receipt_has_no_code_and_no_payment():
    system, fake, _ = make()
    registration = system.sign(REGISTER, receipt(cs.TRAINING, payments=()))
    intention, transaction = records(fake)
    assert intention["operation"]["details"]["training"] is True
    assert transaction["operation"]["document"]["series"] == "OVNING"
    assert transaction["operation"]["payments"][0]["type"] == "OTHER"
    assert registration.control_code == ""


@pytest.mark.parametrize("status", [2, 3, 4, 5, 6, 7])
def test_no_receipt_unless_status_ok(status):
    fake = FakeFiskaly()
    fake.status = status
    system, _, _ = make(fake)
    with pytest.raises(cs.Unreachable) as caught:
        system.sign(REGISTER, receipt())
    assert caught.value.status == fk.CODES[status]


def test_a_rejected_record_is_rejected():
    assert pytest.raises(cs.Rejected, fk.outcome, {"state": "REJECTED", "logs": [{"message": "bad"}]})
    assert fk.outcome({"state": "COMPLETED"}) == cs.STATUS_OK
    assert fk.outcome({"state": "FAILED"}) == cs.STATUS_NO_CONTACT


def test_receipt_compliance_data_as_fiskaly_test_sends_it():
    record = {"id": "r2", "type": "TRANSACTION::RECEIPT", "mode": "FINISHED", "state": "COMPLETED",
              "compliance": {"data": TEST_RECEIPT_DATA}}
    code_ = "NIKLXBBLGMPOTG6DDPXHVTI5TA7SGART;6LRFC2MVYYVNH5N3TYJT5PGQR6LSDXKV2KRIN6VSBITSYR2M4IYNTQXN4KHOHRHJZCAIMNH2KQCX2==="
    assert fk.compliance_fields(record) == {"server": TEST_SERVER, "code": code_, "status": "STATUS_OK",
                                            "text": "Kvitto får skrivas ut"}
    assert fk.control_code(record) == code_ and len(code_) == cs.AVSTAMNING_LENGTH
    assert fk.server_id(record) == TEST_SERVER and fk.outcome(record) == cs.STATUS_OK


@pytest.mark.parametrize("data,fields", [
    ("STATUS_OK", {"status": "STATUS_OK"}),
    ("NO_CONTACT_WITH_KS", {"status": "NO_CONTACT_WITH_KS"}),
    ("S,C,ANMAL_FEL_48H,Kvitto får inte skrivas ut", {"server": "S", "code": "C", "status": "ANMAL_FEL_48H",
                                                      "text": "Kvitto får inte skrivas ut"}),
    ("S,C,STATUS_OK,Text, med komma", {"server": "S", "code": "C", "status": "STATUS_OK", "text": "Text, med komma"}),
    ("S,C,STATUS_OK", None), ("S,C1,C2,STATUS_OK,Text", None), ("hej", None), ("", None),
])
def test_compliance_data_by_field_count(data, fields):
    assert fk.compliance_fields({"compliance": {"data": data}}) == fields


def test_a_receipt_without_a_usable_code_is_rejected():
    system, fake, _ = make()
    original = fake._record

    def three_fields(content):
        record = original(content)
        if content["type"] == "TRANSACTION":
            record["compliance"] = {"data": "S,C,STATUS_OK"}
        return record

    fake._record = three_fields
    with pytest.raises(cs.Rejected):
        system.sign(REGISTER, receipt())


def test_server_id_comes_from_the_receipt_and_is_journalled(with_fiskaly):
    env = with_fiskaly
    rid = register(env)
    sale(env, rid)
    entry = next(e for e in journal(env, rid) if e["type"] == "kassakvitto")
    assert entry["data"]["csSerial"] == TEST_SERVER and entry["data"]["csMessage"] == "Kvitto får skrivas ut"
    assert ";" in entry["data"]["controlCode"] and entry["data"]["controlCode"].endswith("===")


def test_close_day_and_pause_register():
    system, fake, _ = make()
    system.close_day(REGISTER)
    assert records(fake)[0]["operation"] == {"type": "CLOSING", "period": "DAILY"}
    system.pause_register(REGISTER)
    patch = [c for c in fake.calls if c["method"] == "PATCH"][0]
    assert patch["path"] == f"/systems/{REF['systemId']}" and patch["body"] == {"content": {"state": "DECOMMISSIONED"}}


# --- enrolment ------------------------------------------------------------------------------

@pytest.fixture
def kassa_table(monkeypatch):
    monkeypatch.setenv("KASSA_TABLE_NAME", "test-kassa")
    monkeypatch.setenv("FISKALY_UNIT_PREFIX", "booqy/dev/fiskaly-unit/")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "t")
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        boto3.resource("dynamodb", region_name="eu-north-1").create_table(
            TableName="test-kassa", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        yield boto3.resource("dynamodb", region_name="eu-north-1").Table("test-kassa")
        shared_dynamo._resource = None
        shared_dynamo._client = None


def test_enrolment_creates_merchant_company_location_once_and_a_system_per_register(kassa_table):
    system, fake, _ = make()
    new = {k: v for k, v in REGISTER.items() if k not in ("csRef", "designation")}
    first = system.register_register(new, COMPANY)
    second = system.register_register({**new, "registerId": "s" * 32, "label": "Bar"}, COMPANY)
    assert first.designation == "SE01BQ0000000001" and second.designation == "SE01BQ0000000002"
    assert first.ref["unitId"] == second.ref["unitId"] and first.ref["systemId"] != second.ref["systemId"]
    assert [len(fake.posted(p)) for p in ("/organizations", "/taxpayers", "/locations", "/systems")] == [1, 1, 1, 2]
    taxpayer = fake.posted("/taxpayers")[0]["body"]["content"]
    assert taxpayer["fiscalization"] == {"type": "SE", "tax_id_number": "5566778899"}
    assert taxpayer["address"] == {"line": {"type": "STREET_NUMBER", "street": "Götgatan", "number": "1"},
                                   "code": "11621", "city": "Stockholm", "country": "SE"}
    created = fake.posted("/systems")[0]["body"]["content"]
    assert created["producer"]["details"] == {"name": "Booqy Kassa SE", "brand": "Booqy", "model": "Booqy Kassa SE",
                                              "serial": "BQ0123456789ABC"}
    commissioned = [c["path"].split("/")[1] for c in fake.calls
                    if c["method"] == "PATCH" and c["body"] == {"content": {"state": "COMMISSIONED"}}]
    assert commissioned == ["taxpayers", "locations", "systems", "systems"]
    assert fake.posted("/organizations")[0]["headers"]["X-Scope-Identifier"] == "group-org"
    state = kassa_table.get_item(Key={"PK": "TENANT#t1", "SK": "CS#fiskaly#TEST"})["Item"]
    assert state["unitId"] == first.ref["unitId"] and set(state["locations"]) == {"l1"}


def test_enrolment_makes_one_key_per_restaurant_and_keeps_it_in_its_secret(kassa_table):
    system, fake, _ = make()
    fk._units.clear()
    new = {k: v for k, v in REGISTER.items() if k not in ("csRef", "designation")}
    first = system.register_register(new, COMPANY)
    subject = fake.posted("/subjects")[0]
    assert subject["headers"]["X-Scope-Identifier"] == first.ref["unitId"]
    assert subject["headers"]["Authorization"].startswith("Bearer jwt-") and "jwt-u" not in subject["headers"]["Authorization"]
    assert subject["body"]["content"] == {"type": "API_KEY", "name": "bq-t1"}
    stored = json.loads(boto3.client("secretsmanager", region_name="eu-north-1").get_secret_value(
        SecretId="booqy/dev/fiskaly-unit/t1")["SecretString"])
    assert stored["unitId"] == first.ref["unitId"] and stored["env"] == "TEST" and stored["key"] and stored["secret"]
    for path in ("/taxpayers", "/locations", "/systems"):
        assert all(c["headers"]["Authorization"].startswith("Bearer jwt-u") for c in fake.posted(path)), path
    fk._units.clear()          # a cold start: the key comes back from the secret, no new one is made
    fk._tokens.clear()
    system.register_register({**new, "registerId": "s" * 32}, COMPANY)
    assert len(fake.posted("/subjects")) == 1 and len(fake.posted("/organizations")) == 1
    registered = {**new, "csRef": first.ref}
    assert system.status(registered).ok


def test_a_failed_key_store_is_retried_without_a_second_unit(kassa_table, monkeypatch):
    system, fake, _ = make()
    fk._units.clear()
    new = {k: v for k, v in REGISTER.items() if k not in ("csRef", "designation")}
    real = fk.store_unit
    monkeypatch.setattr(fk, "store_unit", lambda *a: (_ for _ in ()).throw(cs.Unreachable(cs.STATUS_NOT_CONFIGURED)))
    with pytest.raises(cs.Unreachable):
        system.register_register(new, COMPANY)
    assert kassa_table.get_item(Key={"PK": "TENANT#t1", "SK": "CS#fiskaly#TEST"})["Item"]["unitId"]
    monkeypatch.setattr(fk, "store_unit", real)
    enrolment = system.register_register(new, COMPANY)
    assert len(fake.posted("/organizations")) == 1
    assert enrolment.designation and fk.load_unit("t1")["unitId"] == enrolment.ref["unitId"]


def test_unit_secret_is_never_in_an_error(kassa_table, monkeypatch):
    from botocore.exceptions import ClientError

    class Refusing:
        def create_secret(self, **kw):
            raise ClientError({"Error": {"Code": "InvalidRequestException",
                                         "Message": f"scheduled for deletion {kw['SecretString']}"}}, "CreateSecret")

    monkeypatch.setattr(boto3, "client", lambda *a, **kw: Refusing())
    with pytest.raises(cs.Unreachable) as caught:
        fk.store_unit("t9", {"env": "TEST", "unitId": "u9", "key": "k9", "secret": "s3cr3t-value"})
    assert "s3cr3t" not in repr(caught.value) and caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_no_key_yet_is_none(kassa_table):
    assert fk.load_unit("nobody") is None


def test_enrolment_uses_the_companys_own_address_when_console_has_it(kassa_table):
    system, fake, _ = make()
    new = {k: v for k, v in REGISTER.items() if k not in ("csRef", "designation")}
    system.register_register(new, {**COMPANY, "legalAddress": {"street": "Hornsgatan 12 B", "postalCode": "118 20",
                                                               "city": "Stockholm"}})
    assert fake.posted("/taxpayers")[0]["body"]["content"]["address"]["line"] == {
        "type": "STREET_NUMBER", "street": "Hornsgatan", "number": "12B"}
    assert fake.posted("/locations")[0]["body"]["content"]["address"]["code"] == "11621"


def test_enrolment_needs_an_organisation_number_and_a_full_address(kassa_table):
    system, _, _ = make()
    new = {k: v for k, v in REGISTER.items() if k not in ("csRef", "designation")}
    with pytest.raises(cs.Rejected):
        system.register_register(new, {**COMPANY, "org": "198001011234"})
    with pytest.raises(cs.Rejected):
        system.register_register(new, {**COMPANY, "address": "Götgatan, Stockholm"})


@pytest.mark.parametrize("text,street,number,postal", [
    ("Götgatan 1, 116 21 Stockholm", "Götgatan", "1", "11621"),
    ("Restaurang Roma, Sankt Eriksgatan 117A, 113 43 Stockholm", "Sankt Eriksgatan", "117A", "11343"),
    ("Storgatan 5-7, 41101 Göteborg", "Storgatan", "5-7", "41101"),
])
def test_address_lines(text, street, number, postal):
    address = fk.address(None, text)
    assert address["line"] == {"type": "STREET_NUMBER", "street": street, "number": number}
    assert address["code"] == postal


# --- the secret ------------------------------------------------------------------------------

def test_credentials_come_from_secrets_manager_and_only_live_is_certifiable(monkeypatch):
    monkeypatch.setenv("FISKALY_SECRET_ID", "booqy/dev/fiskaly-sign-se")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "t")
    with mock_aws():
        client = boto3.client("secretsmanager", region_name="eu-north-1")
        assert fk.FiskalyControlSystem().env is None
        client.create_secret(Name="booqy/dev/fiskaly-sign-se", SecretString=json.dumps(
            {"api_key": TEST_KEY, "api_secret": TEST_SECRET, "env": "TEST"}))
        fk._secret.update(value=None, at=0.0)
        system = fk.FiskalyControlSystem()
        assert system.env == "TEST" and system.certifiable is False and system.serial == TEST_SERVER
        client.put_secret_value(SecretId="booqy/dev/fiskaly-sign-se", SecretString=json.dumps(
            {"api_key": TEST_KEY, "api_secret": TEST_SECRET, "env": "LIVE"}))
        fk._secret.update(value=None, at=0.0)
        assert fk.FiskalyControlSystem().certifiable is True


def test_the_environment_picks_the_control_system(monkeypatch):
    monkeypatch.delenv("KASSA_CONTROL_SYSTEM", raising=False)
    assert isinstance(cs.for_location({}), cs.SimulatedControlSystem)
    monkeypatch.setenv("KASSA_CONTROL_SYSTEM", "fiskaly")
    assert isinstance(cs.for_location({}), fk.FiskalyControlSystem)
    monkeypatch.setenv("KASSA_CERTIFIED", "true")
    assert cs.certified(fk.FiskalyControlSystem(creds={**CREDS, "env": "TEST"})) is False
    assert cs.certified(fk.FiskalyControlSystem(creds={**CREDS, "env": "LIVE"})) is True
    assert cs.certified(cs.SimulatedControlSystem()) is False


# --- Kassa with fiskaly, end to end --------------------------------------------------------

@pytest.fixture
def with_fiskaly(env, monkeypatch):
    fake = FakeFiskaly()
    monkeypatch.setenv("KASSA_CONTROL_SYSTEM", "fiskaly")
    monkeypatch.setenv("FISKALY_SECRET_ID", "booqy/dev/fiskaly-sign-se")
    monkeypatch.setenv("FISKALY_UNIT_PREFIX", "booqy/dev/fiskaly-unit/")
    monkeypatch.setattr(fk, "_urllib", fake)
    boto3.client("secretsmanager", region_name="eu-north-1").create_secret(
        Name="booqy/dev/fiskaly-sign-se",
        SecretString=json.dumps({"api_key": TEST_KEY, "api_secret": TEST_SECRET, "env": "TEST"}))
    env["res"].Table("test-location").put_item(Item=location_item(
        TENANT_A, LOC_A, {"accountId": "acct_roma", "locationId": "tml_a1"}, address="Götgatan 1, 116 21 Stockholm"))
    from shared import tenant as shared_tenant
    shared_tenant.reset_caches()
    return {**env, "fiskaly": fake}


def test_kassa_sale_copy_return_and_z_with_fiskaly(with_fiskaly, xml_schema):
    env = with_fiskaly
    rid = register(env)
    status, state = call(env, "GET")
    reg = state["registers"][0]
    assert reg["designation"] == "SE01BQ0000000001" and reg["label"] == "Kassa 1"
    assert reg["controlSystem"]["provider"] == "fiskaly" and reg["controlSystem"]["serial"] == TEST_SERVER
    assert state["testMode"] is True and state["simulator"] is False and state["canSimulate"] is False
    assert call(env, "POST", f"registers/{rid}/open-day", body={"float": 0})[0] == 200
    b, done = sale(env, rid)
    rows = done["document"]["rows"]
    texts = [r.get("text") or " ".join(r.get("pair") or []) for r in rows]
    assert "Kassabeteckning SE01BQ0000000001" in texts and "Kassa Kassa 1" in texts
    assert f"Kontrollsystem: {TEST_SERVER}" in texts
    code_rows = [r["text"] for r in rows if r.get("code")]
    sold = next(e for e in journal(env, rid) if e["type"] == "kassakvitto")["data"]["controlCode"]
    assert "".join(code_rows) == sold and len(sold) == 113
    assert len(code_rows) == 4 and all(len(t) <= kassa_receipt.CODE_ROW for t in code_rows)
    status, copied = call(env, "POST", f"registers/{rid}/receipts/1/copy", body={"delivery": "print"})
    assert status == 201
    copy_rows = copied["document"]["rows"]
    assert "".join(r["text"] for r in copy_rows if r.get("code")) == sold
    assert [r for r in copy_rows if r.get("text") == "KOPIA" and r.get("big")]
    assert not [r for r in copy_rows if r.get("text") == "Kontrollkod för kopian:"]
    pasta = next(line for line in b["lines"] if line["menuItemId"] == "pasta")
    status, returned = call(env, "POST", f"registers/{rid}/receipts/1/return",
                            body={"lines": [{"lineId": pasta["lineId"], "quantity": 1}], "reason": "Fel rätt",
                                  "delivery": "print"})
    assert status == 201, returned
    assert call(env, "POST", f"registers/{rid}/z", body={})[0] == 200
    kinds = [c["body"]["content"]["operation"]["type"] for c in env["fiskaly"].posted("/records")]
    assert "DUPLICATE" in kinds and kinds[-1] == "CLOSING" and kinds.count("OPENING") >= 3
    entries = journal(env, rid)
    sale_entry = next(e for e in entries if e["type"] == "kassakvitto")
    assert sale_entry["label"] == "SE01BQ0000000001" and sale_entry["registerName"] == "Kassa 1"
    assert sale_entry["data"]["csRef"]["recordId"] and len(sale_entry["data"]["controlCode"]) == 113
    status, xml = call(env, "GET", f"registers/{rid}/export", query={"from": "2026-10-24", "to": "2026-10-24"})
    xml_schema.validate(xml)
    assert "<utreko:Avstamningskod>" in xml and "<utreko:Kontrollkod>" not in xml
    assert "<utreko:AvstamningskodForKvittotSomKopianAvser>" in xml
    assert "<utreko:Kassabeteckning>SE01BQ0000000001<" in xml
    assert f"<utreko:TillverkningsnummerForKontrollsystemet>{TEST_SERVER}<" in xml


def test_kassa_blocks_the_card_payment_when_fiskaly_says_no_contact(with_fiskaly):
    env = with_fiskaly
    rid = register(env)
    b = bill(env, rid)
    env["fiskaly"].status = 2
    status, body = call(env, "POST", f"bills/{b['billId']}/payments",
                        body={"checkoutId": "a" * 32, "readerId": "tmr_a1", "amount": b["total"]})
    assert status == 503 and body["error"] == "control_system_unreachable"
    assert env["stripe"].count("/v1/payment_intents") == 0
    status, body = call(env, "POST", f"registers/{rid}/open-day", body={"float": 0})
    assert status == 503


def test_kassa_reports_a_rejected_receipt(with_fiskaly):
    env = with_fiskaly
    rid = register(env)
    b = bill(env, rid)
    pay(env, b["billId"], b["total"], "a" * 32)
    env["fiskaly"].fail = [("/records", 422)]
    status, body = call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "print"})
    assert status == 503 and body["error"] == "control_system_rejected"
    status, body = call(env, "POST", f"bills/{b['billId']}/finalize", body={"delivery": "print"})
    assert status == 201 and body["document"]["seq"] == 1


def test_long_code_fits_80_and_58_mm_and_email():
    long_code = code(9)
    entry = {"type": "kassakvitto", "jseq": 3, "local": "2026-10-25T14:03:00", "label": "SE01BQ0000000001",
             "data": {"seq": 1, "company": {"name": "Roma AB", "org": "5566778899", "address": "Götgatan 1"},
                      "lines": [], "total": 0, "vat": [], "payments": [], "controlCode": long_code,
                      "csSerial": "ISTCSKS0000000011"}}
    document = kassa_receipt.document(entry)
    rows = [r["text"] for r in document["rows"] if r.get("code")]
    assert "".join(rows) == long_code and [len(r) for r in rows] == [29, 29, 29, 26]
    for width, columns in ((80, 48), (58, 32)):
        printed = print_ticket.document_ticket(document, width=width).decode("cp1252", errors="ignore")
        assert all(row in printed for row in rows), width
        assert max(len(r) for r in rows) <= columns
    _, text, body = kassa_receipt.email(document, {"name": "Roma"}, {})
    assert all(row in text for row in rows) and all(row in body for row in rows)
    short = [r["text"] for r in kassa_receipt.code_rows("A" * 59)]
    assert [len(r) for r in short] == [30, 29]


@pytest.fixture(scope="module")
def xml_schema():
    from test_kassa_xml import XSD
    import xmlschema
    return xmlschema.XMLSchema(str(XSD))


# --- opt-in: fiskaly's TEST environment ------------------------------------------------------

@pytest.mark.skipif(os.environ.get("KASSA_FISKALY_LIVE_TEST") != "1",
                    reason="set KASSA_FISKALY_LIVE_TEST=1 with AWS credentials that can read the fiskaly secret")
def test_fiskaly_test_environment():
    """Against test.api.fiskaly.com (TEST only, never LIVE): enrol a register
    for a throwaway tenant (a new UNIT and its own key), the startup check, a
    sale, its copy, a return, a training receipt, the day's closing, then
    decommission. One line per call: compliance, logs, state and mode as
    fiskaly sent them, the shape of the rest; failures in full. Never a key,
    a secret or a token. Run with --tb=short so pytest prints no arguments.
    The GROUP key is read from FISKALY_SECRET_ID (default the dev secret);
    the restaurant's key and the fiskaly ids live in moto only."""
    creds = fk.credentials() if os.environ.setdefault("FISKALY_SECRET_ID", "booqy/dev/fiskaly-sign-se") else None
    assert creds.env == "TEST", "the opt-in test runs against fiskaly TEST only"

    def recording(method, url, headers, body, timeout):
        """One line per call, starting with the method. Failures also print
        what was sent and fiskaly's whole answer. Never a key, a secret or a
        token: /tokens prints its status only, /subjects only shapes."""
        status, answer_headers, answer = fk._urllib(method, url, headers, body, timeout)
        path = url.split("fiskaly.com", 1)[1]
        if path.startswith("/tokens"):
            print(method, path, status)
        elif status is None or status >= 300:
            print(method, path, status, "FAILED scope:", headers.get("X-Scope-Identifier"))
            print("  sent:", (body or b"").decode("utf-8"))
            print("  answer:", json.dumps(answer, ensure_ascii=False))
        else:
            print(method, path, status, json.dumps(shape(answer.get("content", answer)), ensure_ascii=False))
        return status, answer_headers, answer

    def shape(value):
        if isinstance(value, dict):
            return {k: (v if k in ("compliance", "logs", "state", "mode", "type") else shape(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [shape(v) for v in value[:2]]
        return f"str[{len(value)}]" if isinstance(value, str) else type(value).__name__

    with mock_aws():
        # The restaurant's own key (POST /subjects) lands in moto, never in the real account.
        os.environ["KASSA_TABLE_NAME"] = "it-kassa"
        os.environ["FISKALY_UNIT_PREFIX"] = "booqy/it/fiskaly-unit/"
        shared_dynamo._resource = None
        shared_dynamo._client = None
        boto3.resource("dynamodb", region_name="eu-north-1").create_table(
            TableName="it-kassa", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        system = fk.FiskalyControlSystem(transport=recording, creds=creds)
        tenant = f"it-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        new = {"registerId": "r" * 32, "tenantId": tenant, "locationId": "it-location", "label": "Test 1",
               "serialNumber": "BQIT" + datetime.now().strftime("%H%M%S")}
        try:
            enrolment = system.register_register(new, {**COMPANY, "org": os.environ.get("KASSA_FISKALY_TEST_ORG",
                                                                                      "5560123456")})
            register_row = {**new, "designation": enrolment.designation, "csRef": enrolment.ref}
            print("tillverkningsnummer", enrolment.designation)
            print("status", system.status(register_row))
            sold = system.sign(register_row, receipt(serial=1, register_label=enrolment.designation))
            print("record sale: control code length", len(sold.control_code))
            copy = system.sign(register_row, receipt(cs.COPY, serial=1, original_serial=1, original_at=AT,
                                                     original_ref=sold.ref, register_label=enrolment.designation))
            print("record copy: control code length", len(copy.control_code))
            returned = system.sign(register_row, receipt(returns_by_rate={12: 8950}, vat_by_rate={12: 959}, original_serial=1,
                                              serial=2, register_label=enrolment.designation,
                                              lines=({"text": "Dagens soppa", "quantity": 1, "total": 8950, "rate": 12,
                                                      "vat": 959},),
                                              payments=({"method": "card", "amount": 8950, "last4": "4242"},)))
            print("record return: control code length", len(returned.control_code))
            training = system.sign(register_row, receipt(cs.TRAINING, serial=1, payments=(),
                                                         register_label=enrolment.designation))
            print("record training: control code length", len(training.control_code))
            system.close_day(register_row)
            print("record closing: sent")
            system.pause_register(register_row)
            print("record decommissioned")
        finally:
            shared_dynamo._resource = None
            shared_dynamo._client = None
        assert len(sold.control_code) == cs.AVSTAMNING_LENGTH


def test_register_view_tells_what_to_report_to_skatteverket(with_fiskaly):
    env = with_fiskaly
    register(env)
    reg = call(env, "GET")[1]["registers"][0]
    assert reg["controlSystem"]["name"] == "fiskaly SIGN SE" and reg["controlSystem"]["connected"] is True
    assert reg["controlSystem"]["unit"] == {"manufacturer": "TCS", "model": "System ver. 1.1 + Miljö rev. A",
                                            "address": "Tunnelgatan 2, Stockholm", "serverId": TEST_SERVER}
    assert reg["csEnrolledAt"].startswith("2026-10-24")
