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
      GET    /locations/{locationId}/print                     settings, printers, status,
                                                               simulatorAllowed (not in prod)
      GET    /locations/{locationId}/print/status              status only (orders screen)
      PUT    /locations/{locationId}/print/settings            owner: {autoPrint, printerId, paperWidth, version}
      POST   /locations/{locationId}/print/devices             owner or Beställningar staff: {name, kind?, protocol?}
                                                               -> device + secret (shown once); protocol "sim"
                                                               outside prod: no secret
      PUT    /locations/{locationId}/print/devices/{deviceId}  owner or Beställningar staff: {name?, kind?}
                                                               (kind kok|dryck|annat moves its tickets)
      DELETE /locations/{locationId}/print/devices/{deviceId}  owner: revoke the printer
      GET    /locations/{locationId}/print/devices/{deviceId}/simulated  a simulated printer's last tickets,
                                                               rendered now (never stored with personal data)
      POST   /locations/{locationId}/print/setup               owner: {skipped}: "Inga skrivare än" in Kom igång
      PUT    /locations/{locationId}/print/routing/categories  owner or Meny staff: {categories:
                                                               {categoryId: stationId|"none"|null}}
      POST   /locations/{locationId}/print/devices/{deviceId}/rotate  owner: new secret
      POST   /locations/{locationId}/print/devices/{deviceId}/test    owner or Beställningar staff: test print
      POST   /locations/{locationId}/print/jobs                {orderId, stationId?}: "Skriv ut igen"
                                                               (409 order_not_paid, order_cancelled, no_printer)
      GET    /locations/{locationId}/print/routing             stations, category routing, allergy ticket
      PUT    /locations/{locationId}/print/routing             owner: the same with version
      GET    /locations/{locationId}/print/tickets             the kitchen display: last day's tickets
      PUT    /locations/{locationId}/print/tickets/{src}/{station}        {status}: sent|preparing|ready
      POST   /locations/{locationId}/print/tickets/{src}/{station}/print  the ticket again on its printers

PURPOSE:
    Creates and manages kitchen print jobs (shared/printing.py); the printer
    fetches them through print-device. Never in the order request path: a
    print problem can't fail or slow an order.
    Multi-tenant: the token's tenant must own the location (else 404) and
    have the "ordering" feature; staff are limited to their own location.
    Staff with Beställningar may add printers, rename them, change their
    kind and test print; removing a printer, a new secret, the settings
    and "Inga skrivare än" stay the owner's. Booqy Console reads and
    changes the same rows (booqy-console api, app/printers.py).

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

from shared import http, print_ticket, printing as p, routing, tenant
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
    settings = p.get_settings(location_id, setup=True)
    return p.summary(location_id, settings, p.list_devices(location_id), now)


def _routing(location_id, settings, devices):
    """The config as the screen edits it: only printers that still exist."""
    config = routing.get_config(location_id, settings["printerId"])
    names = {d["deviceId"]: d.get("name") or "" for d in devices}
    for station in config["stations"]:
        station["printerIds"] = [d for d in station["printerIds"] if d in names]
        station["kind"] = routing.station_kind(station["stationId"])
        station["printerNames"] = [names[d] for d in station["printerIds"]]  # Meny cannot read the printers
    return config


def _overview(location_id, now):
    settings = p.get_settings(location_id, setup=True)
    devices = p.list_devices(location_id)
    config = _routing(location_id, settings, devices)
    views = []
    for d in devices:
        view = p.device_view(d, now, _server_url())
        view["kind"] = routing.infer_kind(d, config)  # a printer from before kinds shows what it does
        views.append(view)
    return {
        "settings": {k: v for k, v in settings.items() if k != "setupSkipped"},
        "devices": views,
        "status": p.summary(location_id, settings, devices, now),
        "routing": config,
        "simulatorAllowed": p.simulator_allowed(),
    }


def _owner(ctx):
    if not ctx.is_owner:
        raise tenant.TenantError(403, "owner_only")


def _credentials(device, secret):
    """What the printer's CloudPRNT settings need; the secret only here."""
    if secret is None:  # a simulated printer has nothing to type in
        return {"device": p.device_view(device, p.now_utc())}
    return {"device": p.device_view(device, p.now_utc(), _server_url()), "username": device["deviceId"],
            "secret": secret}


