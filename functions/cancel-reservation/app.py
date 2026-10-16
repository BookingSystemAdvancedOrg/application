"""cancel-reservation

TRIGGER:
    API Gateway -- POST /locations/{locationId}/reservations/{reservationId}/cancel
    Auth: NONE, header X-Manage-Token (the guest's manage link)

PURPOSE:
    A guest cancels their own booking before it starts. The tables are freed
    in the same transaction (holds deleted, table locks bumped), the booking
    keeps its row with status "cancelled_no_charge" and a history entry.
    Cancelling twice is answered with the booking as it is (idempotent).

    Card guarantee: inside the cutoff the guest accepted at booking, a
    guaranteed booking costs the late-cancellation fee. The guest must send
    {"acceptFee": true} - without it the answer is 409 {"error":
    "fee_applies", "fee": <öre>} so the site can show the amount first. The
    booking is cancelled (tables freed, receipt sent) and then the fee is
    charged off-session (shared/guarantee.py): status cancelled_charged or
    cancelled_charge_failed. If Stripe is unreachable the booking stays
    cancelled_no_charge with payment.status "pending" for staff to retry.
    Pending bookings (card never confirmed) cancel for free.

    Multi-tenant: the location decides the tenant; a wrong token, another
    location's booking or an unknown id all answer 404.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    SLOT_OCCUPANCY_TABLE_NAME, RESERVATION_TABLE_NAME,
    RESERVATION_LINK_KEY_SECRET_ARN (verifies the manage token)
    STRIPE_SECRET_ARN, STRIPE_API_VERSION (late-cancellation fee)

    The update carries a "cancelled_by_guest" notice: the guest gets a
    cancellation receipt and the restaurant an email (notification).

Full details: docs/RESERVATIONS.md
"""

import logging

from botocore.exceptions import BotoCoreError, ClientError

from shared import guarantee, http, manage_link, tenant
from shared import reservations as r

logger = logging.getLogger()

_PATH = "/locations/{locationId}/reservations/{reservationId}/cancel"


def _cancel(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="reservations")
    data = http.body(event, allowed={"acceptFee"}, required=False) or {}
    accept_fee = data.get("acceptFee", False)
    if not isinstance(accept_fee, bool):
        raise ValueError("acceptFee must be true or false")
    item = r.load(location_id, http.path_id(event, "reservationId"))
    if not manage_link.matches(item, http.header(event, "x-manage-token")):
        raise r.NotFound
    now = r.now_utc()
    if item["status"] in (r.CANCELLED_BY_GUEST, r.CANCELLED_BY_RESTAURANT):
        return http.respond(200, r.guest_view(item, ctx.location, now=now))
    if item["status"] not in r.ACTIVE:
        raise r.InvalidState("not_cancellable")
    if r.parse_iso(item["bookedFor"]) <= now:
        raise r.InvalidState("already_started")
    fee = guarantee.late_cancel_fee(item, now) if item["status"] == r.RESERVED else 0
    if fee and not accept_fee:
        return http.respond(409, {"error": "fee_applies", "fee": fee,
                                  "currency": item["guarantee"].get("currency", "sek")})

    entry = r.history_entry("cancelled", "guest", now, status=r.CANCELLED_BY_GUEST)
    notice = r.notice(r.NOTICE_CANCELLED_BY_GUEST, now)
    versions = r.read_lock_versions(location_id, item["date"], item.get("tableIds") or [])
    r.transact(
        r.lock_updates(location_id, item["date"], versions)
        + r.release_items(item)
        + guarantee.marker_delete_ops(item)
        + [{
            "Update": {
                "TableName": r.reservation_table(),
                "Key": r.serialize(r.reservation_key(location_id, item["date"], item["reservationId"])),
                "UpdateExpression": "SET #s = :new, updatedAt = :now, updatedBy = :by, "
                                    "cancelledAt = :now, notice = :notice, history = list_append(if_not_exists(history, :empty), :entry), "
                                    "version = if_not_exists(version, :zero) + :one REMOVE pendingExpiresAt",
                "ConditionExpression": "#s = :old",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": r.serialize({
                    ":new": r.CANCELLED_BY_GUEST, ":old": item["status"], ":now": r.iso(now),
                    ":by": "guest", ":notice": notice, ":entry": [entry], ":empty": [], ":zero": 0, ":one": 1,
                }),
            }
        }]
    )
    item = {**item, "status": r.CANCELLED_BY_GUEST, "holdsReleased": True}
    if fee:
        try:
            item = guarantee.run_charge(item, guarantee.KIND_LATE_CANCEL, now, "guest", amount=fee)
        except Exception:  # noqa: BLE001 - the cancellation stands; staff can charge later
            logger.exception("late-cancel fee not started for %s", item["reservationId"])
    return http.respond(200, r.guest_view(item, ctx.location, now=now))


def handler(event, context):
    method, path = http.route(event)
    if path != _PATH:
        return http.error(404, "not found")
    if method != "POST":
        return http.respond(405, {"error": "method not allowed"}, {"Allow": "POST"})
    try:
        return _cancel(event)
    except tenant.TenantError as exc:
        return exc.response()
    except r.NotFound:
        return http.error(404, "not found")
    except r.InvalidState as exc:
        return http.error(409, exc.code)
    except r.Conflict:
        # The booking or a table lock changed meanwhile - ask to retry.
        return http.error(409, "changed_retry")
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError, manage_link.LinkKeyUnavailable):
        return http.error(503, "reservation service unavailable")
