"""End of speech to first audio with the live costs on the virtual clock (vg_harness.runner.SIM_LATENCY: Bedrock
rounds, tool Lambdas, Polly, the Transcribe final): the opener comes within 250 ms of the turn end, "Jag kollar."
only before a lookup that runs, the booking turn takes one Bedrock round less with the lookup first, nothing plays
over the caller."""

import asyncio
import dataclasses
import importlib.util
import json

import pytest

from voice_gateway.events import AgentTurnDone, PlaybackStarted, TurnEnd


def _run(key, cfg=None):
    pytest.importorskip("moto")
    from vg_harness import runner, scenarios
    wire = "mulaw" if importlib.util.find_spec("numpy") else "pcm"
    return asyncio.run(runner.run(scenarios.get(key), "fake", wire=wire, cfg=cfg, sim_latency=dict(runner.SIM_LATENCY)))


def test_openers_come_at_the_turn_end_and_check_only_before_a_real_lookup():
    from vg_harness import analysis as an
    r = _run("lang-A")
    ends = [e for e in r.events if isinstance(e, TurnEnd)]
    openers = [e for e in r.events if isinstance(e, PlaybackStarted) and e.kind == "opener"]
    assert openers
    for o in openers:
        end = max((e for e in ends if e.at_ms <= o.at_ms), key=lambda e: e.at_ms)
        assert o.at_ms - end.at_ms <= 250
        if ":kollar" in o.item_id:                         # "Jag kollar.": the turn starts with the lookup
            done = next(e for e in r.events if isinstance(e, AgentTurnDone) and e.at_ms >= end.at_ms)
            assert done.tools[0] == "get_availability"
    assert an.backchannel_overlaps(r.events) == 0
    assert an.percentile(an.eos_to_first_audio(r.events), 0.5) <= 1500


def test_the_lookup_first_saves_a_bedrock_round_on_the_booking_turn():
    from vg_harness import analysis as an
    from vg_harness.runner import fake_config
    from voice_gateway.config import GatewayConfig
    off = GatewayConfig.from_json(json.dumps({"brain": {"one_round": False, "prefetch": False},
                                              "speak": {"opener": False}}))
    before, after = _run("lang-A", fake_config(off)), _run("lang-A")
    first = lambda r: an.eos_to_reply_audio(r.events)[0]
    assert first(before) - first(after) >= 1400            # one model round (1500 ms) less to the reply
