"""A fake fiskaly SIGN SE (Unified API 2026-06-01) for tests: the transport
FiskalyControlSystem takes. Every request body is validated against
fiskaly's own OpenAPI spec (tests/fixtures/fiskaly, the published
fiskaly.uapi.sign-se.2026-06-01.yaml), so a payload fiskaly would refuse
fails here. Answers follow fiskaly's documented examples (Postman collection,
integration guide): {"content": {...}}, the control code in
compliance.data, the status in compliance.status, the tillverkningsnummer in
compliance.technical_security_system_id. No network.

Keys as in fiskaly TEST (2026-10-26): Booqy's GROUP key may only create
organizations and subjects; anything inside a UNIT (taxpayers, locations,
systems, records) needs that UNIT's own key from POST /subjects, and the
GROUP token gets 405 "non-unit organization" there, X-Scope-Identifier or
not. Errors carry their code and message in "content", as fiskaly's do."""

import json
import re
import uuid
from pathlib import Path

import jsonschema
import yaml

SPEC_PATH = Path(__file__).parent / "fixtures" / "fiskaly" / "fiskaly.uapi.sign-se.2026-06-01.yaml"
_SPEC = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
_ROOT = {"components": _SPEC["components"]}
REQUEST_SCHEMAS = {
    ("POST", "/tokens"): "TokenCreateRequest", ("POST", "/organizations"): "OrganizationCreateRequest",
    ("POST", "/taxpayers"): "TaxpayerCreateRequest", ("PATCH", "/taxpayers"): "TaxpayerUpdateRequest",
    ("POST", "/locations"): "LocationCreateRequest", ("PATCH", "/locations"): "LocationUpdateRequest",
    ("POST", "/systems"): "SystemCreateRequest", ("PATCH", "/systems"): "SystemUpdateRequest",
    ("POST", "/records"): "RecordCreateRequest", ("POST", "/subjects"): "SubjectCreateRequest",
}
UNIT_PATHS = ("/taxpayers", "/locations", "/systems", "/records")
TEST_KEY = "test-key"
TEST_SECRET = "test-secret-value"


def validate(schema_name, body):
    """Errors of `body` against a component schema of fiskaly's spec."""
    schema = {"$ref": f"#/components/schemas/{schema_name}", **_ROOT}
    return [error.message for error in jsonschema.Draft7Validator(schema).iter_errors(body)]


def uuid7():
    raw = uuid.uuid4().hex
    return f"{raw[:8]}-{raw[8:12]}-7{raw[13:16]}-8{raw[17:20]}-{raw[20:32]}"


def code(n):
    """A 113-character control code, as fiskaly's TCS returns."""
    return (f"SE{n:04d}" + "ABCDEF0123456789" * 8)[:113]


