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
    Epson Server Direct Print (form posts, same Basic auth):
      POST   /print/epson/{deviceId}       ConnectionType=GetRequest: the next
                                           job as ePOS-Print XML (empty if none);
                                           ConnectionType=SetResponse: the
                                           printer's result for printjobid

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

    Tickets are rendered at fetch time (shared/print_ticket.py): a station
    ticket from its TICKET item (shared/routing.py) and the order (name), so
    the job itself holds no personal data; a ticket that was cancelled, or
    whose order is no longer open, is dropped instead of printed; a Kassa job carries its own
    document (receipt, report or kitchen ticket, shared/kassa_receipt.py). An automatic job whose order
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
import re
from urllib.parse import parse_qs, unquote

from botocore.exceptions import BotoCoreError, ClientError

from shared import http, print_ticket, request_guard, routing, tenant
from shared import orders as o
from shared import printing as p

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_FAILED_LOGINS = 20
_FAILED_WINDOW = 900
_EPSON_JOB = re.compile(r"<printjobid>\s*([^<\s]{1,200})\s*</printjobid>")
_EPSON_OK = re.compile(r"<response\b[^>]*\bsuccess\s*=\s*\"(true|false)\"")
_EPSON_CODE = re.compile(r"<response\b[^>]*\bcode\s*=\s*\"([^\"]{0,40})\"")


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
        if job.get("kind") in (p.AUTO, p.STATION, p.ALLERGY) and not _still_wanted(job, ctx):
            p.close_job(job, p.CANCELLED, now, expected=job["status"])
            continue
        return job
    return None


def _still_wanted(job, ctx):
    """An automatic ticket prints only while its order is open and (for a
    station ticket) the ticket was not cancelled."""
    if job.get("ticketSk"):
        ticket = p.get_job(ctx.location_id, job["ticketSk"])
        if not ticket or ticket.get("status") == routing.CANCELLED:
            return False
        if job.get("copy"):
            return True
    if not job.get("orderId"):
        return True
    try:
        return o.load(ctx.location_id, job["orderId"]).get("status") in o.OPEN_STATUSES
    except o.NotFound:
        return False


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


def _render(job, device, ctx, settings, now, fmt="star"):
    width = settings["paperWidth"]
    kind = job.get("kind")
    retry = int(job.get("attempts") or 0) > 1
    if kind == p.TEST:
        return print_ticket.test_ticket(ctx.location, device.get("name"), width=width, now=now, fmt=fmt)
    if kind == p.KASSA:
        return print_ticket.document_ticket(job.get("ticket"), width=width, retry=retry, fmt=fmt)
    if kind in (p.STATION, p.ALLERGY):
        ticket = p.get_job(ctx.location_id, job.get("ticketSk"))
        if not ticket:
            raise o.NotFound
        order = None
        if ticket.get("orderId"):
            try:
                order = o.load(ctx.location_id, ticket["orderId"])
            except o.NotFound:
                order = None
        return print_ticket.station_ticket(ticket, order, ctx.location, width=width, copy=bool(job.get("copy")),
                                           retry=retry, allergy=kind == p.ALLERGY, fmt=fmt)
    order = o.load(ctx.location_id, job.get("orderId"))
    if kind == p.CANCEL:
        return print_ticket.cancel_ticket(order, ctx.location, width=width, now=now, fmt=fmt)
    return print_ticket.order_ticket(order, ctx.location, width=width, copy=kind == p.REPRINT, retry=retry, fmt=fmt)


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


# --- Epson Server Direct Print ----------------------------------------------------

def _form(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", errors="replace")
    return {k: v[0] for k, v in parse_qs(raw[:200_000], keep_blank_values=True).items()}


def _xml(body, status=200):
    return {"statusCode": status, "headers": {"Content-Type": "text/xml; charset=utf-8", "Cache-Control": "no-store"},
            "body": body}


def _epson_get(device, ctx, now):
    p.seen(device, now, status_code="epson")
    settings = p.get_settings(ctx.location_id)
    job = _due_job(device, ctx, settings, now)
    leased = p.lease(job, device["deviceId"], now) if job else None
    if not leased:
        return _xml("")
    try:
        document = _render(leased, device, ctx, settings, now, fmt="epson")
    except o.NotFound:
        p.close_job(leased, p.CANCELLED, now, expected=p.PRINTING)
        return _xml("")
    token = p.job_token(leased["SK"])
    logger.info(json.dumps({"print": "job_sent", "job": token, "attempt": int(leased["attempts"]), "protocol": "epson"}))
    return _xml('<?xml version="1.0" encoding="utf-8"?><PrintRequestInfo Version="2.00"><ePOSPrint><Parameter>'
                f'<devid>local_printer</devid><timeout>10000</timeout><printjobid>{token}</printjobid></Parameter>'
                f'<PrintData>{document}</PrintData></ePOSPrint></PrintRequestInfo>')


def _epson_set(form, device, ctx, now):
    """The printer's results (one <ePOSPrint> per job it printed)."""
    response = form.get("ResponseFile") or ""
    for chunk in response.split("</ePOSPrint>"):
        found = _EPSON_JOB.search(chunk)
        sk = p.job_sk(found.group(1)) if found else None
        job = p.get_job(ctx.location_id, sk) if sk else None
        if not job:
            continue
        ok = (_EPSON_OK.search(chunk) or [None, "false"])[1] == "true"
        status = p.confirm(job, device["deviceId"], ok, now)
        code = (_EPSON_CODE.search(chunk) or [None, ""])[1]
        logger.info(json.dumps({"print": "job_confirmed", "job": p.job_token(job["SK"]), "code": code,
                                "status": status, "protocol": "epson"}))
    return _xml("")


def _epson(event, device_id):
    form = _form(event)
    auth, error = _authenticate(event, device_id, None)
    if error:
        return error
    device, ctx = auth
    now = p.now_utc()
    kind = form.get("ConnectionType")
    if kind == "GetRequest":
        return _epson_get(device, ctx, now)
    if kind == "SetResponse":
        return _epson_set(form, device, ctx, now)
    return http.error(400, "ConnectionType must be GetRequest or SetResponse")


def handler(event, context):
    method, path = http.route(event)
    device_id = (event.get("pathParameters") or {}).get("deviceId")
    try:
        if path.startswith("/print/epson/"):
            if method != "POST":
                return http.respond(405, {"error": "method not allowed"}, {"Allow": "POST"})
            return _epson(event, device_id)
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
