"""The real fortnox client, a skeleton until feature I (accounting-sync) builds it:
every method is the port's and raises NotImplementedError, so
FORTNOX_CLIENT=real fails loudly instead of pretending to work."""

from shared.fortnox import AccountingProviderClient


class RealFortnox(AccountingProviderClient):
    pass
