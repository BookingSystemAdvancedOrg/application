"""print-jobs

TRIGGER:
    * DynamoDB Stream, order table (filtered at the event source mapping,
      ReportBatchItemFailures, DLQ print-stream drained by dlq-replay):
        MODIFY pending_payment -> confirmed   the auto job (if auto print is on)
        MODIFY * -> cancelled                  open jobs cancelled; "MAKULERAD"
                                               slip if a ticket may be printed
    * EventBridge Scheduler every 5 minutes, {"source": "sweep"}: jobs out of
      attempts turn FAILED; PrintStuckJobs / PrintFailedJobs metrics.
    * API Gateway -- Auth: JWT (owner_user, staff_user of the location's tenant)
      GET    /locations/{locationId}/print                     settings, printers, status
      GET    /locations/{locationId}/print/status              status only (orders screen)
      PUT    /locations/{locationId}/print/settings            owner: {autoPrint, printerId, paperWidth, version}
      POST   /locations/{locationId}/print/devices             owner: {name} -> device + secret (shown once)
      DELETE /locations/{locationId}/print/devices/{deviceId}  owner: revoke the printer
      POST   /locations/{locationId}/print/devices/{deviceId}/rotate  owner: new secret
      POST   /locations/{locationId}/print/devices/{deviceId}/test    test print
      POST   /locations/{locationId}/print/jobs                {orderId}: "Skriv ut igen"
                                                               (409 order_not_paid, order_cancelled, no_printer)

PURPOSE:
    Creates and manages kitchen print jobs (shared/printing.py); the printer
    fetches them through print-device. Never in the order request path: a
    print problem can't fail or slow an order.
    Multi-tenant: the token's tenant must own the location (else 404) and
    have the "ordering" feature; staff are limited to their own location.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    USER_TABLE_NAME, ORDER_TABLE_NAME, PRINT_TABLE_NAME,
    PRINT_SERVER_URL (<api>/print/cloudprnt, shown with the device id)

AWS RESOURCE ACCESS:
    Print table read/write (and Scan of its queue index); order table
    GetItem; order stream read; tenant-context read policy.
"""

import json
import logging
import os

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from shared import http, printing as p, tenant
from shared import orders as o

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_deserializer = TypeDeserializer()
_ROOT = "/locations/{locationId}/print"
_ITEM = "/locations/{locationId}/print/{proxy+}"


# --- stream ----------------------------------------------------------------------

def _image(raw):
    return {k: _deserializer.deserialize(v) for k, v in (raw or {}).items()}


def _record(record, now):
    new = _image(record.get("dynamodb", {}).get("NewImage"))
    old = _image(record.get("dynamodb", {}).get("OldImage"))
    if not str(new.get("SK", "")).startswith("ORDER#") or not new.get("orderId") or not new.get("locationId"):
        return
    if old.get("status") == o.PENDING_PAYMENT and new.get("status") == o.CONFIRMED:
        created = p.on_order_paid(new, now)
        logger.info(json.dumps({"print": "auto_job", "orderId": new["orderId"], "created": created}))
    elif new.get("status") == o.CANCELLED and old.get("status") != o.CANCELLED:
        slip = p.on_order_cancelled(new, now)
        logger.info(json.dumps({"print": "order_cancelled", "orderId": new["orderId"], "slip": slip}))


def _stream(event):
    failures = []
    now = p.now_utc()
    for record in event.get("Records") or []:
        try:
            _record(record, now)
        except (BotoCoreError, ClientError, KeyError, ValueError):
            logger.exception("print stream record failed")
            seq = (record.get("dynamodb") or {}).get("SequenceNumber")
            if seq:
                failures.append({"itemIdentifier": seq})
    return {"batchItemFailures": failures}


# --- API -------------------------------------------------------------------------

def _server_url():
    return os.environ.get("PRINT_SERVER_URL") or None


def _status(location_id, now):
    settings = p.get_settings(location_id)
    return p.summary(location_id, settings, p.list_devices(location_id), now)


def _overview(location_id, now):
    settings = p.get_settings(location_id)
    devices = p.list_devices(location_id)
    return {
        "settings": settings,
        "devices": [p.device_view(d, now, _server_url()) for d in devices],
        "status": p.summary(location_id, settings, devices, now),
    }


def _owner(ctx):
    if not ctx.is_owner:
        raise tenant.TenantError(403, "owner_only")


