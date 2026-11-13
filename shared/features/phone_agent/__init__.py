"""AI phone booking agent (feature L, ADR-L, CONTRACTS.md 4, 5, 6.L, 9, 13):
shared logic for the phone-agent, phone-agent-call and phone-agent-jobs
Lambdas.

Table `<prefix>phone-agent` (PHONE_AGENT_TABLE_NAME), tenant in every partition key:

    TENANT#<t> / CONFIG                          agent name, tone, languages, recording, cap
    TENANT#<t> / LOC#<l>#CONFIG                  per location: enabled, killSwitch, mode, hours, number, ...
    TENANT#<t> / NUMBER#<e164>                   pointer {locationId}; GSI byNumber (GSI1PK = NUMBER#<e164>)
    TENANT#<t> / CALL#<day>#<callId>             the call row (status machine, disclosure evidence, minutes)
    TENANT#<t> / CALL#<day>#<callId>#T#<seq:05d> a transcript turn
    TENANT#<t> / CALLBACK#<callbackId>           a callback task for Bokningar
    TENANT#<t> / USAGE#<periodEnd>               minutes per billing period (25th-24th)
    TENANT#<t> / SIM#<sessionId>                 the wizard's text simulator session (1 h)

The platform row `PLATFORM#PHONE_AGENT/SETTINGS {killSwitch, maxMinutesCap}`
lives in the tenant table (console forward, like `PLATFORM#META_ADS`).

callId = `<yyyymmdd><16 hex>`: the day is in the id, so a call is found by
its key from the id alone (L8) and the hex part is sha1 of the provider's
call reference, so a duplicate `call_started` webhook maps to the same row.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import dynamo, finance
from shared import tenant as shared_tenant

FEATURE_ID = "phone-agent"
FEATURE_KEY = "phoneAgent"
LIMIT_KEY = "phoneAgentMinutesPerMonth"

MODES = ("after_hours", "overflow", "always")
TONES = ("formal", "friendly")
# The call languages (owner 2026-11-13): Swedish first, then whichever the
# caller speaks (language.CALL_LANGUAGES). `languages` in CONFIG lists the
# ones the agent may switch to; Swedish is always on.
from shared.features.phone_agent.language import CALL_LANGUAGES as LANGUAGES  # noqa: E402
BOOKING_LANGUAGES = ("sv", "en")   # what confirmation SMS and emails are written in
RETENTION_DAYS = (7, 30, 90)  # [NEEDS YOUR OK: options and default 30]
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
OUTCOMES = ("booked", "moved", "cancelled", "info", "transferred", "callback", "abandoned", "refused", "error")
COUNTER_OFFERS = ("none", "other_time", "next_day", "nearby_table")
CALL_STATUSES = ("ringing", "answered", "disclosed", "talking", "transferring", "ended", "processed")
MAX_CALL_DEFAULT, MAX_CALL_MIN, MAX_CALL_MAX = 480, 120, 900
CAP_MAX = 100000
TEXT_MAX = 200
NAME_MAX = 40
SIM_TEXT_MAX = 500
TURN_TTL_DAYS = 730          # 24 months [NEEDS YOUR OK]
CALL_TTL_DAYS = 7 * 365      # billing record
CALLBACK_TTL_DAYS = 90
SIM_TTL_SECONDS = 3600
NUMBER_GRACE_DAYS = 30
TRANSFER_TIMEOUT_S = 25
# CONTRACTS.md 4: the agent's system user is `USER#agent:<l>` with status
# "system"; shared/tenant.staff_location() accepts that status for this
# prefix only, and manage-user keeps such rows out of the tenant's user list.
AGENT_USER_PREFIX = shared_tenant.SYSTEM_SUB_PREFIX
AGENT_USER_STATUS = "system"
PLATFORM_KEY = {"PK": "PLATFORM#PHONE_AGENT", "SK": "SETTINGS"}

_E164 = re.compile(r"\+[1-9]\d{6,14}")
_HHMM = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_ID = re.compile(r"[0-9A-Za-z_-]{6,64}")
_CALL_ID = re.compile(r"(\d{8})([0-9a-f]{16})")


class PhoneAgentError(Exception):
    def __init__(self, status: int, code: str, field: Optional[str] = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.field = field


def table():
    return dynamo.table(os.environ["PHONE_AGENT_TABLE_NAME"])


def tenant_table():
    return dynamo.table(os.environ["TENANT_TABLE_NAME"])


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def conditional_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def query_all(pk, prefix, **kwargs) -> list:
    cond = Key("PK").eq(pk) & Key("SK").begins_with(prefix) if prefix else Key("PK").eq(pk)
    items, args = [], {"KeyConditionExpression": cond, **kwargs}
    while True:
        page = table().query(**args)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        args["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def phone_hash(tenant_id, phone) -> str:
    return hashlib.sha256(f"{tenant_id}|{phone}".encode()).hexdigest()


# --- keys --------------------------------------------------------------------------------

def tenant_pk(tenant_id) -> str:
    return f"TENANT#{tenant_id}"


def config_key(tenant_id) -> dict:
    return {"PK": tenant_pk(tenant_id), "SK": "CONFIG"}


def loc_key(tenant_id, location_id) -> dict:
    return {"PK": tenant_pk(tenant_id), "SK": f"LOC#{location_id}#CONFIG"}


def number_key(tenant_id, e164) -> dict:
    return {"PK": tenant_pk(tenant_id), "SK": f"NUMBER#{e164}"}


def call_id_for(call_ref: str, at: datetime) -> str:
    day = at.astimezone(ZoneInfo("Europe/Stockholm")).strftime("%Y%m%d")
    return day + hashlib.sha1(str(call_ref).encode()).hexdigest()[:16]


def call_day(call_id) -> Optional[str]:
    m = _CALL_ID.fullmatch(call_id or "")
    if not m:
        return None
    d = m.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def call_key(tenant_id, call_id) -> Optional[dict]:
    day = call_day(call_id)
    return {"PK": tenant_pk(tenant_id), "SK": f"CALL#{day}#{call_id}"} if day else None


def turn_key(tenant_id, call_id, seq) -> dict:
    return {"PK": tenant_pk(tenant_id), "SK": f"CALL#{call_day(call_id)}#{call_id}#T#{int(seq):05d}"}


def callback_key(tenant_id, callback_id) -> dict:
    return {"PK": tenant_pk(tenant_id), "SK": f"CALLBACK#{callback_id}"}


def usage_key(tenant_id, period_end) -> dict:
    return {"PK": tenant_pk(tenant_id), "SK": f"USAGE#{period_end}"}


def sim_key(tenant_id, session_id) -> dict:
    return {"PK": tenant_pk(tenant_id), "SK": f"SIM#{session_id}"}


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


# --- billing period ----------------------------------------------------------------------

def period_end(at: Optional[datetime] = None) -> str:
    """The billing period's last day (periods run 25th-24th, Swedish calendar day)."""
    day = datetime.fromisoformat(finance.billing_day(at or now())).date()
    if day.day <= 24:
        return day.replace(day=24).isoformat()
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return day.replace(year=year, month=month, day=24).isoformat()


