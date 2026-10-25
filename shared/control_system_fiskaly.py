"""fiskaly SIGN SE behind the control system port (shared/control_system.py).

fiskaly's Unified API, version 2026-06-01 (workspace.fiskaly.com, SIGN SE,
spec fiskaly.uapi.sign-se.2026-06-01.yaml, a copy in tests/fixtures/fiskaly):

    POST  /tokens         API key and secret -> bearer (JWT), kept in memory
                          until shortly before it expires
    POST  /organizations  one UNIT per Booqy tenant (the merchant), under our GROUP
    POST  /taxpayers      the company (org.nr), then PATCH state COMMISSIONED
    POST  /locations      one BRANCH per Booqy location, then COMMISSIONED
    POST  /systems        one FISCAL_DEVICE per register; COMMISSIONED enrols it
                          and gives the 16-character tillverkningsnummer, which
                          is the register's kassabeteckning
    POST  /records        INTENTION::OPENING (the status check, before any sale),
                          INTENTION::TRANSACTION then TRANSACTION::RECEIPT (a
                          receipt; the control code comes back), INTENTION::DUPLICATE
                          (a copy), INTENTION::CLOSING (end of day)
    PATCH /systems/{id}   state DECOMMISSIONED (register out of use)

The key is read at runtime from Secrets Manager (FISKALY_SECRET_ID), JSON
{"api_key", "api_secret", "env": "TEST" | "LIVE"}, kept in memory and never
logged or put in an error. Every call uses the GROUP key's token, scoped to
the merchant's UNIT with X-Scope-Identifier.

Errors: 400, 404, 405, 422 -> Rejected (our data; no retry); 401 -> a new
token, once; 403 or a refused key -> Unreachable(EJ_KONFIGURERAD); 409, 429,
5xx and timeouts -> exponential backoff within a time budget, then
Unreachable. The status codes 1-7 map to Skatteverket's names
(control_system); only 1 allows a receipt. fiskaly has no 48-hour offline
mode, so 2 (no contact with the control server) means no receipt as well.

fiskaly's ids live in the register's csRef (systemId, unitId, env) and in one
item per tenant and environment in the kassa table: PK TENANT#<tenantId>,
SK CS#fiskaly#<env> (unitId, taxpayerId, locations {Booqy location: fiskaly
location}).

Not yet tried against fiskaly's TEST environment (docs/KASSA-REQUIREMENTS.md,
"fiskaly SIGN SE", Osäkert): where a record carries the status code, which
field holds the 113-character code and the tillverkningsnummer, whether a copy
needs a TRANSACTION after INTENTION::DUPLICATE. The parsers look in the
documented places first and tests/test_control_system_fiskaly.py pins them.
"""

import json
import os
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from shared import control_system as cs
from shared import dynamo

PROVIDER = "fiskaly"
API_VERSION = "2026-06-01"
BASES = {"TEST": "https://test.api.fiskaly.com", "LIVE": "https://live.api.fiskaly.com"}
# Kontrollserverns ID of fiskaly's control system (TCS). fiskaly documents it as
# static ("Skatteverket Registration", "Receipt Compliance"); the secret may
# override it with "control_server_id".
CONTROL_SERVER_ID = "ISTCSKS0000000011"
BRAND = "Booqy"                   # producer.details.brand; enrolment needs brand and model
PART_NUMBER = "BOOQY-KASSA-SE"
VAT_CODES = {25: "STANDARD", 12: "REDUCED_1", 6: "REDUCED_2", 0: "REDUCED_3"}
SERIES = {cs.NORMAL: "KVITTO", cs.COPY: "KOPIA", cs.TRAINING: "OVNING"}

CODES = {1: cs.STATUS_OK, 2: cs.STATUS_NO_CONTACT, 3: cs.STATUS_CONTACT_AGAIN, 4: cs.STATUS_NOT_CONFIGURED,
         5: cs.STATUS_CALL_MANUFACTURER, 6: cs.STATUS_REPORT_24H, 7: cs.STATUS_REPORT_48H}
