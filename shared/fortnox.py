"""Fortnox client (CONTRACTS.md 13, feature I) on the shared accounting
provider port. Visma (shared/visma.py) uses the same port and simulator.
`client()` picks the simulator or shared/fortnox_real.py from
FORTNOX_CLIENT."""

from dataclasses import dataclass
from typing import Optional

from shared import external_client

ERROR_CODES = ("account_missing", "financial_year_missing", "period_locked", "unauthorized",
               "rate_limited", "unavailable")


class AccountingProviderError(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Token:
    access_token: str
    refresh_token: str
    expires_at: str
    company_id: str
    org_number: str
    company_name: str


@dataclass(frozen=True)
class Voucher:
    series: str
    date: str
    description: str
    rows: tuple = ()   # ({account, debit_ore, credit_ore, text, cost_center?}, ...)


@dataclass(frozen=True)
class Ref:
    id: str
    series: str
    number: int


class AccountingProviderClient:
    provider = ""

    def authorize_url(self, state, redirect_uri): raise NotImplementedError
    def exchange_code(self, code, redirect_uri): raise NotImplementedError
    def refresh(self, refresh_token): raise NotImplementedError
    def post_voucher(self, token, voucher): raise NotImplementedError
    def find_voucher(self, token, date, series, text_contains) -> Optional[Ref]: raise NotImplementedError
    def revoke(self, token): raise NotImplementedError


class SimulatedAccountingProvider(external_client.Simulated, AccountingProviderClient):
    """An in-memory company ledger numbered per series. Refresh tokens
    rotate; a used one raises `unauthorized`. `missing_accounts={"2421"}`
    makes vouchers using them fail with `account_missing`."""

    error = AccountingProviderError
    provider = "simulator"

    def __init__(self, fail=None, missing_accounts=(), org_number="5566778899",
                 company_name="Simulerat AB"):
        super().__init__(fail)
        self.missing_accounts = {str(a) for a in missing_accounts}
        self.org_number = org_number
        self.company_name = company_name
        self.vouchers = []          # [(Ref, Voucher)]
        self._live_refresh = set()
        self._issued = 0

    def _token(self):
        self._issued += 1
        refresh = f"sim-refresh-{self.provider}-{self._issued}"
        self._live_refresh.add(refresh)
        return Token(f"sim-access-{self.provider}-{self._issued}", refresh, "2099-01-01T00:00:00Z",
                     f"sim-company-{self.provider}", self.org_number, self.company_name)

    def authorize_url(self, state, redirect_uri):
        self._call("authorize_url", state)
        return f"{redirect_uri}?code=simulated&state={state}"

    def exchange_code(self, code, redirect_uri):
        self._call("exchange_code", code)
        if code != "simulated":
            raise AccountingProviderError("unauthorized")
        return self._token()

    def refresh(self, refresh_token):
        self._call("refresh")
        if refresh_token not in self._live_refresh:
            raise AccountingProviderError("unauthorized")
        self._live_refresh.discard(refresh_token)
        return self._token()

    def post_voucher(self, token, voucher):
        self._call("post_voucher", voucher.series, voucher.date)
        missing = {str(r["account"]) for r in voucher.rows} & self.missing_accounts
        if missing:
            raise AccountingProviderError("account_missing")
        if sum(int(r.get("debit_ore") or 0) - int(r.get("credit_ore") or 0) for r in voucher.rows) != 0:
            raise AccountingProviderError("unbalanced")
        number = 1 + sum(1 for ref, _ in self.vouchers if ref.series == voucher.series)
        ref = Ref(f"{voucher.series}-{number}", voucher.series, number)
        self.vouchers.append((ref, voucher))
        return ref

    def find_voucher(self, token, date, series, text_contains):
        self._call("find_voucher", date, series)
        for ref, voucher in self.vouchers:
            if voucher.date == date and ref.series == series and text_contains in voucher.description:
                return ref
        return None

    def revoke(self, token):
        self._call("revoke")


class SimulatedFortnox(SimulatedAccountingProvider):
    provider = "fortnox"


FortnoxClient = AccountingProviderClient


def client(**kwargs) -> AccountingProviderClient:
    if external_client.mode("fortnox") == external_client.REAL:
        from shared.fortnox_real import RealFortnox
        return RealFortnox()
    return SimulatedFortnox(**kwargs)
