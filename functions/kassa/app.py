"""kassa

TRIGGER:
    API Gateway -- Auth: JWT (owner_user, staff_user of the location's tenant)
      GET  /locations/{locationId}/kassa                     state: registers, open bills, test mode
      GET|POST|PUT /locations/{locationId}/kassa/{proxy+}:
        POST registers                        {label}                  owner: a new register
        GET  registers/{r}                    the register, its control system and day status
        POST registers/{r}/open-day           {float}                  växelkassa at day start
        POST registers/{r}/training           {on}                     training mode on/off
        POST registers/{r}/x | z              {print?}                 X report / Z report
        GET  registers/{r}/receipts?date=     the day's receipts and reports (journal)
        GET  registers/{r}/receipts/{seq}     one receipt, what is returned, copy taken
        POST registers/{r}/receipts/{seq}/copy    {delivery, email?}   the one copy
        POST registers/{r}/receipts/{seq}/return  {lines, reason, delivery, email?}
        GET  registers/{r}/export?from=&to=   owner: journal XML (SKVFS 2021:16)
        POST registers/{r}/simulate           {down}   test mode only: control system down/up
        POST registers/{r}/outage-notice      {action: shown|dismissed}  audit: felanmälan banner
        GET  setup                            "Kom igång med kassan" checklist
        PUT  setup                            {item, done}             owner: tick an item
        GET  manual                           the user manual (docs/kassa/BRUKSANVISNING.md)
        GET  bills                            open bills
        POST bills                            {registerId, kind, tableLabel?}
        GET  bills/{b}
        PUT  bills/{b}                        {version, lines, discount}
        POST bills/{b}/send                   kitchen ticket for new lines
        POST bills/{b}/void                   throw away an unpaid bill
        POST bills/{b}/payments               {checkoutId, readerId, amount}: card on a reader
        POST bills/{b}/payments/{p}/undo      a paid part back to the card (before the receipt)
        POST bills/{b}/finalize               {delivery, email?}: control code, receipt

PURPOSE:
    Kassa, the till (shared/kassa.py, docs/KASSA-REQUIREMENTS.md): bills per
    table or takeaway, card payments on the restaurant's Stripe Terminal
    readers (polling, cancel and retry stay on terminal-payments), receipts
    with a control code, returns, one copy, training mode, X/Z reports and
    the journal export. Behind the per-tenant "kassa" feature (off by
    default). Until a certified control system is connected and the label is
    switched (KASSA_CERTIFIED), everything says "Testläge".

    Multi-tenant: the token's tenant must own the location (else 404) and
    have the "kassa" feature; staff are limited to their own location.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    USER_TABLE_NAME, KASSA_TABLE_NAME, KASSA_JOURNAL_TABLE_NAME, MENU_TABLE_NAME,
    ORDER_TABLE_NAME, PRINT_TABLE_NAME, STRIPE_SECRET_ARN, STRIPE_API_VERSION,
    KASSA_CERTIFIED ("true" only with a certified control system),
    KASSA_CONTROL_SYSTEM (simulator | fiskaly), FISKALY_SECRET_ID

AWS RESOURCE ACCESS:
    Kassa table GetItem/PutItem/UpdateItem/Query; journal table PutItem/
    GetItem/Query only (append-only, no Update/Delete); menu table GetItem/
    BatchGetItem; order table GetItem/PutItem/UpdateItem/Query (card payment
    records, receipt e-mail notices); print table GetItem/PutItem (Kassa
    print jobs); the platform Stripe key; the fiskaly SIGN SE key (one
    secret, when the environment uses fiskaly); tenant-context read policy.
"""

import json
import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from shared import card_payments as cp
from shared import control_system as cs
from shared import http, kassa, kassa_receipt, kassa_xml, printing, tenant
from shared import orders as o
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ROOT = "/locations/{locationId}/kassa"
_PROXY = "/locations/{locationId}/kassa/{proxy+}"
_FEATURE = "kassa"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MAX_EXPORT_DAYS = 366


