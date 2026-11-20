"""ScriptedBrain: the AgentBrain interface with canned replies and no AWS,
DynamoDB or agent loop. Owner: builder 'brain'.

For `server.py --fake` and the orchestrator's unit tests only: it knows
nothing about bookings (the real guards live in the shared agent, which
the real AgentBrain runs). Each caller turn takes the next Reply of the
script (the last one repeats); every call it receives is recorded in
`calls` so a test can assert what the orchestrator asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator

from shared.features.phone_agent import copy

from voice_gateway.audio.adapter import CallInfo
from voice_gateway.brain.agent_brain import CallStart, fixed_text
from voice_gateway.brain.streaming import NO_FILLER_TOOLS, split_sentences
from voice_gateway.clock import Clock
from voice_gateway.config import GatewayConfig
from voice_gateway.events import AgentSentence, AgentTurnDone, EventBus, ProviderAction, ToolFinished, ToolStarted

GREETING = "Hej och välkommen till Testkrogen. Du pratar med vår digitala assistent. Vad kan jag hjälpa dig med?"


@dataclass
class Reply:
    """One scripted reply. `tool` (a name) is announced `tool_after_ms`
    into the turn and runs `tool_ms`; sentences come `gap_ms` apart after
    `first_ms`."""
    text: str = "Absolut, det ordnar jag gärna."
    language: str = "sv"
    first_ms: int = 600
    gap_ms: int = 150
    tool: str | None = None
    tool_after_ms: int = 300
    tool_ms: int = 900
    actions: tuple[ProviderAction, ...] = ()
    outcome: str | None = None
    awaiting_confirmation: bool = False
    asked_question: str | None = None
    call_language: str | None = None      # the call language after the turn (a switch on the model tag)
    error: bool = False


@dataclass
class ScriptedBrain:
    cfg: GatewayConfig
    bus: EventBus
    clock: Clock
    script: list[Reply] = field(default_factory=lambda: [Reply()])
    greeting: str = GREETING
    refused: bool = False
    booqy_number: bool = True
    calls: list[tuple] = field(default_factory=list)
    _language: str = "sv"
    _turn: int = 0

    async def start(self, info: CallInfo) -> CallStart:
        self.calls.append(("start", info.call_ref))
        if not self.booqy_number:
            return CallStart(None, None, "sv", None)
        if self.refused:
            text = copy.REFUSED["sv"]
            return CallStart("call-1", "tenant-1", "sv", text, refused=True,
                             greeting_sentences=tuple(split_sentences(text)))
        sentences = tuple(split_sentences(self.greeting))
        until = next((i for i, s in enumerate(sentences) if "digitala assistent" in s), len(sentences) - 1)
        return CallStart("call-1", "tenant-1", "sv", self.greeting, names=("Testkrogen",), allowed=("sv", "en", "de"),
                         greeting_sentences=sentences, disclosure_until=until)

    async def turn(self, text: str, *, language: str, asr_locale: str | None = None
                   ) -> AsyncIterator[AgentSentence | ToolStarted | ToolFinished | AgentTurnDone]:
        self._turn += 1
        number = self._turn
        reply = self.script[min(number - 1, len(self.script) - 1)]
        self.calls.append(("turn", number, text, language))
        started = self.clock.now_ms()
        tools = ()
        if reply.tool:
            await self.clock.sleep_ms(reply.tool_after_ms)
            tools = (reply.tool,)
            if reply.tool not in NO_FILLER_TOOLS:
                event = ToolStarted(self.clock.now_ms(), number, reply.tool)
                self.bus.publish(event)
                yield event
            await self.clock.sleep_ms(reply.tool_ms)
            if reply.tool not in NO_FILLER_TOOLS:
                event = ToolFinished(self.clock.now_ms(), number, reply.tool, True, reply.tool_ms)
                self.bus.publish(event)
                yield event
        await self.clock.sleep_until(started + reply.first_ms)
        for index, sentence in enumerate(split_sentences(reply.text)):
            if index:
                await self.clock.sleep_ms(reply.gap_ms)
            event = AgentSentence(self.clock.now_ms(), number, index, sentence, reply.language,
                                  "fixed" if reply.error else "assistant")
            self.bus.publish(event)
            yield event
        if reply.call_language:
            self._language = reply.call_language
        done = AgentTurnDone(self.clock.now_ms(), number, self._language, 2 if reply.tool else 1, False, reply.error,
                             reply.actions, reply.outcome, reply.awaiting_confirmation, reply.asked_question,
                             self.clock.now_ms() - started, tools)
        self.bus.publish(done)
        yield done

    async def set_language(self, code: str, at_ms: int, reason: str) -> str | None:
        self.calls.append(("set_language", code, reason))
        if code == self._language:
            return None
        self._language = code
        return copy.disclosure_short(code)

    async def mark_not_heard(self, heard: list[str]) -> None:
        self.calls.append(("mark_not_heard", list(heard)))

    async def greeting_interrupted(self) -> str | None:
        self.calls.append(("greeting_interrupted",))
        return copy.text(copy.DISCLOSURE_SHORT, self._language)

    async def greeting_played(self, spoken: str | None = None) -> None:
        self.calls.append(("greeting_played", spoken))

    async def transfer_result(self, answered: bool) -> str | None:
        self.calls.append(("transfer_result", answered))
        return None if answered else fixed_text("transfer_unanswered", self._language)[0]

    async def finish(self, reason: str) -> None:
        self.calls.append(("finish", reason))

    def fixed(self, key: str, language: str) -> str:
        return fixed_text(key, language)[0]

    @property
    def language(self) -> str:
        return self._language

    def names(self, kind: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == kind]


def factory(script: list[Reply] | None = None, **kwargs):
    """Components.brain_factory for a scripted call."""
    def make(cfg: GatewayConfig, bus: EventBus, clock: Clock) -> ScriptedBrain:
        brain = ScriptedBrain(cfg, bus, clock, **kwargs)
        if script is not None:
            brain.script = list(script)
        return brain
    return make
