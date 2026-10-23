"""print-device

TRIGGER:
    API Gateway -- Auth: NONE at the gateway; the printer authenticates here
    with HTTP Basic over TLS (user = device id, password = the device's
    secret from admin). Star CloudPRNT protocol:
      POST   /print/cloudprnt/{deviceId}   poll: printer status in,
                                           {"jobReady", "mediaTypes", "jobToken"} out
      GET    /print/cloudprnt/{deviceId}?type=&mac=&token=
                                           the job as StarPRNT bytes (leases it)
      DELETE /print/cloudprnt/{deviceId}?code=&mac=&token=
                                           printed (code 2xx) or failed

PURPOSE:
    The printer's side of kitchen printing (shared/printing.py). Secrets are
    compared by SHA-256 in constant time; the printer's MAC address is bound
    on its first poll and required on every request after that (POST body
    printerMAC, GET/DELETE ?mac=). Failed logins are counted per IP address
    after the check fails (shared/request_guard.py): past the limit the
    answer is 429 instead of 401. The secret is 256 random bits, so the
    counter limits noise and cost, not guessing; counting before every check
    would cost two writes per poll. Firmware
    without Basic auth may send the secret as ?k= instead: the query string
    is never logged (the API stage has no access log; this function logs
    only ids and outcomes).

    Tickets are rendered from the order at fetch time (shared/print_ticket.py),
    so the job itself holds no personal data. An automatic job whose order
    is already completed or cancelled is dropped instead of printed.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    ORDER_TABLE_NAME, PRINT_TABLE_NAME, REQUEST_GUARD_TABLE_NAME

AWS RESOURCE ACCESS:
    Print table GetItem/UpdateItem and Query on its queue/byDevice indexes;
    order table GetItem; request-guard UpdateItem; tenant-context read policy.
"""

import base64
import binascii
import json
import logging
from urllib.parse import unquote

from botocore.exceptions import BotoCoreError, ClientError

from shared import http, print_ticket, request_guard, tenant
from shared import orders as o
from shared import printing as p

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_FAILED_LOGINS = 20
_FAILED_WINDOW = 900


def _basic(event):
    """(user, password) from the Authorization header, or (None, None)."""
    raw = http.header(event, "authorization") or ""
    if not raw.lower().startswith("basic "):
        return None, None
    try:
        user, _, password = base64.b64decode(raw[6:].strip(), validate=True).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return None, None
    return user, password


def _mac(value):
    value = unquote(str(value or "")).strip().lower()
    return value[:32] or None


def _unauthorized(event, reason, device_id):
    logger.warning(json.dumps({"print": "auth_failed", "reason": reason, "deviceId": device_id}))
    try:
        request_guard.check([("print_login", http.source_ip(event), _FAILED_LOGINS, _FAILED_WINDOW)], p.now_utc())
    except request_guard.Limited as exc:
        return http.respond(429, {"error": "too_many_attempts"}, {"Retry-After": str(exc.retry_after)})
    return http.respond(401, {"error": "unauthorized"}, {"WWW-Authenticate": 'Basic realm="booqy"'})


def _authenticate(event, device_id, mac):
    """-> (device, ctx) or an error response."""
    user, secret = _basic(event)
    if secret is None:
        secret = http.query(event).get("k")  # fallback for firmware without Basic auth
        user = device_id
    device = p.find_device(device_id)
    if not device or user != device_id or not p.secret_matches(device, secret):
        return None, _unauthorized(event, "secret", device_id if p.valid_device_id(device_id) else None)
    if device.get("mac") and mac != device["mac"]:
        return None, _unauthorized(event, "mac", device_id)
    ctx = tenant.for_public(device["locationId"], feature="ordering")
    if ctx.tenant_id != device.get("tenantId"):
        return None, _unauthorized(event, "tenant", device_id)
    return (device, ctx), None


def _due_job(device, ctx, settings, now):
    """The first job due for this device; auto jobs of orders that are no
    longer open are dropped on the way."""
    for job in p.queue(ctx.location_id, due_by=p.iso(now)):
        if job.get("status") not in (p.PENDING, p.PRINTING) or not p.for_device(job, device["deviceId"], settings["printerId"]):
            continue
        if job.get("kind") == p.AUTO:
            try:
                order = o.load(ctx.location_id, job.get("orderId"))
            except o.NotFound:
                order = {}
            if order.get("status") not in o.OPEN_STATUSES:
                p.close_job(job, p.CANCELLED, now, expected=job["status"])
                continue
        return job
    return None


