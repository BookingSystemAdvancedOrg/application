"""accounting

TRIGGER:
    API Gateway -- Auth: JWT (tenant pool, owner only)
    GET /locations/{locationId}/accounting
        The location's account mapping (Bokföring): every role's account,
        the BAS defaults and names, the SIE series and the first month of
        the fiscal year.
    PUT /locations/{locationId}/accounting
        {accounts: {role: "nnnn"}, series, fiscalYearStartMonth, version}.
        Accounts are 4 digits (1000-8999); only the ones differing from the
        defaults are stored, so sending the defaults resets. 409
        changed_retry when someone saved first.
    GET /locations/{locationId}/accounting/export?from=&to=&format=
        One balanced verifikation per business day of the period (at most
        366 days), as format=sie (SIE type 4, PC8), csv or json. Files come
        back base64 in {filename, mediaType, content}, with a summary and
        the warnings for the accountant.

PURPOSE:
    The accounting export (shared/accounting.py, shared/accounting_export.py,
    docs/ACCOUNTING-EXPORT.md). Read only on the money: entries are built
    from the stored orders, card payments, Kassa journal, card guarantee
    fees, catering requests and the connected account's Stripe balance
    transactions every time, so exporting a day again gives the same entry.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    USER_TABLE_NAME, ORDER_TABLE_NAME, RESERVATION_TABLE_NAME,
    CATERING_REQUESTS_TABLE_NAME, CATERING_REQUEST_HISTORY_TABLE_NAME,
    KASSA_TABLE_NAME, KASSA_JOURNAL_TABLE_NAME, STRIPE_SECRET_ARN, STRIPE_API_VERSION

AWS RESOURCE ACCESS:
    Tenant-context read policy; location table UpdateItem (`accounting`
    only); Query on the order, reservation, catering-requests,
    catering-request-history and kassa tables and the kassa-journal byDay
    index; GetItem on catering-request-history (the signed offer);
    the platform Stripe secret (balance transactions, read only).
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

from botocore.exceptions import BotoCoreError, ClientError

from shared import accounting as acc
from shared import accounting_export as ax
from shared import dynamo, http, tenant
from shared import stripe_client as stripe
from shared.order_messages import _seller

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ROOT = "/locations/{locationId}/accounting"
_EXPORT = _ROOT + "/export"
FORMATS = ("sie", "csv", "json")


def now_utc():
    return datetime.now(timezone.utc)


def _ctx(event):
    location_id = http.path_id(event, "locationId")
    return tenant.for_jwt(event, location_id=location_id, owner_only=True)


def _view(location):
    settings = acc.settings_of(location)
    return {**settings,
            "defaults": {role: default for role, (default, _) in acc.DEFAULT_ACCOUNTS.items()},
            "names": {role: name for role, (_, name) in acc.DEFAULT_ACCOUNTS.items()},
            "labels": dict(acc.ROLE_LABELS)}


def _whole(value):
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return value


def _put(event, ctx):
    data = http.body(event)
    if "fiscalYearStartMonth" in data:
        data["fiscalYearStartMonth"] = _whole(data["fiscalYearStartMonth"])
    version = _whole(data.get("version", 0))
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError("version must be a whole number")
    stored = acc.validate_settings(data)
    current = acc.settings_of(ctx.location)["version"]
    if version != current:
        return http.error(409, "changed_retry")
    try:
        dynamo.table(os.environ["LOCATION_TABLE_NAME"]).update_item(
            Key=ctx.location_key, UpdateExpression="SET accounting = :a",
            ConditionExpression="attribute_exists(PK) AND (attribute_not_exists(accounting) OR accounting.version = :v)",
            ExpressionAttributeValues={":a": {**stored, "version": current + 1}, ":v": version})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return http.error(409, "changed_retry")
        raise
    return http.respond(200, _view({"accounting": {**stored, "version": current + 1}}))


def build_export(ctx, first, last, now):
    acc.check_range(first, last)
    location = ctx.location
    settings = acc.settings_of(location)
    events = acc.collect(ctx.tenant, location, tenant.list_locations(ctx.tenant_id), first, last)
    label = str(location.get("name") or "")
    entries = acc.build_entries(first, last, events, settings, label)
    seller = _seller(ctx.tenant, location)
    return ax.Export(company=str(seller.get("name") or label or "Restaurang"), org_number=seller.get("org"),
                     label=label, first=first, last=last, generated=now.astimezone(acc.zone(location)).date(),
                     fiscal_start_month=settings["fiscalYearStartMonth"], accounts=settings["accounts"],
                     entries=entries)


def summary(export):
    accounts = export.accounts
    sales = {accounts[f"sales{r}"] for r in acc.VAT_RATES}
    vat = {accounts[f"vat{r}"] for r in acc.VAT_RATES}
    lines = [ln for e in export.entries for ln in e.lines]
    warnings = [{"date": e.day, "text": w} for e in export.entries for w in e.warnings]
    if not ax.org_text(export.org_number):
        warnings.insert(0, {"date": None, "text": "Organisationsnummer saknas: lägg in det i Booqy Console innan "
                                                  "filen läses in."})
    return {"days": len(export.entries), "bookedDays": len(export.booked),
            "sales": -sum(ln.amount for ln in lines if ln.account in sales),
            "vat": -sum(ln.amount for ln in lines if ln.account in vat),
            "balanced": all(e.balanced for e in export.entries), "warnings": warnings}


def _export(event, ctx):
    params = http.query(event)
    fmt = params.get("format") or "sie"
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    export = build_export(ctx, params.get("from"), params.get("to"), now_utc())
    body = {"from": export.first, "to": export.last, "format": fmt, "summary": summary(export)}
    if fmt == "json":
        body["entries"] = [e.view(export.names) for e in export.entries]
    else:
        delivery = ax.TARGETS[fmt].deliver(export)
        body.update(filename=delivery.filename, mediaType=delivery.media_type,
                    content=base64.b64encode(delivery.content).decode("ascii"))
    logger.info(json.dumps({"export": fmt, "location": ctx.location_id, "from": export.first, "to": export.last,
                            "days": len(export.booked)}))
    return http.respond(200, body)


def handler(event, context):
    method, path = http.route(event)
    try:
        if path == _ROOT and method in ("GET", "PUT"):
            ctx = _ctx(event)
            return _put(event, ctx) if method == "PUT" else http.respond(200, _view(ctx.location))
        if path == _EXPORT and method == "GET":
            return _export(event, _ctx(event))
        if path in (_ROOT, _EXPORT):
            return http.respond(405, {"error": "method not allowed"},
                                {"Allow": "GET, PUT" if path == _ROOT else "GET"})
        return http.error(404, "not found")
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return http.error(400, str(exc))
    except acc.StripeReadFailed as exc:
        logger.error(json.dumps({"stripe": "refused", "message": str(exc)}))
        return http.error(502, "stripe_refused")
    except stripe.StripeUnavailable:
        return http.error(503, "stripe_unavailable")
    except (BotoCoreError, ClientError):
        logger.exception("accounting unavailable")
        return http.error(503, "service_unavailable")
    except Exception:  # noqa: BLE001 - never leak a stack trace
        logger.exception("accounting failed")
        return http.error(500, "internal_error")