class _MethodNotAllowed(Exception):
    def __init__(self, allow):
        super().__init__(allow)
        self.allow = allow


def _allow(method, *allowed):
    if method not in allowed:
        raise _MethodNotAllowed(", ".join(allowed))


def _ctx(event):
    location_id = http.path_id(event, "locationId")
    return tenant.for_jwt(event, location_id=location_id, feature=_FEATURE)


def _owner(ctx):
    if not ctx.is_owner:
        raise tenant.TenantError(403, "owner_only")


def _whole_seq(text):
    if not text.isdigit() or len(text) > 12 or int(text) < 1:
        raise kassa.NotFound
    return int(text)


def _bill_summary(ctx, bill):
    priced = kassa.price(bill)
    return {"billId": bill["billId"], "registerId": bill.get("registerId"), "kind": bill.get("kind"),
            "tableLabel": bill.get("tableLabel"), "training": bool(bill.get("training")),
            "lines": sum(int(line.get("quantity") or 0) for line in bill.get("lines") or []),
            "total": priced["total"], "createdAt": bill.get("createdAt")}


def _checked(ctx, system, register, now, user):
    register, status = kassa.check_control_system(ctx, register, system, now=now, user=user)
    return kassa.register_view(register, system, ctx.location, status)


def _company(ctx):
    try:
        return kassa.company(ctx.tenant, ctx.location)
    except kassa.Conflict:
        return None


def _manual():
    """The manual ships in the image next to app.py; tests read it from docs."""
    for path in (Path(__file__).parent / "BRUKSANVISNING.md",
                 Path(__file__).parents[2] / "docs" / "kassa" / "BRUKSANVISNING.md"):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise kassa.NotFound


def _state(ctx, system):
    registers = kassa.list_registers(ctx.location_id)
    terminal = (ctx.location.get("terminal") or {})
    now, user = o.now_utc(), kassa.user_of(ctx)
    return {
        "testMode": kassa.test_mode(system), "testNotice": kassa_receipt.TEST_NOTICE,
        "company": _company(ctx), "felanmalAfterMinutes": kassa.FELANMAL_AFTER_MINUTES,
        "registers": [_checked(ctx, system, r, now, user) for r in registers],
        "openBills": [_bill_summary(ctx, b) for b in kassa.list_open_bills(ctx.location_id)],
        "printer": bool(printing.get_settings(ctx.location_id).get("printerId")),
        "cardReaders": bool(terminal.get("locationId")) and bool(
            ((ctx.tenant.get("entitlements") or {}).get("features") or {}).get("terminal")),
        "isOwner": ctx.is_owner, "simulator": system.provider == cs.SIMULATOR,
        "canSimulate": os.environ.get("ENVIRONMENT") != "prod" and system.provider == cs.SIMULATOR,
    }


def _done(entry, delivered):
    return {"entry": {"jseq": int(entry["jseq"]), "type": entry["type"], "at": entry["at"]},
            "document": delivered["document"], "problems": delivered["problems"]}


def _report(event, ctx, register_id, kind, now, user, system):
    data = http.body(event, allowed={"print"}, required=False)
    entry = kassa.report(ctx, register_id, kind, user=user, now=now, tenant_row=ctx.tenant, system=system)
    document = kassa_receipt.document(entry, ctx.location)
    problems = ["control_system_closing_failed"] if entry.get("closing") is False else []
    if data.get("print") is True:
        try:
            printing.kassa_job(ctx.tenant_id, ctx.location_id, document, now)
        except printing.Conflict:
            problems.append("no_printer")
    return http.respond(200, {"document": document, "problems": problems})


