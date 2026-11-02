"""The real visma client, a skeleton until feature I (accounting-sync) builds it:
every method is the port's and raises NotImplementedError, so
VISMA_CLIENT=real fails loudly instead of pretending to work."""

from shared.visma import AccountingProviderClient


class RealVisma(AccountingProviderClient):
    pass
