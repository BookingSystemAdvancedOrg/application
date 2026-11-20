"""Scenarios voice A to F and language A to H through the whole gateway
(builder 'harness'). Skipped until the harness and the components exist;
VG_HARNESS=live runs them against AWS (booqy-dev, costs cents)."""

import asyncio
import os

import pytest

from vg_harness.scenarios import ALL

pytestmark = pytest.mark.skipif(os.environ.get("VG_HARNESS") not in ("fake", "live"),
                                reason="set VG_HARNESS=fake or live (builder 'harness' enables fake in CI)")


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.key)
def test_scenario(scenario):
    from vg_harness.asserts import check
    from vg_harness.runner import run
    result = asyncio.run(run(scenario, os.environ["VG_HARNESS"]))
    assert check(scenario, result) == []