def _credentials(device, secret):
    """What the printer's CloudPRNT settings need; the secret only here."""
    return {"device": p.device_view(device, p.now_utc(), _server_url()), "username": device["deviceId"],
            "secret": secret}


def _route(event, method, location_id, ctx):
    now = p.now_utc()
    proxy = ((event.get("pathParameters") or {}).get("proxy") or "").strip("/")
    parts = proxy.split("/") if proxy else []
    allow = None

    if parts == ["status"]:
        if method == "GET":
            return http.respond(200, _status(location_id, now))
        allow = "GET"
    elif parts == ["settings"]:
        if method == "PUT":
            _owner(ctx)
            data = http.body(event, allowed={"autoPrint", "printerId", "paperWidth", "version"})
            ids = {d["deviceId"] for d in p.list_devices(location_id)}
            saved = p.put_settings(ctx.tenant_id, location_id, p.validate_settings(data, ids), ctx.sub, now)
            return http.respond(200, saved)
        allow = "PUT"
    elif parts == ["devices"]:
        if method == "POST":
            _owner(ctx)
            data = http.body(event, allowed={"name"})
            device, secret = p.add_device(ctx.tenant_id, location_id, data.get("name"), ctx.sub, now)
            logger.info(json.dumps({"print": "device_added", "deviceId": device["deviceId"]}))
            return http.respond(201, _credentials(device, secret))
        allow = "POST"
    elif len(parts) == 2 and parts[0] == "devices":
        if method == "DELETE":
            _owner(ctx)
            p.remove_device(location_id, parts[1], now)
            logger.info(json.dumps({"print": "device_removed", "deviceId": parts[1]}))
            return http.respond(200, _overview(location_id, now))
        allow = "DELETE"
    elif len(parts) == 3 and parts[0] == "devices" and parts[2] == "rotate":
        if method == "POST":
            _owner(ctx)
            secret = p.rotate_device(location_id, parts[1], ctx.sub, now)
            logger.info(json.dumps({"print": "device_rotated", "deviceId": parts[1]}))
            return http.respond(200, _credentials(p.get_device(location_id, parts[1]), secret))
        allow = "POST"
    elif len(parts) == 3 and parts[0] == "devices" and parts[2] == "test":
        if method == "POST":
            job = p.test_job(ctx.tenant_id, location_id, parts[1], now)
            return http.respond(202, {"jobId": p.job_token(job["SK"]), "status": job["status"]})
        allow = "POST"
    elif parts == ["jobs"]:
        if method == "POST":
            data = http.body(event, allowed={"orderId"})
            order = o.load(location_id, data.get("orderId"))
            if order.get("tenantId") != ctx.tenant_id:
                raise o.NotFound
            if order.get("status") in (o.PENDING_PAYMENT, o.FAILED):
                raise p.Conflict("order_not_paid")
            if order.get("status") == o.CANCELLED:
                raise p.Conflict("order_cancelled")
            job = p.reprint(ctx.tenant_id, location_id, order["orderId"], now)
            return http.respond(202, {"jobId": p.job_token(job["SK"]), "status": job["status"]})
        allow = "POST"
    else:
        return http.error(404, "not found")
    return http.respond(405, {"error": "method not allowed"}, {"Allow": allow})


def _api(event):
    method, path = http.route(event)
    try:
        location_id = http.path_id(event, "locationId")
        ctx = tenant.for_jwt(event, location_id=location_id, feature="ordering")
        if path == _ROOT:
            if method != "GET":
                return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET"})
            return http.respond(200, _overview(location_id, p.now_utc()))
        if path == _ITEM:
            return _route(event, method, location_id, ctx)
        return http.error(404, "not found")
    except tenant.TenantError as exc:
        return exc.response()
    except (p.NotFound, o.NotFound):
        return http.error(404, "not found")
    except p.Conflict as exc:
        return http.error(409, exc.code)
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError):
        logger.exception("print service unavailable")
        return http.error(503, "print service unavailable")


def handler(event, context):
    if isinstance(event, dict) and "Records" in event:
        return _stream(event)
    if isinstance(event, dict) and event.get("source") == "sweep":
        counts = p.sweep(p.now_utc())
        logger.info(json.dumps({"print": "sweep", **counts}))
        return counts
    return _api(event)
