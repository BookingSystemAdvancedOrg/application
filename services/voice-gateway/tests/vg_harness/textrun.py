"""A scenario as plain text, no audio: the quickest check of the dialog logic. Owner: builder 'harness'.

Runs every step's text through the real phone-agent-call code (app.start_call, session.call_turn with the real tools and
guards) in the fake world, the way tests/test_phone_agent_calls.py does, without the streaming gateway. It shows what the
agent answers and whether a booking is made; the language switch is the model's tag here (in the gateway the listener
decides it first). `python -m voice_gateway.replay --text <scenario>` prints it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vg_harness.scenarios import Scenario, step_text
from vg_harness.world import DIALED, fake_world


@dataclass
class TextTurn:
    caller: str
    reply: str
    actions: list = field(default_factory=list)


@dataclass
class TextResult:
    scenario: str
    turns: list[TextTurn] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    reservations: list[dict] = field(default_factory=list)
    callbacks: list[dict] = field(default_factory=list)
    sms: list[dict] = field(default_factory=list)
    language: str = "sv"


def glosses_of(scenario: Scenario) -> tuple[dict[str, str], dict[str, str]]:
    """(text -> what the fake model reads, text -> language) for every piece and every joined step."""
    glosses, languages = {}, {}
    for step in scenario.steps:
        whole = step_text(step)
        languages[whole] = step.language
        gloss = " ".join(p.gloss or p.text for p in step.pieces)
        if gloss != whole:
            glosses[whole] = gloss
        for piece in step.pieces:
            languages[piece.text] = step.language
            if piece.gloss:
                glosses[piece.text] = piece.gloss
    return glosses, languages


def run_text(scenario: Scenario) -> TextResult:
    import json

    import test_phone_agent_support as sup
    glosses, languages = glosses_of(scenario)
    with fake_world(scenario.tenant, glosses=glosses, languages=languages) as world:
        row = world.live.start_call(call_ref="connect-gw-text-" + scenario.key, caller=scenario.caller, to=DIALED,
                                    at=sup.NOW)
        out = TextResult(scenario.key)
        for step in scenario.steps:
            text = step_text(step)
            row = world.live.updated_or(row)
            reply, actions = world.live.run_call_turn(row, text, sup.NOW)
            out.turns.append(TextTurn(text, reply, actions))
        row = world.live.updated_or(row)
        out.language = row.get("language") or "sv"
        out.tool_calls = list(world.invoke.log)
        out.reservations = list(world.invoke.reservations.values())
        out.callbacks = world.callbacks()
        out.sms = [json.loads(c.get("Payload", "{}")) if isinstance(c, (str, bytes)) else c for c in world.lam.calls]
        return out
