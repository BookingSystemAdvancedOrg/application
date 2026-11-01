"""Picking an external client: simulator or real (CONTRACTS.md section 13).

Same rule as shared/control_system.py: one env var per client, set by a
validated Terraform variable (`<x>_client`, default "simulator"). The
simulator is refused at runtime when ENVIRONMENT=prod, so prod keeps
planning while the features are off; an unknown value raises.
"""

import os

from shared import contracts

SIMULATOR = "simulator"
REAL = "real"


def mode(name: str) -> str:
    """"simulator" | "real" for the client `name` (a key of contracts.CLIENTS)."""
    env_var = contracts.CLIENTS[name]["env"]
    value = os.environ.get(env_var) or SIMULATOR
    if value not in (SIMULATOR, REAL):
        raise ValueError(f"{env_var} must be simulator or real")
    if value == SIMULATOR and os.environ.get("ENVIRONMENT") == "prod":
        raise RuntimeError(f"{name} simulator is not allowed in prod")
    return value


class Simulated:
    """Base of the simulators: `fail={"method": n}` makes the first n calls
    of `method` raise the port's error (retryable), for resume tests."""

    error = Exception

    def __init__(self, fail=None):
        self._fail = dict(fail or {})
        self.calls = []

    def _call(self, method, *args):
        self.calls.append((method, *args))
        if self._fail.get(method, 0) > 0:
            self._fail[method] -= 1
            raise self.error("unavailable", True)
