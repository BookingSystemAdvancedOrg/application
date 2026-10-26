"""The control system port for Kassa (shared/kassa.py).

Skatteverket requires every cash register to be connected to a certified
control unit or control system (SKVFS 2014:9 3 kap. 1 §, 4 kap. 9 §). The
register sends each receipt's data and must get a control code back before
the sale or return is registered; without it, no receipt.

The till talks to this one small interface:

    status(register)            -> Status: may a receipt be made right now?
                                   (the startup check; also asked before
                                   every card payment and return)
    sign(register, data)        -> Registration (control code, the control
                                   system's manufacturing number, the
                                   provider's reference) or raises
                                   Unreachable / Rejected
    register_register(register, company)
                                -> Enrolment (the kassabeteckning the
                                   provider assigned) or None
    pause_register(register)    -> take a register out of use
    close_day(register)         -> the day ended (after the Z report)

Two implementations: SimulatedControlSystem (dev and test mode) and the
fiskaly SIGN SE adapter (shared/control_system_fiskaly.py), picked by
KASSA_CONTROL_SYSTEM (Terraform var kassa_control_system). Nothing
provider-specific lives outside the adapter; another certified provider
(Axena KS4POS, Loomis Control System) would be one more adapter.

ReceiptData carries what SKVFS 2020:9 5 kap. 34 § (tabell A, B, C) says a
control program receives per receipt, and kvittodata() renders it in that
format; it also carries the receipt's lines and payments for providers
that want them (fiskaly does).
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
AVSTAMNING_LENGTH = 113           # Avstamningskod (a control system's code, SKVFS 2020:9)
SERIAL_LENGTH = 17                # TillverkningsnummerForKontrollsystemet
DESIGNATION_MAX = 16              # kassabeteckning (tabell A N3)
SIMULATOR_SERIAL = "BOOQYSIMULATOR001"
SIMULATOR = "simulator"


class Unreachable(Exception):
    """The control system can't be asked right now, or answered with a status
    code that forbids a receipt: no receipt may be made."""

    def __init__(self, status=STATUS_NO_CONTACT):
        super().__init__(status)
        self.status = status


class Rejected(Exception):
    """The control system refused the request itself (our data was wrong):
    no receipt, and retrying the same request won't help."""


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
    ref: dict | None = None           # the provider's ids for this receipt (copies refer to it)
    message: str = ""                 # the control system's text for staff ("Kvitto får skrivas ut")


@dataclass(frozen=True)
class Enrolment:
    designation: str                  # kassabeteckning the provider assigned (max 16)
    ref: dict | None = None           # the provider's ids for the register


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
    original_serial: int | None = None                   # copies; returns: the sale returned
    original_ref: dict | None = None                     # copies: Registration.ref of the original
    lines: tuple = ()        # ({"text", "quantity", "total", "rate", "vat"}, ...) gross öre after discount
    payments: tuple = ()     # ({"method": "card", "amount", "last4", "funding"}, ...)
    user: str = ""

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
    """The port. `register` is the kassa register row (shared/kassa.py),
    `company` kassa.company() (name, org, address, addressParts)."""

    provider = ""
    serial = ""              # TillverkningsnummerForKontrollsystemet / kontrollserverns ID
    certifiable = True       # False: receipts are never real (simulator, a provider's test environment)
    enrols = True            # register_register needs the company (org.nr, address)
    name = ""                # shown to staff
    control_unit = None      # what the merchant reports to Skatteverket as the kontrollenhet

    def status(self, register):
        raise NotImplementedError

    def sign(self, register, data):
        raise NotImplementedError

    def register_register(self, register, company):
        raise NotImplementedError

    def pause_register(self, register):
        raise NotImplementedError

    def close_day(self, register):
        return None


class SimulatedControlSystem(ControlSystem):
    """Dev and test mode only: a deterministic code (HMAC-SHA256 of the
    receipt data) and Skatteverket-shaped answers. Not certified - Kassa
    shows "Testläge" while this is in use. "Kontrollsystemet nere" in the
    till's test tools (the register's ``simulateDown``) makes it unreachable,
    so the error path can be tried."""

    provider = SIMULATOR
    serial = SIMULATOR_SERIAL
    certifiable = False
    enrols = False
    name = "Simulator (testläge)"

    def __init__(self, key=None):
        self._key = (key or os.environ.get("KASSA_SIMULATOR_KEY") or "booqy-kassa-simulator").encode()

    def status(self, register):
        down = bool((register or {}).get("simulateDown"))
        return Status(ok=not down, code=STATUS_NO_CONTACT if down else STATUS_OK, provider=self.provider,
                      serial=SIMULATOR_SERIAL)

    def register_register(self, register, company):
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
    """The control system of a location: the one the environment is set to
    (KASSA_CONTROL_SYSTEM: "fiskaly" or, by default, the simulator)."""
    if os.environ.get("KASSA_CONTROL_SYSTEM") == "fiskaly":
        from shared.control_system_fiskaly import FiskalyControlSystem
        return FiskalyControlSystem()
    return SimulatedControlSystem()


def certified(system):
    """Whether receipts from `system` may be presented as real ones: never
    for the simulator or a provider's test environment, and only once the
    owner switched the label (KASSA_CERTIFIED, Terraform var kassa_certified)."""
    return (system.provider != SIMULATOR and bool(system.certifiable)
            and os.environ.get("KASSA_CERTIFIED") == "true")
