"""AgentBrain: one call's agent, the orchestrator's only door into
shared/features/phone_agent. Owner: builder 'brain'.

Per call: one StreamingAgentModel (its own sink), the call row in the
PhoneAgent table exactly as today (calls.start via app.start_call, the
same agentMessages, toolState, turns, disclosure evidence, outcome,
metering at the end via app.finish). Blocking work (DynamoDB, Lambda
tool invokes, Bedrock) runs in worker threads (asyncio.to_thread); the
event loop never blocks.

turn(): the body of app.run_call_turn with the per-call model:
  location pending -> session.choose_location (a reply means: speak it,
  done); max call length -> copy.MAX_LENGTH + end_call; else
  session.call_turn(call, text, model=<this call's StreamingAgentModel>,
  invoke=app.invoker(), voice=GatewayVoice, tenant_row, location, loc,
  config, at, add_callback=app.add_callback_for(...), send_sms=app.sms_sender(),
  asr_locale=<Transcribe code of the stream>), then the transfer-answered
  finish. While it runs, AgentSentence / ToolStarted / ToolFinished
  stream out; at the end the reconciliation (streaming.py) and one
  AgentTurnDone with the actions, the outcome and the context the next
  caller turn needs (awaiting_confirmation: a proposal in toolState
  without a booked outcome; asked_question: the last sentence ends in "?"
  and is a yes/no, a number or an open question by simple rules).

Language: the call row's `language` is the authority. set_language()
applies a switch the listener decided acoustically BEFORE the turn
(calls.set_status language + languageSwitches + 1, calls.repeat_disclosure
with new_language=True) and returns the short disclosure sentence to say
first (None when the model must say it: session asks it via disclose_now).
A pending read-back heard in the old language no longer counts: the same
not-heard rule as below is applied to toolState (MULTILINGUAL.md gap 9).
A switch session.call_turn makes on its own shows as AgentTurnDone.language.

Interruptions: mark_not_heard(heard) after a barge-in on an assistant
reply (or a reply dropped because the caller kept talking): the same
rule as tools.Executor.not_heard (read-backs of this turn count as not
heard, so create_reservation(confirmed=true) is refused until a new
read-back and a later clear yes), applied to the stored toolState by
calling Executor.not_heard on a minimal stand-in (state, turn); `resume`
is popped again (a barge-in is not a deadline cut); the last assistant
message becomes what the caller heard plus " ..." (agent.replace_last_reply).
greeting_interrupted(): calls.repeat_disclosure (the short sentence, said
before the next reply), as the voice webhook does today.

Non-streaming fallback (BrainConfig.stream_sentences False, or the
streaming model failing to construct): the same turn(), sentences cut
from the final reply only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Callable, Literal

from shared.agent_model import AgentModel

from voice_gateway.audio.adapter import CallInfo
from voice_gateway.clock import Clock
from voice_gateway.config import GatewayConfig
from voice_gateway.brain.streaming import StreamSink
from voice_gateway.events import AgentSentence, AgentTurnDone, EventBus, ToolFinished, ToolStarted


@dataclass(frozen=True)
class CallStart:
    call_id: str | None              # None: not a Booqy number (hang up, nothing said)
    tenant_id: str | None
    language: str                    # "sv" at start
    greeting: str | None             # the full greeting with the disclosure, Booqy wording
    refused: bool = False            # the greeting is the refusal sentence; hang up after it
    location_pending: bool = False   # shared number: the greeting asks which location
    names: tuple[str, ...] = ()      # restaurant and location names (LID proper noun strip, turn vocab)
    allowed: tuple[str, ...] = ()    # languages the call may switch to (session.allowed_languages)


class AgentBrain:
    def __init__(self, cfg: GatewayConfig, bus: EventBus, clock: Clock, *, app=None,
                 model_factory: Callable[[StreamSink], AgentModel] | None = None) -> None:
        """`app`: the loaded phone-agent-call module (lambda_app.load()), injectable.
        `model_factory(sink)`: the call's AgentModel; None builds a
        StreamingAgentModel on PHONE_AGENT_MODEL_ID. A model that is not a
        StreamingAgentModel (shared.agent_model's simulator, a scripted test
        model) runs the non-streaming path: sentences from the final reply."""
        self.cfg, self.bus, self.clock = cfg, bus, clock

    async def start(self, info: CallInfo) -> CallStart:
        raise NotImplementedError

    def turn(self, text: str, *, language: str, asr_locale: str | None = None
             ) -> AsyncIterator[AgentSentence | ToolStarted | ToolFinished | AgentTurnDone]:
        """Runs one caller turn; the iterator ends after AgentTurnDone. Every
        item is also published on the bus."""
        raise NotImplementedError

    async def set_language(self, code: str, at_ms: int, reason: str) -> str | None:
        raise NotImplementedError

    async def mark_not_heard(self, heard: list[str]) -> None:
        raise NotImplementedError

    async def greeting_interrupted(self) -> str | None:
        raise NotImplementedError

    async def greeting_played(self) -> None:
        """The disclosure evidence (calls.disclosed with the greeting text),
        once the greeting's audio finished on the line."""
        raise NotImplementedError

    async def transfer_result(self, answered: bool) -> str | None:
        """Records the transfer; returns copy.TRANSFER_UNANSWERED when not answered."""
        raise NotImplementedError

    async def finish(self, reason: Literal["hangup", "agent_end", "transferred", "max_length", "error",
                                          "refused", "still_there"]) -> None:
        raise NotImplementedError

    def fixed(self, key: Literal["still_there", "error", "transferring", "max_length"], language: str) -> str:
        """Booqy-authored copy in `language` (copy.STILL_THERE, copy.ERROR, ...)."""
        raise NotImplementedError

    @property
    def language(self) -> str:
        raise NotImplementedError
