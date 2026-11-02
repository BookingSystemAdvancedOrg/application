"""Telephony port (CONTRACTS.md 13, feature L, ADR-L decision 4): one
Swedish number per enabled location, inbound calls to the voice provider's
SIP target with the restaurant's phone as fallback, transfer, signed
webhooks. `client()` picks the simulator or shared/telephony_real.py
(46elks, owned by L) from TELEPHONY_CLIENT."""

import base64
import json
from dataclasses import dataclass, field

from shared import external_client
from shared.voice_provider import TransferResult

EVENT_KINDS = ("inbound", "ended")
SIM_BASIC_AUTH = "Basic " + base64.b64encode(b"sim:sim").decode()


class TelephonyError(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Number:
    e164: str
    provider_id: str


@dataclass(frozen=True)
class ForwardingCheck:
    status: str                    # "ok" | "failed" | "unsupported"
    detail: str | None = None


@dataclass(frozen=True)
class TelephonyEvent:
    kind: str                      # one of EVENT_KINDS
    call_ref: str | None = None
    data: dict = field(default_factory=dict)


class Telephony:
    def provision_number(self, country, label): raise NotImplementedError
    def release_number(self, provider_id): raise NotImplementedError
    def set_inbound(self, provider_id, sip, fallback_e164): raise NotImplementedError
    def transfer(self, call_ref, to_e164, timeout_s): raise NotImplementedError
    def parse_webhook(self, headers, body): raise NotImplementedError
    def verify_forwarding(self, e164): raise NotImplementedError


class SimulatedTelephony(external_client.Simulated, Telephony):
    """Numbers `+4610000<n:04d>` handed out in order (kept in `numbers`,
    provider id `sim-number-<n:04d>`); `numbers_left=0` makes
    `provision_number` raise `number_unavailable`; `set_inbound` keeps the
    target per number in `inbound`; `transfer` answers `transfer_answered`.
    Webhooks: Basic `sim:sim` and `{"events": [{"kind", "call_ref", ...}]}`,
    anything else raises `unauthorized`. `verify_forwarding` is
    `unsupported`."""

    error = TelephonyError

    def __init__(self, fail=None, numbers_left=None, transfer_answered=True):
        super().__init__(fail)
        self.numbers_left = numbers_left
        self.transfer_answered = transfer_answered
        self.numbers = {}
        self.inbound = {}
        self._issued = 0

    def provision_number(self, country="SE", label=""):
        self._call("provision_number", country, label)
        if country != "SE":
            raise TelephonyError("unsupported_country")
        if self.numbers_left is not None:
            if self.numbers_left <= 0:
                raise TelephonyError("number_unavailable")
            self.numbers_left -= 1
        self._issued += 1
        number = Number(f"+4610000{self._issued:04d}", f"sim-number-{self._issued:04d}")
        self.numbers[number.provider_id] = {"e164": number.e164, "label": label}
        return number

    def release_number(self, provider_id):
        self._call("release_number", provider_id)
        self.numbers.pop(provider_id, None)
        self.inbound.pop(provider_id, None)

    def set_inbound(self, provider_id, sip, fallback_e164):
        self._call("set_inbound", provider_id)
        if provider_id not in self.numbers:
            raise TelephonyError("not_found")
        self.inbound[provider_id] = {"sip": sip, "fallback": fallback_e164}

    def transfer(self, call_ref, to_e164, timeout_s):
        self._call("transfer", call_ref, to_e164, timeout_s)
        return TransferResult(bool(self.transfer_answered))

    def parse_webhook(self, headers, body):
        self._call("parse_webhook")
        auth = next((v for k, v in (headers or {}).items() if str(k).lower() == "authorization"), None)
        if auth != SIM_BASIC_AUTH:
            raise TelephonyError("unauthorized")
        try:
            raw = json.loads(body if isinstance(body, (str, bytes, bytearray)) else json.dumps(body))
            events = []
            for item in raw["events"]:
                if item["kind"] not in EVENT_KINDS:
                    raise ValueError(item["kind"])
                data = {k: v for k, v in item.items() if k not in ("kind", "call_ref")}
                events.append(TelephonyEvent(item["kind"], item.get("call_ref"), data))
        except (TypeError, ValueError, KeyError, AttributeError):
            raise TelephonyError("unauthorized") from None
        return events

    def verify_forwarding(self, e164):
        self._call("verify_forwarding", e164)
        return ForwardingCheck("unsupported")


def client(**kwargs) -> Telephony:
    if external_client.mode("telephony") == external_client.REAL:
        from shared.telephony_real import RealTelephony
        return RealTelephony()
    return SimulatedTelephony(**kwargs)