def period_start(end: str) -> str:
    day = datetime.fromisoformat(end).date()
    year, month = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
    return day.replace(year=year, month=month, day=25).isoformat()


# --- config ------------------------------------------------------------------------------

def get_config(tenant_id) -> dict:
    return table().get_item(Key=config_key(tenant_id), ConsistentRead=True).get("Item") or {}


def get_loc(tenant_id, location_id) -> dict:
    return table().get_item(Key=loc_key(tenant_id, location_id), ConsistentRead=True).get("Item") or {}


def platform_settings() -> dict:
    item = tenant_table().get_item(Key=PLATFORM_KEY, ConsistentRead=True).get("Item") or {}
    return {"killSwitch": bool(item.get("killSwitch", False)),
            "maxMinutesCap": int(item.get("maxMinutesCap") or CAP_MAX)}


def included_minutes(tenant_row) -> int:
    return shared_tenant.limit(tenant_row, LIMIT_KEY)


def tenant_cap(tenant_row, config) -> int:
    """The tenant's monthly cap: its own setting (default the plan's included
    minutes), never above the platform ceiling."""
    own = config.get("minutesCapPerMonth")
    cap = included_minutes(tenant_row) if own is None else int(own)
    return min(cap, platform_settings()["maxMinutesCap"])


def config_view(tenant_row, config) -> dict:
    return {"agentName": config.get("agentName") or "", "tone": config.get("tone") or "friendly",
            "languages": list(config.get("languages") or LANGUAGES),
            "recording": plain(config.get("recording") or {"enabled": False}),
            "minutesCapPerMonth": tenant_cap(tenant_row, config), "version": int(config.get("version") or 0)}


def _text(value, field, limit=TEXT_MAX):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PhoneAgentError(400, f"{field} must be text", field)
    text = " ".join(value.split())
    if len(text) > limit:
        raise PhoneAgentError(400, "text_too_long", field)
    return text


def _int(value, field, code):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value != int(value):
        raise PhoneAgentError(400, code, field)
    return int(value)


