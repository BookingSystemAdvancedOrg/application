"""CallOrchestrator: the call state machine. Owner: builder 'brain'.

States (events.CallState) and transitions:

  CONNECTING --accept, brain.start--> GREETING (refused: say the refusal,
      ENDING; not a Booqy number: hang up at once)
  GREETING  --greeting played--> LISTENING (brain.greeting_played)
            --BargeIn before the disclosure sentence ended--> flush,
              brain.greeting_interrupted, its short sentence is said
              before the next reply (art. 50), LISTENING
  LISTENING --TurnEnd--> THINKING (language decision applied first:
              SWITCH -> brain.set_language + speaker/listener language;
              ASK/REPEAT/UNSUPPORTED/CALLBACK -> handled by the brain turn
              with the shared copy, see DESIGN.md 6.4)
            --no speech for still_there_after_ms--> say STILL_THERE (twice,
              then the goodbye, ENDING)
  THINKING  --first AgentSentence queued and playing--> SPEAKING
            --caller speaks again within continuation_grace_ms of the
              TurnEnd and before any audio--> the reply is NOT played;
              when the turn finishes, brain.mark_not_heard([]) and the new
              utterance is the next turn (nothing is lost, nothing is said
              twice)
  SPEAKING  --AgentTurnDone and the queue drained--> LISTENING, with
              AssistantContext published (turn detector and backchannels)
            --BargeIn--> speaker.flush (target under 300 ms), unplayed or
              partly played sentences -> brain.mark_not_heard(heard),
              LISTENING (the caller's speech continues as the next turn)
            --action end_call--> ENDING after the goodbye drained
            --action transfer_to_number--> TRANSFERRING after the
              TRANSFERRING sentence drained
  TRANSFERRING --adapter.transfer answered--> ENDED (brain.finish transferred)
               --not answered--> say TRANSFER_UNANSWERED, LISTENING
  ENDING    --drained--> adapter.hangup, brain.finish, ENDED
  any       --caller hung up (adapter.closed)--> brain.finish("hangup"), ENDED
  any       --SttUnavailable or brain error--> fixed error sentence, ENDING

Tasks per call (asyncio.TaskGroup): Listener.run, Speaker.run, the
orchestrator loop (bus subscription), MetricsRecorder (bus.listen). Every
task ends when the call ends; one failing task ends the call with the
error sentence (never silence) and is logged with the call tag only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from voice_gateway.audio.adapter import TelephonyAdapter
from voice_gateway.backchannel.clips import ClipCache
from voice_gateway.clock import Clock
from voice_gateway.config import GatewayConfig
from voice_gateway.events import CallState, EventBus
from voice_gateway.listen.transcribe import SttFactory
from voice_gateway.metrics import CallMetrics
from voice_gateway.speak.tts import Tts


@dataclass
class Components:
    """Everything a call needs that tests or the harness may replace."""
    stt: SttFactory
    tts: Tts
    clips: ClipCache
    clock: Clock
    brain_factory: object                  # (cfg, bus, clock) -> AgentBrain
    rng_seed: int | None = None
    record_events: bool = False            # bus.history on (harness)


@dataclass
class CallSummary:
    call_id: str | None
    end_reason: str
    metrics: CallMetrics
    states: list[tuple[int, CallState]] = field(default_factory=list)
    events: list | None = None             # bus.history when record_events


class CallOrchestrator:
    def __init__(self, cfg: GatewayConfig, adapter: TelephonyAdapter, components: Components) -> None:
        self.cfg, self.adapter, self.components = cfg, adapter, components
        self.bus = EventBus()
        self.state = CallState.CONNECTING

    async def run(self) -> CallSummary:
        raise NotImplementedError