def _simulated(location_id, device_id, ctx):
    """A simulated printer's paper: its newest tickets as rows, rendered from
    the ticket and order now (like a real printer at fetch time)."""
    device = p.get_device(location_id, device_id)
    if not p.is_sim(device):
        raise p.NotFound
    width = p.get_settings(location_id)["paperWidth"]
    out = []
    for row, job in p.simulated(location_id, device_id):
        kind = job.get("kind")
        try:
            if kind == p.TEST:
                lines = print_ticket.test_ticket(ctx.location, device.get("name"), width=width,
                                                 now=p.parse_iso(job["createdAt"]), fmt="text")
            elif kind in (p.STATION, p.ALLERGY):
                ticket = p.get_job(location_id, job.get("ticketSk"))
                if not ticket:
                    continue
                order = None
                if ticket.get("orderId"):
                    try:
                        order = o.load(location_id, ticket["orderId"])
                    except o.NotFound:
                        order = None
                lines = print_ticket.station_ticket(ticket, order, ctx.location, width=width, copy=bool(job.get("copy")),
                                                    allergy=kind == p.ALLERGY, fmt="text")
            elif kind == p.CANCEL:
                lines = print_ticket.cancel_ticket(o.load(location_id, job.get("orderId")), ctx.location, width=width,
                                                   now=p.parse_iso(job["createdAt"]), fmt="text")
            else:
                continue
        except o.NotFound:
            continue
        out.append({"jobId": p.job_token(job["SK"]), "kind": kind, "printedAt": row["createdAt"], "lines": lines})
    return {"device": p.device_view(device, p.now_utc()), "paperWidth": width, "tickets": out}


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
            data = http.body(event, allowed={"name", "protocol", "kind"})
            device, secret = p.add_device(ctx.tenant_id, location_id, data.get("name"), ctx.sub, now,
                                          data.get("protocol") or "star", data.get("kind"))
            logger.info(json.dumps({"print": "device_added", "deviceId": device["deviceId"],
                                    "protocol": device["protocol"], "kind": device.get("kind"), "role": ctx.role}))
            return http.respond(201, _credentials(device, secret))
        allow = "POST"
    elif len(parts) == 2 and parts[0] == "devices":
        if method == "PUT":
            data = http.body(event, allowed={"name", "kind"})
            if not data:
                raise ValueError("name or kind is required")
            p.update_device(ctx.tenant_id, location_id, parts[1], data, ctx.sub, now)
            logger.info(json.dumps({"print": "device_changed", "deviceId": parts[1], "fields": sorted(data),
                                    "role": ctx.role}))
            return http.respond(200, _overview(location_id, now))
        if method == "DELETE":
            _owner(ctx)
            p.remove_device(location_id, parts[1], now)
            logger.info(json.dumps({"print": "device_removed", "deviceId": parts[1]}))
            return http.respond(200, _overview(location_id, now))
        allow = "PUT, DELETE"
    elif len(parts) == 3 and parts[0] == "devices" and parts[2] == "simulated":
        if method == "GET":
            return http.respond(200, _simulated(location_id, parts[1], ctx))
        allow = "GET"
    elif parts == ["setup"]:
        if method == "POST":
            _owner(ctx)
            data = http.body(event, allowed={"skipped"})
            p.set_setup_skipped(ctx.tenant_id, location_id, data.get("skipped"), ctx.sub, now)
            return http.respond(200, _status(location_id, now))
        allow = "POST"
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
            data = http.body(event, allowed={"orderId", "stationId"})
            order = o.load(location_id, data.get("orderId"))
            if order.get("tenantId") != ctx.tenant_id:
                raise o.NotFound
            if order.get("status") in (o.PENDING_PAYMENT, o.FAILED):
                raise p.Conflict("order_not_paid")
            if order.get("status") == o.CANCELLED:
                raise p.Conflict("order_cancelled")
            jobs = p.reprint(ctx.tenant_id, location_id, order["orderId"], now, data.get("stationId"))
            return http.respond(202, {"jobIds": [p.job_token(j["SK"]) for j in jobs], "jobId": p.job_token(jobs[0]["SK"]),
                                      "status": jobs[0]["status"]})
        allow = "POST"
    elif parts == ["routing"]:
        settings, devices = p.get_settings(location_id), p.list_devices(location_id)
        if method == "GET":
            return http.respond(200, _routing(location_id, settings, devices))
        if method == "PUT":
            _owner(ctx)
            data = http.body(event, allowed={"stations", "categories", "allergyTicket", "version"})
            config = routing.validate_config(data, {d["deviceId"] for d in devices})
            routing.put_config(ctx.tenant_id, location_id, config, ctx.sub, now)
            return http.respond(200, _routing(location_id, settings, devices))
        allow = "GET, PUT"
    elif parts == ["routing", "categories"]:
        if method == "PUT":
            # Owner, or staff with Meny: where a whole category's dishes go.
            data = http.body(event, allowed={"categories"})
            routing.set_categories(ctx.tenant_id, location_id, data.get("categories"),
                                   p.get_settings(location_id)["printerId"], ctx.sub, now)
            return http.respond(200, _routing(location_id, p.get_settings(location_id), p.list_devices(location_id)))
        allow = "PUT"
    elif parts == ["tickets"]:
        if method == "GET":
            return http.respond(200, {"tickets": [routing.view(t) for t in routing.list_tickets(location_id, now)]})
        allow = "GET"
    elif len(parts) == 3 and parts[0] == "tickets":
        if method == "PUT":
            data = http.body(event, allowed={"status"})
            ticket = routing.set_status(location_id, parts[1], parts[2], data.get("status"), ctx.sub, now)
            return http.respond(200, routing.view(ticket))
        allow = "PUT"
    elif len(parts) == 4 and parts[0] == "tickets" and parts[3] == "print":
        if method == "POST":
            ticket = p.get_job(location_id, f"TICKET#{parts[1]}#{parts[2]}") if "#" not in parts[1] + parts[2] else None
            if not ticket or ticket.get("status") == routing.CANCELLED:
                raise p.NotFound
            settings = p.get_settings(location_id)
            config = routing.get_config(location_id, settings["printerId"])
            jobs = p.ticket_jobs(ctx.tenant_id, location_id, ticket, config, now, copy=True)
            if not jobs:
                raise p.Conflict("no_printer")
            return http.respond(202, {"jobIds": [p.job_token(j["SK"]) for j in jobs]})
        allow = "POST"
    else:
        return http.error(404, "not found")
    return http.respond(405, {"error": "method not allowed"}, {"Allow": allow})


def _api(event):
    method, path = http.route(event)
    try:
        location_id = http.path_id(event, "locationId")
        proxy = ((event.get("pathParameters") or {}).get("proxy") or "").strip("/")
        # Meny shows each dish's station (GET routing) and sets where a whole
        # category goes (PUT routing/categories); the rest is Beställningar.
        menu_path = (method == "GET" and proxy == "routing") or proxy == "routing/categories"
        area = ("bestallningar", "meny") if menu_path else "bestallningar"
        ctx = tenant.for_jwt(event, location_id=location_id, feature="ordering", area=area)
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