def _receipts(event, ctx, register_id, now):
    params = http.query(event)
    if set(params) - {"date"}:
        raise ValueError("only date is supported")
    day = params.get("date") or kassa.local(now, ctx.location).date().isoformat()
    if not _DATE.fullmatch(day):
        raise ValueError("date must be YYYY-MM-DD")
    kassa.get_register(ctx, register_id)
    out = []
    for entry in kassa.journal_day(register_id, day):
        if entry.get("type") in (kassa.SALE, kassa.RETURN, kassa.COPY, kassa.TRAINING, kassa.XREPORT, kassa.ZREPORT):
            doc = kassa_receipt.document(entry, ctx.location)
            out.append({"jseq": doc["jseq"], "kind": doc["kind"], "title": doc["title"], "seq": doc.get("seq"),
                        "total": doc.get("total"), "at": entry.get("local")})
    return http.respond(200, {"date": day, "entries": list(reversed(out))})


def _receipt(event, method, ctx, register_id, seq, action, now, user, system):
    pointer = kassa.receipt_pointer(ctx, register_id, seq)
    if action is None:
        _allow(method, "GET")
        entry = kassa.journal_entry(register_id, pointer["jseq"])
        return http.respond(200, {
            "document": kassa_receipt.document(entry, ctx.location),
            "lines": [{k: kassa._plain(line.get(k)) for k in ("lineId", "name", "quantity", "total")}
                      for line in entry["data"].get("lines") or []],
            "returned": kassa._plain(pointer.get("returned") or {}), "copyTaken": bool(pointer.get("copyJseq"))})
    _allow(method, "POST")
    if action == "copy":
        data = http.body(event, allowed={"delivery", "email"})
        entry, delivered = kassa.copy_receipt(ctx, register_id, seq, delivery=data.get("delivery"),
                                              email=data.get("email"), user=user, now=now, system=system,
                                              tenant_row=ctx.tenant)
        logger.info(json.dumps({"kassa": "copy", "register": register_id, "seq": seq}))
        return http.respond(201, _done(entry, delivered))
    if action == "return":
        data = http.body(event, allowed={"lines", "reason", "delivery", "email"})
        entry, delivered = kassa.return_receipt(ctx, register_id, seq, lines=data.get("lines"),
                                                reason=data.get("reason"), delivery=data.get("delivery"),
                                                email=data.get("email"), user=user, now=now, system=system,
                                                tenant_row=ctx.tenant)
        logger.info(json.dumps({"kassa": "return", "register": register_id, "seq": seq}))
        return http.respond(201, _done(entry, delivered))
    return http.error(404, "not found")


def _export(event, ctx, register_id, now, user, system):
    _owner(ctx)
    params = http.query(event)
    if set(params) - {"from", "to"}:
        raise ValueError("only from and to are supported")
    try:
        start = date.fromisoformat(params.get("from") or "")
        end = date.fromisoformat(params.get("to") or "")
    except ValueError:
        raise ValueError("from and to must be YYYY-MM-DD") from None
    if end < start or (end - start).days >= _MAX_EXPORT_DAYS:
        raise ValueError(f"the period must be 1 to {_MAX_EXPORT_DAYS} days")
    register = kassa.get_register(ctx, register_id)
    entries, day = [], start
    while day <= end:
        entries += kassa.journal_day(register_id, day.isoformat())
        day += timedelta(days=1)
    body = kassa_xml.export(register=register, entries=entries, company=kassa.company(ctx.tenant, ctx.location),
                            location=ctx.location, start=f"{start}T00:00:00", end=f"{end}T23:59:59", user=user, now=now,
                            control_system_serial=system.serial)
    name = f"journal-{re.sub(r'[^A-Za-z0-9-]', '-', register['label'])}-{start}-{end}.xml"
    logger.info(json.dumps({"kassa": "export", "register": register_id, "entries": len(entries)}))
    return {"statusCode": 200, "body": body.decode("utf-8"),
            "headers": {"Content-Type": "application/xml; charset=utf-8", "Cache-Control": "no-store",
                        "Content-Disposition": f'attachment; filename="{name}"'}}


