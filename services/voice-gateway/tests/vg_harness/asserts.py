"""Checks a Result against its Scenario.expect. Owner: builder 'harness'.
One function per Expect field; check(scenario, result) runs every set
field and returns a list of failures (empty: pass), each naming the field,
the expected and the observed value."""

from __future__ import annotations

from vg_harness.runner import Result
from vg_harness.scenarios import Scenario


def check(scenario: Scenario, result: Result) -> list[str]:
    raise NotImplementedError