NAMES = {"STATUS_OK": 1, "NO_CONTACT_WITH_KS": 2, "CONTACT_WITH_KS": 3, "NOT_CONFIGURED": 4,
         "CONTACT_MANUFACTURER": 5, "REPORT_ERROR_24H": 6, "REPORT_ERROR_48H": 7,
         **{name: code for code, name in CODES.items()}}
_NAME = re.compile(r"\b(" + "|".join(sorted(NAMES, key=len, reverse=True)) + r")\b")
_DESIGNATION = re.compile(r"[A-Z0-9]{16}")

TIMEOUT = 8                       # one HTTP request
SIGN_BUDGET = 18                  # one receipt; the register lock holds 30 s, API Gateway 30 s
STATUS_BUDGET = 4                 # one status check; the till checks up to 5 registers per view
ENROL_BUDGET = 22
RETRIES = 4
POLL_SECONDS = 0.4
SECRET_SECONDS = 900
TOKEN_MARGIN = 60

_secret = {"value": None, "at": 0.0}
_tokens = {}


def _urllib(method, url, headers, body, timeout):
    """(status, headers, parsed JSON or {}) ; raises OSError on no answer."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https host
            return response.status, dict(response.headers), _json(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), _json(exc.read())


def _json(raw):
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def credentials():
    """{"key", "secret", "env", "server"} from Secrets Manager (cached)."""
    now = time.time()
    if _secret["value"] and now - _secret["at"] < SECRET_SECONDS:
        return _secret["value"]
    secret_id = os.environ.get("FISKALY_SECRET_ID")
    if not secret_id:
        raise cs.Unreachable(cs.STATUS_NOT_CONFIGURED)
    try:
        import boto3
        value = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"])
    except Exception:  # noqa: BLE001 - missing secret, no access, not JSON: all "not configured"
        raise cs.Unreachable(cs.STATUS_NOT_CONFIGURED) from None
    env = str((value or {}).get("env") or "TEST").upper()
    if not isinstance(value, dict) or not value.get("api_key") or not value.get("api_secret") or env not in BASES:
        raise cs.Unreachable(cs.STATUS_NOT_CONFIGURED)
    _secret["value"] = {"key": str(value["api_key"]), "secret": str(value["api_secret"]), "env": env,
                        "server": str(value.get("control_server_id") or CONTROL_SERVER_ID)}
    _secret["at"] = now
    return _secret["value"]


# --- reading fiskaly's answers ---------------------------------------------------------

def _as_code(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value in CODES:
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() and int(text) in CODES:
            return int(text)
        return NAMES.get(text.upper())
    return None


def _find_status(value, depth=0):
    if not isinstance(value, dict) or depth > 3:
        return None
    for key in ("status", "status_code", "code", "register_status"):
        code = _as_code(value.get(key))
        if code:
            return code
    for inner in value.values():
        code = _find_status(inner, depth + 1)
        if code:
            return code
    return None


def _data_tokens(record):
    """compliance.data split on , ; | and spaces (fiskaly's Czech module keeps
    "EET,<code>" there; Sweden may do the same)."""
    data = (record.get("compliance") or {}).get("data")
    return [t for t in re.split(r"[,;|\s]+", data.strip()) if t] if isinstance(data, str) else []


def status_code(record):
    """The status code (1-7) a record carries, or None. fiskaly's guide reads
    it from content.compliance.status; then compliance.data, then the logs."""
    code = _find_status(record.get("compliance")) or _as_code(record.get("status"))
    if code:
        return code
    tokens = _data_tokens(record)
    for token in tokens:
        if token.upper() in NAMES:
            return NAMES[token.upper()]
    if len(tokens) > 1:
        digits = [_as_code(t) for t in tokens if len(t) == 1]
        if digits and digits[0]:
            return digits[0]
    for log in record.get("logs") or []:
        match = _NAME.search(str((log or {}).get("message") or ""))
        if match:
            return NAMES[match.group(1)]
    return None


def _strings(value, depth=0):
    if isinstance(value, str):
        yield value.strip()
    elif isinstance(value, dict) and depth < 4:
        for inner in value.values():
            yield from _strings(inner, depth + 1)
    elif isinstance(value, list) and depth < 4:
        for inner in value:
            yield from _strings(inner, depth + 1)


def control_code(record):
    """The 113-character avstämningskod: compliance.data, else any string of
    that length in compliance. None when there is none (training, pro forma)."""
    compliance = record.get("compliance") or {}
    data = compliance.get("data")
    if isinstance(data, str) and len(data.strip()) == cs.AVSTAMNING_LENGTH:
        return data.strip()
    for text in [*_strings(compliance), *_data_tokens(record)]:
        if len(text) == cs.AVSTAMNING_LENGTH:
            return text
    return data.strip() if isinstance(data, str) and data.strip() and len(_data_tokens(record)) == 1 else None


def designation(system):
    """The 16-character tillverkningsnummer of a commissioned system
    (compliance.technical_security_system_id), or None."""
    compliance = system.get("compliance") or {}
    for value in (compliance.get("technical_security_system_id"), system.get("technical_security_system_id")):
        if isinstance(value, str) and _DESIGNATION.fullmatch(value.strip()):
            return value.strip()
    for text in _strings(compliance):
        if _DESIGNATION.fullmatch(text):
            return text
    return None


def outcome(record):
    """Status name of a finished record; Rejected when fiskaly refused it."""
    code = status_code(record)
    state = record.get("state")
    if state == "REJECTED":
        raise cs.Rejected(f"record rejected: {_log_text(record)}")
    if code is None:
        if state == "COMPLETED":
            return cs.STATUS_OK
        return cs.STATUS_NO_CONTACT
    return CODES[code]


def _log_text(record):
    return "; ".join(str((log or {}).get("message") or "")[:120] for log in (record.get("logs") or [])[:3])


# --- money and addresses -----------------------------------------------------------------

def kr(ore):
    """Öre -> "123.45" (fiskaly's decimal strings)."""
    return str((Decimal(int(ore)) / 100).quantize(Decimal("0.01")))


def _percentage(rate):
    return f"{int(rate)}.00"


_STREET = re.compile(r"^(?P<street>.*?\D)\s*(?P<number>\d+\s?[A-Za-z]?(?:\s?[-/]\s?\d+[A-Za-z]?)?)$")
_TOWN = re.compile(r"^(?P<code>\d{3}\s?\d{2})\s+(?P<city>.+)$")


def address(parts=None, text=None):
    """fiskaly's address ({line: STREET_NUMBER, code, city, country SE}) from
    {street, postalCode, city} or one line "Storgatan 1, 111 22 Stockholm"."""
    street = code = city = ""
    if isinstance(parts, dict) and parts.get("street") and parts.get("postalCode") and parts.get("city"):
        street, code, city = (" ".join(str(parts[k]).split()) for k in ("street", "postalCode", "city"))
    elif text:
        pieces = [" ".join(p.split()) for p in str(text).split(",") if p.strip()]
        town = next((i for i, p in enumerate(pieces) if _TOWN.match(p)), None)
        if town:
            match = _TOWN.match(pieces[town])
            street, code, city = pieces[town - 1], match.group("code"), match.group("city")
    found = _STREET.match(street)
    if not found or not code or not city:
        raise cs.Rejected("the address needs a street with a number, a postal code and a city")
    return {"line": {"type": "STREET_NUMBER", "street": found.group("street").strip(),
                     "number": found.group("number").replace(" ", "")},
            "code": code.replace(" ", ""), "city": city[:128], "country": "SE"}


def operation(data):
    """TRANSACTION::RECEIPT for a ReceiptData: entries per line (SALE, or
    RETURN with positive amounts), the VAT breakdown, totals and payments."""
    returning = bool(data.returns_by_rate) and not data.sales_by_rate
    lines = list(data.lines) or [
        {"text": f"Moms {rate} %", "quantity": 1, "total": int(gross), "rate": int(rate),
         "vat": int(data.vat_by_rate.get(rate, 0))}
        for rate, gross in sorted((data.returns_by_rate if returning else data.sales_by_rate).items())]
    entries, breakdown = [], {}
    for line in lines:
        rate, total, vat = int(line["rate"]), int(line["total"]), int(line["vat"])
        if rate not in VAT_CODES:
            raise cs.Rejected(f"no fiskaly VAT code for {rate} %")
        quantity, name = int(line.get("quantity") or 1), str(line.get("text") or "Vara")
        if quantity > 1 and total % quantity == 0 and vat % quantity == 0:
            unit, unit_vat, text = total // quantity, vat // quantity, name
        else:
            # A unit price that isn't exact to the öre: the line as one unit.
            unit, unit_vat, text = total, vat, f"{quantity} x {name}" if quantity > 1 else name
            quantity = 1
        net, unit_net = total - vat, unit - unit_vat
        vat_block = {"type": "VAT_RATE", "code": VAT_CODES[rate], "percentage": _percentage(rate),
                     "amount": kr(vat), "exclusive": kr(net), "inclusive": kr(total)}
        entries.append({"type": "RETURN" if returning else "SALE", "details": {"concept": "GOOD"},
                        "data": {"type": "ITEM", "text": text[:256],
                                 "unit": {"quantity": str(quantity),
                                          "price": {"inclusive": kr(unit), "exclusive": kr(unit_net)}},
                                 "value": {"base": kr(net)}, "vat": vat_block}})
        row = breakdown.setdefault(rate, {"amount": 0, "inclusive": 0})
        row["amount"] += vat
        row["inclusive"] += total
    rows = [{"type": "VAT_RATE", "code": VAT_CODES[r], "percentage": _percentage(r), "amount": kr(v["amount"]),
             "exclusive": kr(v["inclusive"] - v["amount"]), "inclusive": kr(v["inclusive"])}
            for r, v in sorted(breakdown.items())]
    vat_total = sum(v["amount"] for v in breakdown.values())
    gross_total = sum(v["inclusive"] for v in breakdown.values())
    day = data.at.date().isoformat()
    payments = [{"type": "CARD", "kind": "CREDIT" if p.get("funding") == "credit" else "DEBIT",
                 "number": str(p.get("last4") or "****")[-19:].rjust(4, "*"),
                 "details": {"amount": kr(p["amount"]), "currency": "SEK", "date": day}}
                for p in data.payments if int(p.get("amount") or 0) > 0]
    if not payments:
        # Training receipts take no payment; fiskaly wants one.
        payments = [{"type": "OTHER", "name": "Övningskvitto" if data.kind == cs.TRAINING else "Ingen betalning",
                     "details": {"amount": kr(gross_total), "currency": "SEK", "date": day}}]
    document = {"number": str(data.serial), "series": SERIES[data.kind],
                "issued_at": data.at.isoformat(timespec="seconds"), "operation_date": day}
    if returning and data.original_serial:
        document["references"] = {"preceding_document": {"number": str(data.original_serial)}}
    return {"type": "RECEIPT", "document": document, "entries": entries, "breakdown": rows,
            "totals": {"vat": {"amount": kr(vat_total), "exclusive": kr(gross_total - vat_total),
                               "inclusive": kr(gross_total)}},
            "payments": payments, "details": details(data)}


def details(data):
    """Who made it, and training: fiskaly takes training per request."""
    out = {"creators": [{"type": "PERSON", "label": (data.user or "kassa")[:128]}]}
    if data.kind == cs.TRAINING:
        out["training"] = True
    return out


# --- the adapter -------------------------------------------------------------------------

class FiskalyControlSystem(cs.ControlSystem):
    provider = PROVIDER
    name = "fiskaly SIGN SE"

    @property
    def control_unit(self):
        """The kontrollenhet every fiskaly merchant reports (FSK, "Skatteverket
        Registration"): the same for all of them."""
        return {"manufacturer": "TCS", "model": "System ver. 1.1 + Miljö rev. A",
                "address": "Tunnelgatan 2, Stockholm", "serverId": self.serial}

    def __init__(self, *, transport=None, sleep=time.sleep, clock=time.monotonic, creds=None):
        self._transport = transport or _urllib
        self._sleep = sleep
        self._clock = clock
        self._creds = creds

    # credentials and environment

    def _c(self):
        if self._creds is None:
            self._creds = credentials()
        return self._creds

    @property
    def env(self):
        try:
            return self._c()["env"]
        except cs.Unreachable:
            return None

    @property
    def serial(self):
        try:
            return self._c()["server"]
        except cs.Unreachable:
            return CONTROL_SERVER_ID

    @property
    def certifiable(self):
        """Only LIVE receipts are real; TEST ones never reach Skatteverket."""
        return self.env == "LIVE"

    # HTTP

    def _token(self, deadline):
        c = self._c()
        cache = (c["env"], c["key"])
        cached = _tokens.get(cache)
        if cached and cached["until"] > time.time() + TOKEN_MARGIN:
            return cached
        content = self._call("POST", "/tokens", {"content": {"type": "API_KEY", "key": c["key"],
                                                             "secret": c["secret"]}},
                             deadline=deadline).get("content") or {}
        auth = content.get("authentication") or {}
        if not auth.get("bearer"):
            raise cs.Unreachable(cs.STATUS_NOT_CONFIGURED)
        try:
            until = datetime.fromisoformat(str(auth.get("expires_at")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            until = time.time() + 300
        _tokens[cache] = {"bearer": auth["bearer"], "until": until,
                          "group": (content.get("organization") or {}).get("id")}
        return _tokens[cache]

    def _call(self, method, path, body=None, *, deadline, scope=None, idem=None):
        token_call = path == "/tokens"
        idem = idem or str(uuid.uuid4())   # the same key on every retry: fiskaly answers it once
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        attempt, renewed = 0, False
        while True:
            headers = {"X-Api-Version": API_VERSION, "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            if method in ("POST", "PATCH"):
                headers["X-Idempotency-Key"] = idem
            if not token_call:
                headers["Authorization"] = "Bearer " + self._token(deadline)["bearer"]
            if scope:
                headers["X-Scope-Identifier"] = scope
            left = deadline - self._clock()
            if left <= 0:
                raise cs.Unreachable()
            try:
                status, answer_headers, answer = self._transport(method, BASES[self._c()["env"]] + path, headers,
                                                                 data, min(TIMEOUT, left))
            except (OSError, ValueError):
                status, answer_headers, answer = None, {}, {}
            if status is not None and 200 <= status < 300:
                return answer
            if status == 401 and not token_call and not renewed:
                _tokens.pop((self._c()["env"], self._c()["key"]), None)
                renewed = True
                continue
            if status in (401, 403):
                raise cs.Unreachable(cs.STATUS_NOT_CONFIGURED)
            if status in (400, 404, 405, 422):
                detail = "" if token_call else f" {answer.get('code') or ''} {str(answer.get('message') or '')[:160]}"
                raise cs.Rejected(f"{method} {path.split('?')[0]}: {status}{detail}")
            attempt += 1
            wait = min(4.0, 0.5 * 2 ** (attempt - 1)) * (0.75 + random.random() / 2)  # noqa: S311
            after = str((answer_headers or {}).get("Retry-After") or "")
            if after.isdigit():
                wait = max(wait, float(after))
            if attempt >= RETRIES or self._clock() + wait >= deadline:
                raise cs.Unreachable()
            self._sleep(wait)

    def _record(self, scope, content, *, deadline):
        record = self._call("POST", "/records", {"content": content}, scope=scope,
                            deadline=deadline).get("content") or {}
        while record.get("mode") == "PROCESSING" and record.get("id"):
            if self._clock() + POLL_SECONDS >= deadline:
                raise cs.Unreachable()
            self._sleep(POLL_SECONDS)
            record = self._call("GET", f"/records/{record['id']}", scope=scope,
                                deadline=deadline).get("content") or {}
        return record

    def _ref(self, register):
        ref = (register or {}).get("csRef") or {}
        if ref.get("provider") != PROVIDER or ref.get("env") != self.env or not ref.get("systemId"):
            raise cs.Unreachable(cs.STATUS_NOT_CONFIGURED)
        return ref

    # the port

    def status(self, register):
        """INTENTION::OPENING: fiskaly's startup check (RegisterStatus)."""
        try:
            ref = self._ref(register)
            record = self._record(ref["unitId"], {"type": "INTENTION", "system": {"id": ref["systemId"]},
                                                  "operation": {"type": "OPENING"}},
                                  deadline=self._clock() + STATUS_BUDGET)
            code = outcome(record)
        except cs.Unreachable as exc:
            code = exc.status
        except cs.Rejected:
            code = cs.STATUS_NOT_CONFIGURED
        return cs.Status(ok=code == cs.STATUS_OK, code=code, provider=self.provider, serial=self.serial)

    def sign(self, register, data):
        ref = self._ref(register)
        deadline = self._clock() + SIGN_BUDGET
        if data.kind == cs.COPY:
            original = (data.original_ref or {}).get("recordId")
            if not original:
                raise cs.Rejected("a copy needs the original receipt's fiskaly record")
            intention = {"type": "INTENTION", "system": {"id": ref["systemId"]},
                         "operation": {"type": "DUPLICATE", "record": {"id": original}, "number": "1",
                                       "reason": "Kvittokopia", "details": details(data)}}
        else:
            intention = {"type": "INTENTION", "system": {"id": ref["systemId"]},
                         "operation": {"type": "TRANSACTION", "details": details(data)}}
        first = self._record(ref["unitId"], intention, deadline=deadline)
        self._allowed(first)
        record, code = first, control_code(first) if data.kind == cs.COPY else None
        if not code:
            if not first.get("id"):
                raise cs.Rejected("fiskaly returned no record id")
            record = self._record(ref["unitId"], {"type": "TRANSACTION", "record": {"id": first["id"]},
                                                  "operation": operation(data)}, deadline=deadline)
            self._allowed(record)
            code = control_code(record)
        if not code and data.kind != cs.TRAINING:
            raise cs.Rejected("fiskaly returned no control code")
        return cs.Registration(control_code=code or "", serial=self.serial,
                               ref={"provider": PROVIDER, "recordId": record.get("id"),
                                    "intentionId": first.get("id")})

    @staticmethod
    def _allowed(record):
        code = outcome(record)
        if code != cs.STATUS_OK:
            raise cs.Unreachable(code)

    def close_day(self, register):
        ref = self._ref(register)
        self._record(ref["unitId"], {"type": "INTENTION", "system": {"id": ref["systemId"]},
                                     "operation": {"type": "CLOSING", "period": "DAILY"}},
                     deadline=self._clock() + STATUS_BUDGET)

    def pause_register(self, register):
        ref = self._ref(register)
        self._call("PATCH", f"/systems/{ref['systemId']}", {"content": {"state": "DECOMMISSIONED"}},
                   scope=ref["unitId"], deadline=self._clock() + STATUS_BUDGET)

    def register_register(self, register, company):
        """Merchant (UNIT), company (taxpayer) and location once, then a
        system for the register, commissioned: its tillverkningsnummer is the
        kassabeteckning the merchant registers with Skatteverket."""
        from shared import kassa as k
        deadline = self._clock() + ENROL_BUDGET
        env = self._c()["env"]
        tenant_id, location_id = register["tenantId"], register["locationId"]
        org = "".join(ch for ch in str(company.get("org") or "") if ch.isdigit())
        if len(org) != 10:
            # Enskild firma (personnummer): fiskaly wants the owner's name and gender; not offered yet.
            raise cs.Rejected("fiskaly needs a 10-digit organisation number")
        state = _state(tenant_id, env)
        group = self._token(deadline).get("group")
        unit = state.get("unitId") or _save(tenant_id, env, "unitId", (self._call(
            "POST", "/organizations", {"content": {"type": "UNIT", "name": str(company.get("name"))[:128]}},
            scope=group, deadline=deadline, idem=_key(env, tenant_id, "unit")).get("content") or {}).get("id"))
        taxpayer = state.get("taxpayerId")
        if not taxpayer:
            created = self._call("POST", "/taxpayers", {"content": {
                "type": "COMPANY",
                "name": {"legal": str(company.get("name"))[:128],
                         "trade": str(company.get("restaurant") or company.get("name"))[:128]},
                "address": address(company.get("legalAddress"), company.get("address")),
                "fiscalization": {"type": "SE", "tax_id_number": org}}},
                scope=unit, deadline=deadline, idem=_key(env, tenant_id, "taxpayer", org)).get("content") or {}
            self._commission("taxpayers", created.get("id"), unit, deadline)
            taxpayer = _save(tenant_id, env, "taxpayerId", created.get("id"))
        branch = (state.get("locations") or {}).get(location_id)
        if not branch:
            created = self._call("POST", "/locations", {"content": {
                "type": "BRANCH", "taxpayer": {"id": taxpayer},
                "name": str(company.get("restaurant") or company.get("name"))[:32],
                "address": address(None, company.get("address"))}},
                scope=unit, deadline=deadline, idem=_key(env, tenant_id, "location", location_id)).get("content") or {}
            self._commission("locations", created.get("id"), unit, deadline)
            branch = _save_location(tenant_id, env, location_id, created.get("id"))
        system = self._call("POST", "/systems", {"content": {
            "type": "FISCAL_DEVICE", "location": {"id": branch},
            "producer": {"type": "MPN", "number": PART_NUMBER,
                         "details": {"name": k.MODEL, "brand": BRAND, "model": k.MODEL,
                                     "serial": register["serialNumber"]}},
            "software": {"name": k.PROGRAM, "version": k.KASSA_VERSION},
            "label": str(register.get("label") or "")[:16]}},
            scope=unit, deadline=deadline).get("content") or {}
        if not system.get("id"):
            raise cs.Rejected("fiskaly returned no system id")
        system = self._commission("systems", system["id"], unit, deadline) or system
        number = designation(system)
        while not number and self._clock() + POLL_SECONDS < deadline:
            self._sleep(POLL_SECONDS)
            system = self._call("GET", f"/systems/{system['id']}", scope=unit, deadline=deadline).get("content") or {}
            number = designation(system)
        if not number:
            raise cs.Rejected("fiskaly returned no tillverkningsnummer")
        return cs.Enrolment(designation=number, ref={"provider": PROVIDER, "env": env, "unitId": unit,
                                                    "systemId": system["id"]})

    def _commission(self, kind, resource_id, unit, deadline):
        if not resource_id:
            raise cs.Rejected(f"fiskaly returned no {kind} id")
        return self._call("PATCH", f"/{kind}/{resource_id}", {"content": {"state": "COMMISSIONED"}},
                          scope=unit, deadline=deadline).get("content") or {}


# --- fiskaly's ids per tenant (kassa table) ------------------------------------------------

def _key(*parts):
    """A stable idempotency key: a retry within fiskaly's 24 hours creates nothing twice."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "booqy-kassa-fiskaly:" + ":".join(str(p) for p in parts)))


def _state_key(tenant_id, env):
    return {"PK": f"TENANT#{tenant_id}", "SK": f"CS#{PROVIDER}#{env}"}


def _table():
    return dynamo.table(os.environ["KASSA_TABLE_NAME"])


def _state(tenant_id, env):
    return _table().get_item(Key=_state_key(tenant_id, env), ConsistentRead=True).get("Item") or {}


def _save(tenant_id, env, field, value):
    """Keeps the first value written (two tills enrolling at once agree)."""
    if not value:
        raise cs.Rejected(f"fiskaly returned no {field}")
    result = _table().update_item(
        Key=_state_key(tenant_id, env), UpdateExpression="SET #f = if_not_exists(#f, :v), updatedAt = :now",
        ExpressionAttributeNames={"#f": field},
        ExpressionAttributeValues={":v": value, ":now": datetime.now(timezone.utc).isoformat()},
        ReturnValues="ALL_NEW")
    return result["Attributes"][field]


def _save_location(tenant_id, env, location_id, value):
    if not value:
        raise cs.Rejected("fiskaly returned no location id")
    _table().update_item(Key=_state_key(tenant_id, env),
                         UpdateExpression="SET locations = if_not_exists(locations, :empty)",
                         ExpressionAttributeValues={":empty": {}})
    result = _table().update_item(
        Key=_state_key(tenant_id, env), UpdateExpression="SET locations.#l = if_not_exists(locations.#l, :v)",
        ExpressionAttributeNames={"#l": location_id}, ExpressionAttributeValues={":v": value},
        ReturnValues="ALL_NEW")
    return result["Attributes"]["locations"][location_id]