def validate_config(body, *, ceiling) -> dict:
    name = _text(body.get("agentName"), "agentName", NAME_MAX)
    if not name:
        raise PhoneAgentError(400, "agentName is required", "agentName")
    tone = body.get("tone")
    if tone not in TONES:
        raise PhoneAgentError(400, "tone must be formal or friendly", "tone")
    languages = body.get("languages")
    if not isinstance(languages, list) or any(x not in LANGUAGES for x in languages):
        raise PhoneAgentError(400, "languages must be among " + ", ".join(LANGUAGES), "languages")
    languages = ["sv", *languages]
    rec = body.get("recording") or {}
    if not isinstance(rec, dict) or not isinstance(rec.get("enabled", False), bool):
        raise PhoneAgentError(400, "recording must be an object", "recording")
    recording = {"enabled": bool(rec.get("enabled"))}
    if recording["enabled"]:
        purpose = _text(rec.get("purpose"), "recording.purpose", 300)
        if len(purpose) < 5:
            raise PhoneAgentError(400, "purpose_required", "recording.purpose")
        days = rec.get("retentionDays", 30)
        if isinstance(days, bool) or days not in RETENTION_DAYS:
            raise PhoneAgentError(400, "invalid_retention", "recording.retentionDays")
        recording.update({"purpose": purpose, "retentionDays": int(days)})
    cap = _int(body.get("minutesCapPerMonth"), "minutesCapPerMonth", "invalid_cap")
    if not 0 <= cap <= ceiling:
        raise PhoneAgentError(400, "invalid_cap", "minutesCapPerMonth")
    return {"agentName": name, "tone": tone, "languages": [x for x in LANGUAGES if x in languages],
            "recording": recording, "minutesCapPerMonth": cap}


def normalize_e164(value) -> str:
    text = re.sub(r"[\s-]", "", value) if isinstance(value, str) else ""
    if text.startswith("0"):
        text = "+46" + text[1:]
    if not _E164.fullmatch(text):
        raise PhoneAgentError(400, "invalid_phone", "transferTo")
    return text


def validate_hours(value) -> dict:
    if not isinstance(value, dict) or set(value) - set(DAYS):
        raise PhoneAgentError(400, "invalid_hours", "agentHours")
    out = {}
    for day in DAYS:
        spans = value.get(day) or []
        if not isinstance(spans, list) or len(spans) > 3:
            raise PhoneAgentError(400, "invalid_hours", "agentHours")
        clean = []
        for span in spans:
            start, end = (span or {}).get("from"), (span or {}).get("to")
            if not isinstance(start, str) or not isinstance(end, str) or not _HHMM.fullmatch(start) \
                    or not _HHMM.fullmatch(end) or start >= end:
                raise PhoneAgentError(400, "invalid_hours", "agentHours")
            clean.append({"from": start, "to": end})
        out[day] = sorted(clean, key=lambda s: s["from"])
    return out


def validate_location(body) -> dict:
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise PhoneAgentError(400, "enabled must be true or false", "enabled")
    mode = body.get("mode")
    if mode not in MODES:
        raise PhoneAgentError(400, "mode must be after_hours, overflow or always", "mode")
    hours = validate_hours(body.get("agentHours") or {})
    transfer = body.get("transferTo")
    transfer = normalize_e164(transfer) if transfer not in (None, "") else None
    max_call = body.get("maxCallSeconds", MAX_CALL_DEFAULT)
    if isinstance(max_call, bool) or not isinstance(max_call, (int, Decimal)) or \
            not MAX_CALL_MIN <= max_call <= MAX_CALL_MAX:
        raise PhoneAgentError(400, "invalid_max_call", "maxCallSeconds")
    offers = body.get("counterOffers")
    if offers is None:
        offers = {"nearbyTables": True, "phoneOnlyTables": True}
    if not isinstance(offers, dict) or set(offers) - {"nearbyTables", "phoneOnlyTables"} or \
            any(not isinstance(offers.get(k, True), bool) for k in ("nearbyTables", "phoneOnlyTables")):
        raise PhoneAgentError(400, "invalid_counter_offers", "counterOffers")
    share = body.get("shareNumberWith")
    if share is not None and not valid_id(share):
        raise PhoneAgentError(400, "shareNumberWith must be a location id", "shareNumberWith")
    return {"enabled": enabled, "mode": mode, "agentHours": hours, "shareNumberWith": share,
            "greetingExtraSv": _text(body.get("greetingExtraSv"), "greetingExtraSv"),
            "greetingExtraEn": _text(body.get("greetingExtraEn"), "greetingExtraEn"),
            "transferTo": transfer, "maxCallSeconds": int(max_call),
            "counterOffers": {"nearbyTables": offers.get("nearbyTables", True),
                              "phoneOnlyTables": offers.get("phoneOnlyTables", True)}}


