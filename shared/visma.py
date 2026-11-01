"""Visma eEkonomi client (CONTRACTS.md 13, feature I): the same port and
simulator as Fortnox (shared/fortnox.py). `client()` picks the simulator
or shared/visma_real.py from VISMA_CLIENT."""

from shared import external_client
from shared.fortnox import (AccountingProviderClient, AccountingProviderError, Ref, SimulatedAccountingProvider,
                            Token, Voucher)

__all__ = ["AccountingProviderClient", "AccountingProviderError", "Ref", "Token", "Voucher", "VismaClient",
           "SimulatedVisma", "client"]


class SimulatedVisma(SimulatedAccountingProvider):
    provider = "visma"


VismaClient = AccountingProviderClient


def client(**kwargs) -> AccountingProviderClient:
    if external_client.mode("visma") == external_client.REAL:
        from shared.visma_real import RealVisma
        return RealVisma()
    return SimulatedVisma(**kwargs)
