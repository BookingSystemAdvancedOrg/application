"""The real voice provider client (ElevenLabs Agents Platform over SIP,
ADR-L), a skeleton until feature L (phone-agent) builds it: every method is
the port's and raises NotImplementedError, so VOICE_CLIENT=real fails
loudly instead of pretending to work."""

from shared.voice_provider import VoiceProvider


class RealVoiceProvider(VoiceProvider):
    pass