def loc_view(loc, *, owner=True) -> dict:
    out = {"locationId": loc.get("locationId"), "enabled": bool(loc.get("enabled")),
           "killSwitch": bool(loc.get("killSwitch")), "mode": loc.get("mode") or "after_hours",
           "agentHours": plain(loc.get("agentHours") or {}), "greetingExtraSv": loc.get("greetingExtraSv") or "",
           "greetingExtraEn": loc.get("greetingExtraEn") or "", "maxCallSeconds": int(loc.get("maxCallSeconds")
                                                                                       or MAX_CALL_DEFAULT),
           "counterOffers": plain(loc.get("counterOffers") or {"nearbyTables": True, "phoneOnlyTables": True}),
           "number": (loc.get("number") or {}).get("e164"),
           "numberSharedWith": (loc.get("number") or {}).get("ownerLocationId") or None,
           "version": int(loc.get("version") or 0)}
    if owner:
        out["transferTo"] = loc.get("transferTo")
    return out


# Forwarding codes per Swedish operator (GSM/MMI codes; [NEEDS YOUR OK: verified by a human]).
FORWARDING = {
    "after_hours": [{"operator": "Telia, Tele2, Telenor, Tre", "label": "Vidarekoppla alla samtal",
                     "on": "**21*{n}#", "off": "##21#"}],
    "overflow": [{"operator": "Telia, Tele2, Telenor, Tre", "label": "Vidarekoppla vid ej svar",
                  "on": "**61*{n}**25#", "off": "##61#"},
                 {"operator": "Telia, Tele2, Telenor, Tre", "label": "Vidarekoppla vid upptaget",
                  "on": "**67*{n}#", "off": "##67#"}],
}
FORWARDING["always"] = FORWARDING["after_hours"]


def forwarding(loc) -> dict:
    number = (loc.get("number") or {}).get("e164")
    codes = [] if not number else [{**c, "on": c["on"].format(n=number)}
                                   for c in FORWARDING[loc.get("mode") or "after_hours"]]
    return {"number": number, "codes": codes}


# --- hours and guards --------------------------------------------------------------------

def inside_hours(loc, at: datetime, timezone_name="Europe/Stockholm") -> bool:
    """Agent hours of the location (mode `always` answers around the clock)."""
    if (loc.get("mode") or "after_hours") == "always":
        return True
    local = at.astimezone(ZoneInfo(timezone_name or "Europe/Stockholm"))
    clock = local.strftime("%H:%M")
    spans = (plain(loc.get("agentHours") or {})).get(DAYS[local.weekday()]) or []
    return any(s["from"] <= clock < s["to"] for s in spans)


def usage(tenant_id, end) -> dict:
    return table().get_item(Key=usage_key(tenant_id, end), ConsistentRead=True).get("Item") or {}


def refusal(tenant_row, config, loc, at, *, timezone_name="Europe/Stockholm") -> Optional[str]:
    """Why a new call is refused (CallRefusedReason), or None (ADR-L 5, 9)."""
    if not tenant_row or tenant_row.get("status") != "active" or \
            not ((tenant_row.get("entitlements") or {}).get("features") or {}).get(FEATURE_KEY):
        return "entitlement"
    if not loc.get("enabled"):
        return "entitlement"
    if config.get("paused"):
        return "paused"
    if loc.get("killSwitch") or platform_settings()["killSwitch"]:
        return "kill_switch"
    if not inside_hours(loc, at, timezone_name):
        return "hours"
    if int(usage(tenant_row["tenantId"], period_end(at)).get("minutes") or 0) >= tenant_cap(tenant_row, config):
        return "cap"
    return None


# --- views -------------------------------------------------------------------------------

def call_view(call, *, owner) -> dict:
    out = {"callId": call["callId"], "locationId": call.get("locationId"), "startedAt": call.get("startedAt"),
           "answeredAt": call.get("answeredAt"), "endedAt": call.get("endedAt"),
           "durationSec": int(call.get("durationSec") or 0), "billedMinutes": int(call.get("billedMinutes") or 0),
           "language": call.get("language") or "sv", "outcome": call.get("outcome"),
           "refusedReason": call.get("refusedReason"), "counterOffer": call.get("counterOffer") or "none",
           "reservationId": call.get("reservationId"), "callbackId": call.get("callbackId"),
           "summary": call.get("summary") or "", "status": call.get("status"),
           "disclosure": plain(call.get("disclosure") or {})}
    if owner:
        out["from"] = call.get("from")
    return out


def callback_view(cb) -> dict:
    return {"callbackId": cb["callbackId"], "locationId": cb.get("locationId"), "callId": cb.get("callId"),
            "name": cb.get("name") or "", "phone": cb.get("phone") or "", "topic": cb.get("topic") or "",
            "preferredTime": cb.get("preferredTime"), "status": cb.get("status"), "createdAt": cb.get("createdAt"),
            "closedAt": cb.get("closedAt"), "note": cb.get("note") or "", "version": int(cb.get("version") or 0)}
