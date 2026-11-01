"""Google Business Profile port (CONTRACTS.md 13, feature D). `client()`
picks the simulator or shared/google_business_real.py from
GOOGLE_BUSINESS_CLIENT."""

from dataclasses import dataclass

from shared import external_client


class GoogleBusinessError(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Token:
    refresh_token: str
    access_token: str
    account_name: str


class GoogleBusiness:
    def authorize_url(self, state, redirect_uri): raise NotImplementedError
    def exchange_code(self, code, redirect_uri): raise NotImplementedError
    def refresh(self, refresh_token): raise NotImplementedError
    def list_locations(self, token): raise NotImplementedError
    def patch_hours(self, token, location_name, regular_hours, special_hours): raise NotImplementedError
    def revoke(self, token): raise NotImplementedError


class SimulatedGoogleBusiness(external_client.Simulated, GoogleBusiness):
    """Two locations (locations/sim-1, locations/sim-2); patched hours are
    kept in `hours`, every call in `calls`."""

    error = GoogleBusinessError

    def __init__(self, fail=None):
        super().__init__(fail)
        self.hours = {}

    def authorize_url(self, state, redirect_uri):
        self._call("authorize_url", state)
        return f"{redirect_uri}?code=simulated&state={state}"

    def exchange_code(self, code, redirect_uri):
        self._call("exchange_code", code)
        if code != "simulated":
            raise GoogleBusinessError("invalid_code")
        return Token("sim-refresh", "sim-access", "accounts/sim")

    def refresh(self, refresh_token):
        self._call("refresh")
        return "sim-access"

    def list_locations(self, token):
        self._call("list_locations")
        return [{"name": f"locations/{i}", "title": f"Simulerad restaurang {n}", "address": f"Simgatan {n}",
                 "new_review_uri": f"https://example.invalid/review/{i}"}
                for n, i in ((1, "sim-1"), (2, "sim-2"))]

    def patch_hours(self, token, location_name, regular_hours, special_hours):
        self._call("patch_hours", location_name)
        if location_name not in ("locations/sim-1", "locations/sim-2"):
            raise GoogleBusinessError("not_found")
        self.hours[location_name] = {"regular": regular_hours, "special": special_hours}

    def revoke(self, token):
        self._call("revoke")


def client(**kwargs) -> GoogleBusiness:
    if external_client.mode("google_business") == external_client.REAL:
        from shared.google_business_real import RealGoogleBusiness
        return RealGoogleBusiness()
    return SimulatedGoogleBusiness(**kwargs)
