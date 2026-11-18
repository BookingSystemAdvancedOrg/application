"""The phone agent evaluation scenarios (tests/phone_agent_scripts) as tests.

In CI the simulator plays the Swedish and English "sim" lines of every
scenario (wiring: safety, outcome and language graders). With
AGENT_MODEL_LIVE_TEST=<Bedrock model id> and dev AWS credentials the "live"
cases play every language against the real model with all graders; run them
through scripts/phone_agent_eval.py (never in CI).
"""

import os

import pytest

import phone_agent_eval as ev
from test_phone_agent_calls import Phone, live  # noqa: F401 (fixture)
from test_phone_agent_support import env  # noqa: F401 (fixture)

LIVE = os.environ.get("AGENT_MODEL_LIVE_TEST")
if LIVE:   # reads the AWS credentials now, before moto patches botocore
    from test_phone_agent_bedrock_live import OutOfProcessModel
VERBOSE = os.environ.get("PHONE_AGENT_EVAL_VERBOSE") == "1"


def _id(case):
    scenario, key, _ = case
    return f"{scenario['id']}-{key}"


@pytest.mark.parametrize("case", ev.cases("sim"), ids=_id)
def test_scenario_on_the_simulator(live, monkeypatch, case):
    scenario, key, spec = case
    row = ev.run(live, scenario, key, spec, mode="sim", monkeypatch=monkeypatch, phone_cls=Phone)
    ev.write_report(row)
    assert not row["failures"], row["failures"]


def _live_cases():
    wanted = {x for x in os.environ.get("PHONE_AGENT_EVAL_LANGS", "").split(",") if x}
    only = {x for x in os.environ.get("PHONE_AGENT_EVAL_SCENARIOS", "").split(",") if x}
    return [c for c in ev.cases("live") if (not wanted or c[1] in wanted or any(p in wanted for p in c[1].split(">")))
            and (not only or c[0]["id"].split("-")[0] in only or c[0]["id"] in only)]


@pytest.mark.skipif(not LIVE, reason="set AGENT_MODEL_LIVE_TEST to a Bedrock model id")
@pytest.mark.parametrize("case", _live_cases() if LIVE else [], ids=_id)
def test_scenario_on_real_bedrock(live, monkeypatch, case):
    model = OutOfProcessModel()
    live.live._clients["model"] = model
    try:
        scenario, key, spec = case
        if VERBOSE:
            print(f"\n--- {scenario['id']} {key} ---")
        row = ev.run(live, scenario, key, spec, mode="live", monkeypatch=monkeypatch, phone_cls=Phone,
                     verbose=VERBOSE)
    finally:
        model.pool.shutdown()
    ev.write_report(row)
    assert not row["failures"], row["failures"]


def test_the_graders_catch_a_booking_before_the_yes_and_a_wrong_language(live, monkeypatch):
    scenario = next(s for s in ev.scenarios() if s["id"] == "01-book")
    spec = {**scenario["sim"]["sv"], "confirm": 5, "languages": ["sv", "en", "sv"]}
    row = ev.run(live, scenario, "sv", spec, mode="sim", monkeypatch=monkeypatch, phone_cls=Phone)
    assert row["passed"]["safety"] is False and any(f.startswith("safety:write_before_yes") for f in row["failures"])
    assert row["passed"]["language"] is False and row["passed"]["outcome"] is True


def test_style_and_register_graders():
    assert ev._style(["Klockan 19:00 är ledigt!"]) == [("style", "digits@turn0"), ("style", "exclamation@turn0")]
    assert ev._style(["Då läser jag upp: fyra personer, klockan sju."]) == []
    assert ev._style(["Vilken tid passar? Och hur många?"]) == [("style", "questions@turn0")]
    assert ev._register(["Möchtest du um sieben kommen?"], ["de"]) == [("register", "informal_de@turn0")]
    assert ev._register(["Möchten Sie um sieben kommen?"], ["de"]) == []
    assert ev._register(["Toppen, då bokar jag."], ["sv"]) == [("register", "slang@turn0")]
