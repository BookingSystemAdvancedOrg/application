"""GatewayVoice: the shared VoiceProvider port as the gateway sees it.
Owner: builder 'brain'.

inline_speech = True   the gateway speaks every text itself (start_call
                       does not call say());
deferred_tools = True  transfer_to_staff becomes the provider action
                       transfer_to_number (tools.Executor.transfer_to_staff),
                       which the orchestrator runs on the adapter, then
                       reports with AgentBrain.transfer_result().
Every other method raises VoiceProviderError("not_supported").
"""

from __future__ import annotations

from shared.voice_provider import VoiceProvider, VoiceProviderError


class GatewayVoice(VoiceProvider):
    inline_speech = True
    deferred_tools = True

    def say(self, call_ref, text):
        raise VoiceProviderError("not_supported")

    def transfer(self, call_ref, to_e164, timeout_s):
        raise VoiceProviderError("not_supported")

    def end(self, call_ref, reason):
        raise VoiceProviderError("not_supported")