class FakeFiskaly:
    def __init__(self):
        self.calls = []
        self.status = 1                 # status code of OPENING and TRANSACTION records
        self.status_in = "compliance"   # compliance | logs | data
        self.fail = []                  # [(path prefix, HTTP status), ...] answered before the real handler
        self.processing = 0             # first GETs of a record still PROCESSING
        self.copy_code_on_intention = False
        self.tokens = 0
        self.records = {}
        self.counter = 0
        self.expired = set()
        self.units = set()
        self.unit_keys = {}             # key -> (secret, unit id)
        self.bearers = {}               # bearer -> organization id

    def add_unit(self, unit_id):
        """A UNIT with its own key, as if enrolled earlier: (key, secret)."""
        self.units.add(unit_id)
        key, secret = f"unit-key-{len(self.unit_keys) + 1}", f"unit-secret-{len(self.unit_keys) + 1}"
        self.unit_keys[key] = (secret, unit_id)
        return key, secret

    @staticmethod
    def _error(status, code, message):
        return status, {}, {"content": {"status": status, "code": code, "error": code, "message": message}}

    # transport(method, url, headers, body, timeout) -> (status, headers, json)
    def __call__(self, method, url, headers, body, timeout):
        path = url.split("fiskaly.com", 1)[1]
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "path": path, "headers": dict(headers), "body": payload})
        for i, (prefix, status) in enumerate(self.fail):
            if path.startswith(prefix):
                self.fail.pop(i)
                if status == "timeout":
                    raise TimeoutError("timed out")
                return status, {}, {"status": status, "code": "E_FAKE", "error": "fake", "message": "fake failure"}
        assert headers.get("X-Api-Version") == "2026-06-01"
        if method in ("POST", "PATCH"):
            assert headers.get("X-Idempotency-Key"), "fiskaly requires X-Idempotency-Key on POST and PATCH"
            assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[34][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                                headers["X-Idempotency-Key"]), "fiskaly accepts only UUID v3 or v4 idempotency keys"
        base = "/" + path.strip("/").split("/")[0].split("?")[0]
        schema = REQUEST_SCHEMAS.get((method, base))
        if schema:
            errors = validate(schema, payload)
            if errors:
                return 400, {}, {"status": 400, "code": "E_BAD_REQUEST", "error": "Bad Request", "message": errors[0]}
        if path == "/tokens":
            key, secret = payload["content"]["key"], payload["content"]["secret"]
            if key == TEST_KEY and secret == TEST_SECRET:
                org, prefix = "group-org", "jwt-"
            elif key in self.unit_keys and self.unit_keys[key][0] == secret:
                org, prefix = self.unit_keys[key][1], "jwt-u"
            else:
                return self._error(401, "E_UNAUTHORIZED", "invalid key")
            self.tokens += 1
            bearer = f"{prefix}{self.tokens}"
            self.bearers[bearer] = org
            return 200, {}, {"content": {"id": uuid7(), "authentication": {
                "type": "JWT", "bearer": bearer, "expires_at": "2099-01-01T00:00:00Z",
                "issued_at": "2026-10-25T10:00:00Z"}, "organization": {"id": org}}}
        bearer = headers.get("Authorization", "").removeprefix("Bearer ")
        if bearer not in self.bearers or bearer in self.expired:
            return self._error(401, "E_UNAUTHORIZED", "expired")
        org, scope = self.bearers[bearer], headers.get("X-Scope-Identifier")
        if base in UNIT_PATHS:
            if org == "group-org":
                return self._error(405, "E_METHOD_NOT_ALLOWED",
                                   f"operation invalid for non-unit organization '{org}'")
            if scope != org:
                return self._error(403, "E_FORBIDDEN", "scope is not the token's unit")
        if method == "POST" and base == "/organizations":
            assert org == "group-org" and scope == "group-org"
            unit_id = uuid7()
            self.units.add(unit_id)
            return 200, {}, {"content": {"id": unit_id, "state": "ENABLED", "organization": {"id": org},
                                         **payload["content"]}}
        if method == "POST" and base == "/subjects":
            if org != "group-org" or scope not in self.units:
                return self._error(403, "E_FORBIDDEN", "subjects need the group key and a unit scope")
            key, secret = self.add_unit(scope)
            return 200, {}, {"content": {"id": str(uuid.uuid4()), "state": "ENABLED", **payload["content"],
                                         "credentials": {"key": key, "secret": secret}}}
        if method == "POST" and base in ("/taxpayers", "/locations"):
            return 200, {}, {"content": {"id": uuid7(), "state": "ACQUIRED", **payload["content"]}}
        if method == "POST" and base == "/systems":
            return 200, {}, {"content": {"id": uuid7(), "state": "ACQUIRED", "mode": "INACTIVE"}}
        if method == "PATCH" and base == "/systems":
            if payload["content"].get("state") == "COMMISSIONED":
                self.counter += 1
                return 200, {}, {"content": {"id": path.split("/")[2], "state": "COMMISSIONED", "mode": "OPERATIVE",
                                             "compliance": {"software": {"name": "Booqy Kassa", "version": "1.0.0"},
                                                            "technical_security_system_id":
                                                                f"SE01BQ{self.counter:010d}"}}}
            return 200, {}, {"content": {"id": path.split("/")[2], "state": payload["content"]["state"]}}
        if method == "PATCH":
            return 200, {}, {"content": {"id": path.split("/")[2], **payload["content"]}}
        if method == "POST" and base == "/records":
            return 200, {}, {"content": self._record(payload["content"])}
        if method == "GET" and base == "/records":
            record = self.records[path.split("/")[2]]
            if self.processing:
                self.processing -= 1
                return 200, {}, {"content": {**record, "mode": "PROCESSING"}}
            return 200, {}, {"content": {**record, "mode": "FINISHED"}}
        return 404, {}, {"status": 404, "code": "E_NOT_FOUND", "error": "Not Found", "message": path}

    NAMES = {1: "STATUS_OK", 2: "NO_CONTACT_WITH_KS", 3: "CONTACT_WITH_KS", 4: "NOT_CONFIGURED",
             5: "CONTACT_MANUFACTURER", 6: "REPORT_ERROR_24H", 7: "ANMAL_FEL_48H"}

    def _record(self, content):
        record_id = uuid7()
        kind, operation = content["type"], content["operation"]["type"]
        record = {"id": record_id, "type": f"{kind}::{operation}", "state": "COMPLETED", "mode": "FINISHED",
                  "system": content.get("system") or {"id": "sys"}}
        signed = kind == "TRANSACTION" or operation == "OPENING" or (
            operation == "DUPLICATE" and self.copy_code_on_intention)
        if signed:
            training = (content["operation"].get("details") or {}).get("training")
            self.counter += 1
            ok = self.status == 1
            data = code(self.counter) if ok and not training and operation != "OPENING" else ""
            if not ok:
                record["state"] = "FAILED" if self.status > 2 else "COMPLETED"
            if self.status_in == "compliance":
                record["compliance"] = {"status": self.status, **({"data": data} if data else {})}
            elif self.status_in == "data":
                record["compliance"] = {"data": f"SE,{self.status}" + (f",{data}" if data else "")}
            else:
                record["logs"] = [{"severity": "WARNING", "message": f"TCS status {self.NAMES[self.status]}"}]
                if data:
                    record["compliance"] = {"data": data}
        if self.processing and kind == "TRANSACTION":
            record["mode"] = "PROCESSING"
        self.records[record_id] = record
        return record

    def posted(self, path):
        return [c for c in self.calls if c["method"] == "POST" and c["path"] == path]
