"""Audit #31: a cancelled occurrence e-mails every buyer once the tickets
are refunded in the books, even while Stripe still owes a refund (the
e-mail does not claim that refund is paid), and a refund Stripe keeps
refusing is retried once a day instead of every sweep. It is never
dropped: it stays owed, counted in the job's `failed` and the job is not done."""

from datetime import timedelta

from shared import stripe_client as stripe
from shared.features import events_tickets as et
from test_events_tickets import (L, LOC_A, OCC, OCC1, E1, OWNER_A, TENANT_A, buy, call, env, pay,  # noqa: F401
                                 sold, tickets)

JOB = "01JOBAAAAAAAAAAAAAAAAAAAAA"
P = {"locationId": LOC_A, "eventId": E1, "occurrenceId": OCC1}
JP = {"locationId": LOC_A, "jobId": JOB}


def _refuse_refunds_of(env, monkeypatch, ticket_id, error):
    real = env.stripe.__call__

    def fake(method, path, params=None, *, account=None, idempotency_key=None):
        if method == "POST" and path == "/v1/refunds" and (params or {}).get("metadata", {}).get("ticketId") == ticket_id:
            env.stripe.calls.append({"method": method, "path": path, "params": params, "account": account,
                                     "key": idempotency_key})
            raise error
        return real(method, path, params, account=account, idempotency_key=idempotency_key)
    monkeypatch.setattr(stripe, "request", fake)


def _two_orders(env):
    sold(env)
    buy(env, order_id="01ORDERBBBBBBBBBBBBBBBBBBB", qty=1)
    pay(env, order_id="01ORDERBBBBBBBBBBBBBBBBBBB")
    return [t["ticketId"] for t in tickets(env)]


def _cancelled_mails(env):
    return [c["notice"] for c in env.mail.calls if c["notice"]["kind"] == "events-tickets.cancelled"]


def test_buyers_are_emailed_while_stripe_still_refuses_one_refund(env, monkeypatch):
    bad = _two_orders(env)[0]
    _refuse_refunds_of(env, monkeypatch, bad, stripe.StripeError(400, {"code": "charge_disputed"}))
    status, _ = call(env, "POST", OCC + "/cancel", params=P, claims=OWNER_A, body={"jobId": JOB, "version": 1})
    assert status == 202
    _, job = call(env, "GET", L + "/jobs/{jobId}", params=JP, claims=OWNER_A)
    assert job["counts"]["refunded"] == 3 and job["counts"]["failed"] == 1
    mails = _cancelled_mails(env)
    assert len(mails) == 2  # both buyers, at once
    owed = [m for m in mails if "inte klar än" in m["text"]]
    assert len(owed) == 1  # the order whose refund Stripe refused is told it is on its way, not that it is paid
    assert job["done"] is False


def test_a_refused_refund_is_retried_daily_and_stays_owed(env, monkeypatch):
    bad = _two_orders(env)[0]
    _refuse_refunds_of(env, monkeypatch, bad, stripe.StripeError(400, {"code": "charge_disputed"}))
    call(env, "POST", OCC + "/cancel", params=P, claims=OWNER_A, body={"jobId": JOB, "version": 1})
    for _ in range(20):
        env.api.handler({"job": "sweep"})

    def attempts():
        return [c for c in env.stripe.refunds() if c["params"]["metadata"]["ticketId"] == bad]
    assert len(attempts()) == env.api.REFUND_MAX_REFUSED  # no call every 5 minutes any more
    owed = et.pending_refunds()
    assert [t["txId"] for t in owed] == [f"cancel-{bad}"]  # never dropped
    assert owed[0]["stripeRefundFailedAt"] and owed[0]["refundRetryAt"]
    _, job = call(env, "GET", L + "/jobs/{jobId}", params=JP, claims=OWNER_A)
    assert job["done"] is False and job["counts"]["failed"] == 1  # still visible to the owner
    assert len(_cancelled_mails(env)) == 2  # never twice

    env.clock.value += timedelta(hours=env.api.REFUND_SLOW_RETRY_HOURS, minutes=1)
    env.api.handler({"job": "sweep"})
    assert len(attempts()) == env.api.REFUND_MAX_REFUSED + 1  # the daily retry


def test_a_refund_that_succeeds_later_finishes_the_job(env, monkeypatch):
    bad = _two_orders(env)[0]
    real = stripe.request
    _refuse_refunds_of(env, monkeypatch, bad, stripe.StripeError(400, {"code": "charge_disputed"}))
    call(env, "POST", OCC + "/cancel", params=P, claims=OWNER_A, body={"jobId": JOB, "version": 1})
    for _ in range(10):
        env.api.handler({"job": "sweep"})
    monkeypatch.setattr(stripe, "request", real)  # the dispute is settled
    env.clock.value += timedelta(hours=env.api.REFUND_SLOW_RETRY_HOURS, minutes=1)
    env.api.handler({"job": "sweep"})
    env.api.handler({"job": "sweep"})
    assert et.pending_refunds() == []
    _, job = call(env, "GET", L + "/jobs/{jobId}", params=JP, claims=OWNER_A)
    assert job["done"] is True and job["counts"]["failed"] == 0
    assert len(_cancelled_mails(env)) == 2


def test_insufficient_balance_is_not_treated_as_a_refusal(env, monkeypatch):
    bad = _two_orders(env)[0]
    _refuse_refunds_of(env, monkeypatch, bad, stripe.StripeError(402, {"code": "balance_insufficient"}))
    call(env, "POST", OCC + "/cancel", params=P, claims=OWNER_A, body={"jobId": JOB, "version": 1})
    for _ in range(env.api.REFUND_MAX_REFUSED + 2):
        env.api.handler({"job": "sweep"})
    owed = et.pending_refunds()
    assert int(owed[0]["refundAttempts"]) > env.api.REFUND_MAX_REFUSED and "refundRetryAt" not in owed[0]


def test_stripe_unreachable_keeps_retrying_well_past_the_refused_limit(env, monkeypatch):
    bad = _two_orders(env)[0]
    _refuse_refunds_of(env, monkeypatch, bad, stripe.StripeUnavailable("down"))
    call(env, "POST", OCC + "/cancel", params=P, claims=OWNER_A, body={"jobId": JOB, "version": 1})
    for _ in range(env.api.REFUND_MAX_REFUSED + 2):
        env.api.handler({"job": "sweep"})
    owed = et.pending_refunds()
    assert [t["txId"] for t in owed] == [f"cancel-{bad}"]
    assert int(owed[0]["refundAttempts"]) > env.api.REFUND_MAX_REFUSED