def _register_route(event, method, ctx, parts, now, user, system):
    register_id = parts[1]
    if len(parts) == 2:
        _allow(method, "GET")
        register = kassa.get_register(ctx, register_id)
        return http.respond(200, {"register": _checked(ctx, system, register, now, user),
                                  "day": kassa.day_status(ctx, register, now)})
    action = parts[2]
    if action == "receipts" and len(parts) == 3:
        _allow(method, "GET")
        return _receipts(event, ctx, register_id, now)
    if action == "receipts" and len(parts) in (4, 5):
        return _receipt(event, method, ctx, register_id, _whole_seq(parts[3]), parts[4] if len(parts) == 5 else None,
                        now, user, system)
    if action == "export" and len(parts) == 3:
        _allow(method, "GET")
        return _export(event, ctx, register_id, now, user, system)
    if len(parts) != 3:
        return http.error(404, "not found")
    _allow(method, "POST")
    if action in ("x", "z"):
        return _report(event, ctx, register_id, kassa.XREPORT if action == "x" else kassa.ZREPORT, now, user,
                       system)
    if action == "open-day":
        data = http.body(event, allowed={"float"})
        kassa.open_day(ctx, register_id, data.get("float"), user=user, now=now, system=system)
    elif action == "training":
        data = http.body(event, allowed={"on"})
        kassa.set_training(ctx, register_id, data.get("on"), user=user, now=now)
    elif action == "outage-notice":
        data = http.body(event, allowed={"action"})
        kassa.outage_notice(ctx, register_id, data.get("action"), user=user, now=now)
    elif action == "simulate":
        if os.environ.get("ENVIRONMENT") == "prod" or system.provider != cs.SIMULATOR:
            return http.error(404, "not found")
        data = http.body(event, allowed={"down"})
        kassa.simulate(ctx, register_id, data.get("down"))
    else:
        return http.error(404, "not found")
    return http.respond(200, {"register": _checked(ctx, system, kassa.get_register(ctx, register_id), now, user)})


def _bill_route(event, method, ctx, parts, now, user, system):
    if len(parts) == 1:
        _allow(method, "GET", "POST")
        if method == "GET":
            return http.respond(200, {"bills": [_bill_summary(ctx, b) for b in kassa.list_open_bills(ctx.location_id)]})
        data = http.body(event, allowed={"registerId", "kind", "tableLabel"})
        bill = kassa.create_bill(ctx, data.get("registerId"), data.get("kind"), data.get("tableLabel"), now=now)
        return http.respond(201, kassa.bill_view(ctx, bill, []))
    bill_id = parts[1]
    if len(parts) == 2:
        _allow(method, "GET", "PUT")
        if method == "GET":
            return http.respond(200, kassa.bill_view(ctx, kassa.get_bill(ctx, bill_id)))
        data = http.body(event, allowed={"version", "lines", "discount"})
        expected = o._whole(data.get("version"), 1, 10 ** 9, "version")
        bill = kassa.update_bill(ctx, bill_id, expected_version=expected, lines=data.get("lines"),
                                 discount=data.get("discount"), user=user, now=now)
        return http.respond(200, kassa.bill_view(ctx, bill, []))
    _allow(method, "POST")
    action = parts[2]
    if action == "send" and len(parts) == 3:
        http.body(event, allowed=set(), required=False)
        return http.respond(200, kassa.bill_view(ctx, kassa.send_to_kitchen(ctx, bill_id, now=now)))
    if action == "void" and len(parts) == 3:
        http.body(event, allowed=set(), required=False)
        kassa.void_bill(ctx, bill_id, user=user, now=now)
        return http.respond(200, {"billId": bill_id, "status": kassa.VOID})
    if action == "payments" and len(parts) == 3:
        data = http.body(event, allowed={"checkoutId", "readerId", "amount"})
        item = kassa.start_payment(ctx, bill_id, checkout_id=data.get("checkoutId"), reader_id=data.get("readerId"),
                                   amount=data.get("amount"), system=system, now=now)
        logger.info(json.dumps({"kassa": "payment", "bill": bill_id, "paymentId": item["paymentId"],
                                "status": item.get("status")}))
        return http.respond(201, cp.view(item))
    if action == "payments" and len(parts) == 5 and parts[4] == "undo":
        http.body(event, allowed=set(), required=False)
        return http.respond(200, cp.view(kassa.undo_payment(ctx, bill_id, parts[3], now=now)))
    if action == "finalize" and len(parts) == 3:
        data = http.body(event, allowed={"delivery", "email"})
        entry, delivered = kassa.finalize(ctx, bill_id, delivery=data.get("delivery"), email=data.get("email"),
                                          user=user, now=now, system=system, tenant_row=ctx.tenant)
        logger.info(json.dumps({"kassa": "receipt", "bill": bill_id, "jseq": int(entry["jseq"]),
                                "type": entry["type"], "problems": delivered["problems"]}))
        return http.respond(201, _done(entry, delivered))
    return http.error(404, "not found")


