"""The control system port for Kassa (shared/kassa.py).

Skatteverket requires every cash register to be connected to a certified
control unit or control system (SKVFS 2014:9 3 kap. 1 §, 4 kap. 9 §). The
register sends each receipt's data and must get a control code back before
the sale or return is registered; without it, no receipt.

The certified provider is not chosen yet (docs/KASSA-REQUIREMENTS.md), so
the till talks to this one small interface:

    status(register)            -> Status: may a receipt be made right now?
    sign(register, data)        -> Registration (control code, the control
                                   system's manufacturing number, offline flag)
                                   or raises Unreachable
    register_register(register) -> enrol a new register with the provider
    pause_register(register)    -> take a register out of use

and the only implementation is SimulatedControlSystem for dev and test
mode. The likely provider (fiskaly SIGN SE: one TLS request per sale with
a client certificate, a control code back, registers managed by API) and
the other certified ones (Axena KS4POS, Loomis Control System) fit this
shape; a real provider becomes one adapter class here, and nothing
provider-specific lives outside it.

ReceiptData carries what SKVFS 2020:9 5 kap. 34 § (tabell A, B, C) says a
control program receives per receipt, and kvittodata() renders it in that
format, so a provider adapter only maps fields.
"""

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from datetime import datetime

NORMAL = "normal"      # kassakvitto or returkvitto (tabell A)
COPY = "kopia"         # kvittokopia (tabell B)
TRAINING = "ovning"    # övningskvitto (tabell C)
KINDS = (NORMAL, COPY, TRAINING)

# Status codes a control program sends (SKVFS 2020:9 5 kap. 12 §).
STATUS_OK = "STATUS_OK"
STATUS_NO_CONTACT = "EJ_KONTAKT_MED_KS"
STATUS_CONTACT_AGAIN = "KONTAKT_MED_KS"
STATUS_NOT_CONFIGURED = "EJ_KONFIGURERAD"
STATUS_CALL_MANUFACTURER = "KONTAKTA_TILLVERKARE"
STATUS_REPORT_24H = "ANMAL_FEL_24H"
STATUS_REPORT_48H = "ANMAL_FEL_48H"
# The provider itself says the fault must be reported or fixed by it.
REPORT_NOW = (STATUS_CALL_MANUFACTURER, STATUS_REPORT_24H, STATUS_REPORT_48H)

CODE_LENGTH = 59                  # Kontrollkod in Skatteverket's XML schema
SERIAL_LENGTH = 17                # TillverkningsnummerForKontrollsystemet
SIMULATOR_SERIAL = "BOOQYSIMULATOR001"
SIMULATOR = "simulator"


class Unreachable(Exception):
    """The control system can't be asked right now: no receipt may be made."""

    def __init__(self, status=STATUS_NO_CONTACT):
        super().__init__(status)
        self.status = status


@dataclass(frozen=True)
class Status:
    ok: bool
    code: str
    provider: str
    serial: str


@dataclass(frozen=True)
class Registration:
    control_code: str
    serial: str
    offline: bool = False


def _amount(ore):
    """Öre -> "100,95" (two decimals, comma, as SKVFS 2020:9 writes them)."""
    ore = int(ore)
    sign = "-" if ore < 0 else ""
    ore = abs(ore)
    return f"{sign}{ore // 100},{ore % 100:02d}"


def _rate(prefix, rate):
    """25 -> "F250%" (tens, ones, tenths)."""
    tenths = int(round(float(rate) * 10))
    return f"{prefix}{tenths:03d}%"


@dataclass(frozen=True)
class ReceiptData:
    """One receipt as the control system sees it. Amounts in öre, rates in
    percent, `at` in the register's local (Swedish) time."""

    kind: str
    at: datetime
    org_number: str                 # 10 digits
    register_label: str             # kassabeteckning, max 16
    address: str                    # max 40
    serial: int                     # löpnummer
    sales_by_rate: dict = field(default_factory=dict)    # {rate: gross öre}
    returns_by_rate: dict = field(default_factory=dict)  # {rate: gross öre, positive}
    vat_by_rate: dict = field(default_factory=dict)      # {rate: VAT öre}
    original_at: datetime | None = None                  # copies
    original_serial: int | None = None                   # copies

    def kvittodata(self):
        """The fields of SKVFS 2020:9 5 kap. 34 § in their formats."""
        when = self.at.strftime("%Y%m%d%H%M%S")
        org = "".join(ch for ch in self.org_number if ch.isdigit())[-10:]
        label = self.register_label[:16]
        if self.kind == COPY:
            return {"K1": when, "K2": self.original_at.strftime("%Y%m%d%H%M%S") if self.original_at else "",
                    "K3": org, "K4": label, "K5": str(self.serial), "K6": str(self.original_serial or "")}
        if self.kind == TRAINING:
            return {"O1": when, "O2": org, "O3": label, "O4": str(self.serial)}
        sales = sum(self.sales_by_rate.values())
        returns = sum(self.returns_by_rate.values())
        return {
            "N1": when, "N2": org, "N3": label, "N4": self.address[:40], "N5": str(self.serial),
            "N6": "".join(_rate("F", r) + _amount(v) for r, v in sorted(self.sales_by_rate.items())),
            "N7": _amount(sales),
            "N8": "".join(_rate("R", r) + _amount(v) for r, v in sorted(self.returns_by_rate.items())),
            "N9": _amount(returns),
            "N10": "".join(_rate("M", r) + _amount(v) for r, v in sorted(self.vat_by_rate.items())),
        }

    def canonical(self):
        return "|".join(f"{k}={v}" for k, v in self.kvittodata().items()) + f"|kind={self.kind}"


class ControlSystem:
    """The port. `register` is the kassa register row (shared/kassa.py)."""

    provider = ""

    def status(self, register):
        raise NotImplementedError

    def sign(self, register, data):
        raise NotImplementedError

    def register_register(self, register):
        raise NotImplementedError

    def pause_register(self, register):
        raise NotImplementedError


class SimulatedControlSystem(ControlSystem):
    """Dev and test mode only: a deterministic code (HMAC-SHA256 of the
    receipt data) and Skatteverket-shaped answers. Not certified - Kassa
    shows "Testläge" while this is in use. "Kontrollsystemet nere" in the
    till's test tools (the register's ``simulateDown``) makes it unreachable,
    so the error path can be tried."""

    provider = SIMULATOR

    def __init__(self, key=None):
        self._key = (key or os.environ.get("KASSA_SIMULATOR_KEY") or "booqy-kassa-simulator").encode()

    def status(self, register):
        down = bool((register or {}).get("simulateDown"))
        return Status(ok=not down, code=STATUS_NO_CONTACT if down else STATUS_OK, provider=self.provider,
                      serial=SIMULATOR_SERIAL)

    def register_register(self, register):
        return None

    def pause_register(self, register):
        return None

    def sign(self, register, data):
        if (register or {}).get("simulateDown"):
            raise Unreachable()
        digest = hmac.new(self._key, data.canonical().encode("utf-8"), hashlib.sha256).hexdigest().upper()
        code = (digest + digest)[:CODE_LENGTH]
        return Registration(control_code=code, serial=SIMULATOR_SERIAL)


def for_location(location):
    """The control system of a location. Only the simulator exists today;
    a configured real provider would be picked here."""
    return SimulatedControlSystem()


def certified(system):
    """Whether receipts from `system` may be presented as real ones: never
    for the simulator, and only once the owner switched the label
    (KASSA_CERTIFIED, Terraform var kassa_certified)."""
    return system.provider != SIMULATOR and os.environ.get("KASSA_CERTIFIED") == "true"