def _poll_body(event):
    try:
        return http.body(event, required=False)
    except ValueError:
        return {}


def _poll(event, device, ctx, now):
    data = _poll_body(event)
    p.seen(device, now, mac=_mac(data.get("printerMAC")), status_code=unquote(str(data.get("statusCode") or ""))[:64] or None)
    if data.get("printingInProgress") is True:
        return http.respond(200, {"jobReady": False})
    job = _due_job(device, ctx, p.get_settings(ctx.location_id), now)
    if not job:
        return http.respond(200, {"jobReady": False})
    return http.respond(200, {"jobReady": True, "mediaTypes": [print_ticket.MEDIA_TYPE],
                              "jobToken": p.job_token(job["SK"])})


def _render(job, device, ctx, settings, now):
    width = settings["paperWidth"]
    kind = job.get("kind")
    if kind == p.TEST:
        return print_ticket.test_ticket(ctx.location, device.get("name"), width=width, now=now)
    order = o.load(ctx.location_id, job.get("orderId"))
    if kind == p.CANCEL:
        return print_ticket.cancel_ticket(order, ctx.location, width=width, now=now)
    return print_ticket.order_ticket(order, ctx.location, width=width, copy=kind == p.REPRINT,
                                     retry=int(job.get("attempts") or 0) > 1)


def _fetch(event, device, ctx, now):
    params = http.query(event)
    if params.get("type") and unquote(params["type"]) != print_ticket.MEDIA_TYPE:
        return http.error(415, "unsupported media type")
    settings = p.get_settings(ctx.location_id)
    sk = p.job_sk(params.get("token"))
    job = p.get_job(ctx.location_id, sk) if sk else _due_job(device, ctx, settings, now)
    if not job or not p.for_device(job, device["deviceId"], settings["printerId"]):
        return http.error(404, "no job")
    leased = p.lease(job, device["deviceId"], now)
    if not leased:
        return http.error(404, "no job")
    try:
        body = _render(leased, device, ctx, settings, now)
    except o.NotFound:
        p.close_job(leased, p.CANCELLED, now, expected=p.PRINTING)
        return http.error(404, "no job")
    p.seen(device, now)
    logger.info(json.dumps({"print": "job_sent", "job": p.job_token(leased["SK"]), "attempt": int(leased["attempts"])}))
    return {"statusCode": 200, "isBase64Encoded": True, "body": base64.b64encode(body).decode("ascii"),
            "headers": {"Content-Type": print_ticket.MEDIA_TYPE, "Cache-Control": "no-store"}}


def _confirm(event, device, ctx, now):
    params = http.query(event)
    sk = p.job_sk(params.get("token"))
    job = p.get_job(ctx.location_id, sk) if sk else next(
        (j for j in p.queue(ctx.location_id) if j.get("status") == p.PRINTING and j.get("leasedBy") == device["deviceId"]),
        None)
    if not job:
        return http.respond(200, {"ok": True})
    code = unquote(str(params.get("code") or "200")).strip()
    ok = code.startswith("2") or code.upper() == "OK"
    status = p.confirm(job, device["deviceId"], ok, now)
    logger.info(json.dumps({"print": "job_confirmed", "job": p.job_token(job["SK"]), "code": code[:40], "status": status}))
    return http.respond(200, {"ok": True})


def handler(event, context):
    method, _ = http.route(event)
    device_id = (event.get("pathParameters") or {}).get("deviceId")
    try:
        mac = _mac(http.query(event).get("mac") if method != "POST" else _poll_body(event).get("printerMAC"))
        auth, error = _authenticate(event, device_id, mac)
        if error:
            return error
        device, ctx = auth
        now = p.now_utc()
        if method == "POST":
            return _poll(event, device, ctx, now)
        if method == "GET":
            return _fetch(event, device, ctx, now)
        if method == "DELETE":
            return _confirm(event, device, ctx, now)
        return http.respond(405, {"error": "method not allowed"}, {"Allow": "POST, GET, DELETE"})
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError):
        logger.exception("print device service unavailable")
        return http.error(503, "print service unavailable")