def _proxy(event, method):
    ctx = _ctx(event)
    system = cs.for_location(ctx.location)
    now, user = o.now_utc(), kassa.user_of(ctx)
    proxy = ((event.get("pathParameters") or {}).get("proxy") or "").strip("/")
    parts = proxy.split("/") if proxy else []
    if parts == ["manual"]:
        _allow(method, "GET")
        return http.respond(200, {"markdown": _manual()})
    if parts == ["setup"]:
        _allow(method, "GET", "PUT")
        if method == "PUT":
            _owner(ctx)
            data = http.body(event, allowed={"item", "done"})
            return http.respond(200, {"items": kassa.set_setup_item(ctx, data.get("item"), data.get("done"),
                                                                    user=user, now=now)})
        return http.respond(200, {"items": kassa.get_setup(ctx)})
    if parts == ["registers"]:
        _allow(method, "GET", "POST")
        if method == "GET":
            return http.respond(200, {"registers": [kassa.register_view(r, system, ctx.location)
                                                    for r in kassa.list_registers(ctx.location_id)]})
        _owner(ctx)
        data = http.body(event, allowed={"label"})
        entry = kassa.create_register(ctx, data.get("label"), user=user, now=now, system=system,
                                      tenant_row=ctx.tenant)
        register = kassa.get_register(ctx, entry["registerId"])
        return http.respond(201, {"register": kassa.register_view(register, system, ctx.location)})
    if len(parts) >= 2 and parts[0] == "registers" and kassa.valid_id(parts[1]):
        return _register_route(event, method, ctx, parts, now, user, system)
    if parts and parts[0] == "bills" and (len(parts) == 1 or kassa.valid_id(parts[1])):
        return _bill_route(event, method, ctx, parts, now, user, system)
    return http.error(404, "not found")


def handler(event, context):
    method, path = http.route(event)
    try:
        if path == _ROOT:
            _allow(method, "GET")
            ctx = _ctx(event)
            return http.respond(200, _state(ctx, cs.for_location(ctx.location)))
        if path == _PROXY:
            return _proxy(event, method)
        return http.error(404, "not found")
    except tenant.TenantError as exc:
        return exc.response()
    except _MethodNotAllowed as exc:
        return http.respond(405, {"error": "method not allowed"}, {"Allow": exc.allow})
    except (kassa.NotFound, cp.NotFound, o.NotFound):
        return http.error(404, "not found")
    except kassa.Conflict as exc:
        return http.error(409, exc.code)
    except (cp.InvalidState, printing.Conflict) as exc:
        return http.error(409, exc.code)
    except kassa.Unavailable as exc:
        return http.error(503, exc.code)
    except stripe.StripeError as exc:
        logger.error(json.dumps({"stripe": "rejected", "path": path, "code": exc.code, "message": str(exc)}))
        return http.error(502, "stripe_refused")
    except stripe.StripeUnavailable:
        return http.error(503, "payment_unavailable")
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError):
        logger.exception("kassa unavailable")
        return http.error(503, "service unavailable")
