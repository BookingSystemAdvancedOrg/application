"""GatewayVoice: the shared VoiceProvider port as the gateway sees it.
Owner: builder 'brain'.

inline_speech = True   the gateway speaks every text itself (start_call
                       does not call say());
deferred_tools = True  transfer_to_staff becomes the provider action
                       transfer_to_number (tools.Executor.transfer_to_staff),
                       which the orchestrator runs on the adapter, then
                       reports with AgentBrain.transfer_result().

transfer() and end() behave as the deferred ElevenLabs provider does
(shared/voice_provider_real.py): Executor.transfer_to_staff still calls
voice.transfer after queueing the action, and a raise there would break
the agent turn (Executor.run catches only its own errors). transfer()
answers "answered" (the transfer is pending, the line does it after this
reply); AgentBrain.transfer_result() corrects the call row and the tool
state when nobody answers. end() is a no-op: end_call is the provider
action the orchestrator executes. Every other method raises
VoiceProviderError("not_supported").
"""

from __future__ import annotations

from shared.voice_provider import TransferResult, VoiceProvider, VoiceProviderError


class GatewayVoice(VoiceProvider):
    inline_speech = True
    deferred_tools = True

    def say(self, call_ref, text):
        raise VoiceProviderError("not_supported")      # callers check inline_speech first

    def transfer(self, call_ref, to_e164, timeout_s):
        return TransferResult(True)                     # pending: transfer_to_number in this turn's actions

    def end(self, call_ref, reason):
        return None                                     # end_call in this turn's actions
