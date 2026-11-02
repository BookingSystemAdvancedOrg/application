"""Voice provider port (CONTRACTS.md 13, feature L, ADR-L decision 3): the
provider-side agent per location, its SIP target, speech injected outside
the LLM turn, attended transfer, hangup, recordings and signed webhooks.
`client()` picks the simulator or shared/voice_provider_real.py
(ElevenLabs, owned by L) from VOICE_CLIENT."""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from shared import external_client

EVENT_KINDS = ("call_started", "transcript", "language_detected", "interrupted", "transfer_result", "call_ended")
SIM_SIGNATURE_HEADER = "x-sim-signature"
SIM_RECORDING = Path(__file__).with_name("voice_provider_sim_recording.wav")


class VoiceProviderError(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class SipTarget:
    uri: str
    auth: dict | None = None


@dataclass(frozen=True)
class TransferResult:
    answered: bool


@dataclass(frozen=True)
class VoiceEvent:
    kind: str                      # one of EVENT_KINDS
    call_ref: str | None = None
    data: dict = field(default_factory=dict)


class VoiceProvider:
    def create_agent(self, config): raise NotImplementedError
    def update_agent(self, provider_agent_id, config): raise NotImplementedError
    def delete_agent(self, provider_agent_id): raise NotImplementedError
    def sip_target(self, provider_agent_id): raise NotImplementedError
    def say(self, call_ref, text): raise NotImplementedError
    def transfer(self, call_ref, to_e164, timeout_s): raise NotImplementedError
    def end(self, call_ref, reason): raise NotImplementedError
    def recording_url(self, call_ref): raise NotImplementedError
    def parse_webhook(self, headers, body): raise NotImplementedError


def _agent_name(config) -> str:
    name = config.get("name") if isinstance(config, dict) else getattr(config, "name", None)
    if not name:
        raise VoiceProviderError("invalid_config")
    return str(name)


def _header(headers, name):
    return next((v for k, v in (headers or {}).items() if str(k).lower() == name), None)


class SimulatedVoiceProvider(external_client.Simulated, VoiceProvider):
    """Agents `sim-agent-<sha1(name)[:10]>`, idempotent by name (kept in
    `agents`); say/transfer/end recorded in `calls`; `transfer` answers
    `transfer_answered`; `recording_url` is a file:// fixture only when
    `recording=True`. Webhooks: the simulator's own JSON
    `{"events": [{"kind", "call_ref", ...data}]}` with `X-Sim-Signature: ok`;
    anything else raises `unauthorized`."""

    error = VoiceProviderError

    def __init__(self, fail=None, transfer_answered=True, recording=False):
        super().__init__(fail)
        self.transfer_answered = transfer_answered
        self.recording = recording
        self.agents = {}

    def create_agent(self, config):
        name = _agent_name(config)
        self._call("create_agent", name)
        agent_id = f"sim-agent-{hashlib.sha1(name.encode()).hexdigest()[:10]}"
        self.agents.setdefault(agent_id, config)
        return agent_id

    def update_agent(self, provider_agent_id, config):
        self._call("update_agent", provider_agent_id)
        if provider_agent_id not in self.agents:
            raise VoiceProviderError("not_found")
        self.agents[provider_agent_id] = config

    def delete_agent(self, provider_agent_id):
        self._call("delete_agent", provider_agent_id)
        self.agents.pop(provider_agent_id, None)

    def sip_target(self, provider_agent_id):
        self._call("sip_target", provider_agent_id)
        if provider_agent_id not in self.agents:
            raise VoiceProviderError("not_found")
        return SipTarget(f"sip:{provider_agent_id}@sim.invalid")

    def say(self, call_ref, text):
        self._call("say", call_ref, text)

    def transfer(self, call_ref, to_e164, timeout_s):
        self._call("transfer", call_ref, to_e164, timeout_s)
        return TransferResult(bool(self.transfer_answered))

    def end(self, call_ref, reason):
        self._call("end", call_ref, reason)

    def recording_url(self, call_ref):
        self._call("recording_url", call_ref)
        return SIM_RECORDING.as_uri() if self.recording else None

    def parse_webhook(self, headers, body):
        self._call("parse_webhook")
        if _header(headers, SIM_SIGNATURE_HEADER) != "ok":
            raise VoiceProviderError("unauthorized")
        try:
            raw = json.loads(body if isinstance(body, (str, bytes, bytearray)) else json.dumps(body))
            events = []
            for item in raw["events"]:
                data = {k: v for k, v in item.items() if k not in ("kind", "call_ref")}
                if item["kind"] not in EVENT_KINDS:
                    raise ValueError(item["kind"])
                events.append(VoiceEvent(item["kind"], item.get("call_ref"), data))
        except (TypeError, ValueError, KeyError, AttributeError):
            raise VoiceProviderError("unauthorized") from None
        return events


def client(**kwargs) -> VoiceProvider:
    if external_client.mode("voice") == external_client.REAL:
        from shared.voice_provider_real import RealVoiceProvider
        return RealVoiceProvider()
    return SimulatedVoiceProvider(**kwargs)
