"""The real phone agent clients with their HTTP and AWS calls faked:
ElevenLabs (shared/voice_provider_real.py), 46elks (shared/telephony_real.py)
and Bedrock Converse (shared/agent_model_real.py)."""

import hashlib
import hmac
import json
import urllib.parse

import pytest

from shared.agent_model import AgentModelError, ToolUse
from shared.agent_model_real import RealAgentModel, read_stream, to_bedrock_messages, to_bedrock_tools
from shared.features.phone_agent.tools import TOOLS
from shared.telephony import TelephonyError
from shared.telephony_real import RealTelephony, forward_action
from shared.voice_provider import SipTarget, VoiceProviderError
from shared.voice_provider_real import RealVoiceProvider, call_ids_from_prompt

SECRET = {"apiKey": "xi-test", "webhookSecret": "whsec", "initiationToken": "init-token-value",
          "callUrl": "https://abc.lambda-url.eu-north-1.on.aws/"}
CONFIG = {"name": "dev-phone-agent-loc-1", "greetingSv": "Hej, du pratar med Sara, en AI-assistent för Bistro A.",
          "greetingEn": "Hello, you are speaking with Sara, an AI assistant for Bistro A.",
          "languages": ["sv", "en", "de"], "tone": "friendly", "transferTo": "+46812345678",
          "maxDurationSeconds": 480}


class Http:
    """Answers by (method, path prefix) in order; records every request."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, headers, body))
        for i, (m, prefix, status, answer) in enumerate(self.answers):
            if m == method and urllib.parse.urlparse(url).path.startswith(prefix):
                self.answers.pop(i)
                return status, answer
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def voice_env(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_VOICE_ID", "voice-sv-1")
    monkeypatch.delenv("ELEVENLABS_API_BASE", raising=False)


def provider(http, **kw):
    return RealVoiceProvider(secret=dict(SECRET), llm_key="llm-key-value", transport=http, **kw)


# --- ElevenLabs ----------------------------------------------------------------------------------

def test_create_agent_builds_a_swedish_first_multilingual_agent_on_booqys_llm(voice_env):
    http = Http([("GET", "/v1/convai/secrets", 200, {"secrets": []}),
                 ("POST", "/v1/convai/secrets", 200, {"secret_id": "sec-llm"}),
                 ("GET", "/v1/convai/secrets", 200, {"secrets": []}),
                 ("POST", "/v1/convai/secrets", 200, {"secret_id": "sec-tok"}),
                 ("GET", "/v1/convai/agents", 200, {"agents": []}),
                 ("POST", "/v1/convai/agents/create", 200, {"agent_id": "ag-1"})])
    assert provider(http).create_agent(CONFIG) == "ag-1"
    _, url, headers, body = http.requests[-1]
    assert url == "https://api.elevenlabs.io/v1/convai/agents/create" and headers == {"xi-api-key": "xi-test"}
    agent = body["conversation_config"]["agent"]
    assert agent["language"] == "sv" and agent["first_message"] == CONFIG["greetingSv"]
    assert agent["disable_first_message_interruptions"] is True
    llm = agent["prompt"]
    assert llm["llm"] == "custom-llm" and llm["prompt"] == "booqy-call tenant={{tenantId}} call={{callId}}"
    assert llm["custom_llm"] == {"url": SECRET["callUrl"] + "v1", "model_id": "booqy-phone-agent",
                                 "api_key": {"secret_id": "sec-llm"}, "api_type": "chat_completions"}
    assert set(llm["built_in_tools"]) == {"end_call", "language_detection", "transfer_to_number"}
    transfer = llm["built_in_tools"]["transfer_to_number"]["params"]["transfers"][0]
    assert transfer["transfer_destination"]["phone_number"] == "+46812345678"
    assert body["conversation_config"]["tts"] == {"voice_id": "voice-sv-1", "model_id": "eleven_flash_v2_5"}
    presets = body["conversation_config"]["language_presets"]
    assert len(presets) == 31 and {"en", "de", "tr", "ar", "fil"} <= set(presets) and "sv" not in presets
    assert body["conversation_config"]["conversation"]["max_duration_seconds"] == 480
    settings = body["platform_settings"]
    assert settings["overrides"]["enable_conversation_initiation_client_data_from_webhook"] is True
    hook = settings["workspace_overrides"]["conversation_initiation_client_data_webhook"]
    assert hook == {"url": SECRET["callUrl"] + "public/phone-agent/voice/webhook",
                    "request_headers": {"x-booqy-webhook-token": {"secret_id": "sec-tok"}}}
    assert settings["privacy"]["record_voice"] is False
    # keys never travel in plain text inside the agent
    assert "llm-key-value" not in json.dumps(body) and "init-token-value" not in json.dumps(body)


def test_create_agent_is_idempotent_by_name(voice_env):
    listed = {"secrets": [{"name": "dev-booqy-llm-endpoint-key", "secret_id": "s1"},
                          {"name": "dev-booqy-webhook-token", "secret_id": "s2"}]}
    http = Http([("GET", "/v1/convai/secrets", 200, listed), ("GET", "/v1/convai/secrets", 200, listed),
                 ("GET", "/v1/convai/agents", 200, {"agents": [{"name": "dev-phone-agent-loc-1",
                                                                "agent_id": "ag-old"}]}),
                 ("PATCH", "/v1/convai/agents/ag-old", 200, {})])
    assert provider(http).create_agent(CONFIG) == "ag-old"
    assert [r[0] for r in http.requests] == ["GET", "GET", "GET", "PATCH"]


def test_agent_needs_a_voice_and_the_call_url(monkeypatch):
    monkeypatch.delenv("PHONE_AGENT_VOICE_ID", raising=False)
    with pytest.raises(VoiceProviderError) as raised:
        provider(Http([])).create_agent(CONFIG)
    assert raised.value.code == "not_configured"
    monkeypatch.setenv("PHONE_AGENT_VOICE_ID", "v")
    bad = RealVoiceProvider(secret={**SECRET, "callUrl": "http://insecure/"}, llm_key="k", transport=Http([]))
    with pytest.raises(VoiceProviderError):
        bad.create_agent(CONFIG)


def test_sip_target_imports_the_number_once_tcp_and_46elks_only(voice_env):
    http = Http([("GET", "/v1/convai/phone-numbers", 200, []),
                 ("POST", "/v1/convai/phone-numbers", 200, {"phone_number_id": "pn-1"})])
    target = provider(http).sip_target("ag-1", "+46101234567")
    assert target == SipTarget("sip:+46101234567@sip.rtc.elevenlabs.io:5060;transport=tcp")
    body = http.requests[-1][3]
    assert body["provider"] == "sip_trunk" and body["agent_id"] == "ag-1"
    assert body["inbound_trunk_config"]["allowed_addresses"] == ["176.10.154.205", "85.24.146.136"]
    again = Http([("GET", "/v1/convai/phone-numbers", 200, [{"phone_number": "+46101234567",
                                                              "phone_number_id": "pn-1"}]),
                  ("PATCH", "/v1/convai/phone-numbers/pn-1", 200, {})])
    provider(again).sip_target("ag-2", "+46101234567")
    assert again.requests[-1][3] == {"agent_id": "ag-2"}


def test_provider_errors_map_to_port_errors(voice_env):
    for status, code, retry in ((500, "unavailable", True), (429, "unavailable", True), (404, "not_found", False),
                                (422, "rejected", False)):
        with pytest.raises(VoiceProviderError) as raised:
            provider(Http([("DELETE", "/v1/convai/agents/x", status, {})]))._api("DELETE", "/v1/convai/agents/x")
        assert (raised.value.code, raised.value.retryable) == (code, retry)
    provider(Http([("DELETE", "/v1/convai/agents/gone", 404, {})])).delete_agent("gone")   # already gone: fine


def _signed(payload, secret="whsec", stamp=1_800_000_000):
    raw = json.dumps(payload)
    sig = hmac.new(secret.encode(), f"{stamp}.{raw}".encode(), hashlib.sha256).hexdigest()
    return raw, {"ElevenLabs-Signature": f"t={stamp},v0={sig}"}


def test_post_call_webhook_is_verified_and_becomes_disclosure_evidence_and_the_end():
    voice = provider(Http([]), now=lambda: 1_800_000_100)
    payload = {"type": "post_call_transcription", "data": {
        "conversation_id": "conv-1", "metadata": {"start_time_unix_secs": 1_800_000_000, "call_duration_secs": 95},
        "transcript": [{"role": "agent", "message": "Hej, du pratar med Sara.", "time_in_call_secs": 0},
                       {"role": "user", "message": "Hej", "time_in_call_secs": 3}],
        "conversation_initiation_client_data": {"dynamic_variables": {"tenantId": "t1", "callId": "c1",
                                                                      "system__caller_id": "+4670"}}}}
    raw, headers = _signed(payload)
    events = voice.parse_webhook(headers, raw)
    assert [e.kind for e in events] == ["transcript", "call_ended"]
    assert events[0].data == {"tenantId": "t1", "callId": "c1", "role": "agent", "text": "Hej, du pratar med Sara.",
                              "final": True, "at": "2027-01-15T08:00:00Z"}
    assert events[1].data["usage"] == {"seconds": 95} and events[1].data["at"] == "2027-01-15T08:01:35Z"
    assert "+4670" not in json.dumps([e.data for e in events])        # no caller number from the provider
    for bad in (_signed(payload, secret="other"), _signed(payload, stamp=1_799_990_000),
                (raw.replace("95", "96"), headers), (raw, {"ElevenLabs-Signature": "garbage"})):
        with pytest.raises(VoiceProviderError):
            voice.parse_webhook(bad[1], bad[0])
    raw, headers = _signed({"type": "post_call_audio", "data": {}})
    assert voice.parse_webhook(headers, raw) == []


def test_initiation_webhook_needs_the_token_and_answers_initiation_data():
    voice = provider(Http([]))
    body = json.dumps({"caller_id": "+46701112233", "called_number": "+46101234567", "agent_id": "ag",
                       "call_sid": "sip-1", "conversation_id": "conv-9"})
    [event] = voice.parse_webhook({"X-Booqy-Webhook-Token": "init-token-value"}, body)
    assert (event.kind, event.call_ref, event.data) == ("call_started", "conv-9", {"from": "+46701112233",
                                                                                   "to": "+46101234567"})
    for headers in ({}, {"x-booqy-webhook-token": "nope"}):
        with pytest.raises(VoiceProviderError):
            voice.parse_webhook(headers, body)
    answer = voice.webhook_response({"dynamicVariables": {"tenantId": "t", "callId": "c"}, "firstMessage": "Hej",
                                     "language": "sv"})
    assert answer == {"type": "conversation_initiation_client_data", "dynamic_variables": {"tenantId": "t",
                                                                                           "callId": "c"},
                      "conversation_config_override": {"agent": {"language": "sv", "first_message": "Hej"}},
                      "custom_llm_extra_body": {"booqy": {"tenantId": "t", "callId": "c"}}}
    assert voice.webhook_response({}) == {}


def test_call_ids_from_the_prompt_elevenlabs_sends():
    assert call_ids_from_prompt("booqy-call tenant=01aa call=20261102abcdef0123456789") == (
        "01aa", "20261102abcdef0123456789")
    assert call_ids_from_prompt("booqy-call tenant={{tenantId}} call={{callId}}") == (None, None)


# --- 46elks ----------------------------------------------------------------------------------------

def elks(http):
    return RealTelephony(secret={"username": "u", "password": "p", "webhookToken": "wt"}, transport=http)


def test_46elks_number_forwarded_to_elevenlabs_with_the_restaurant_as_fallback():
    http = Http([("POST", "/a1/numbers", 200, {"id": "n1", "number": "+46766860001"}),
                 ("POST", "/a1/numbers/n1", 200, {}), ("POST", "/a1/numbers/n1", 200, {})])
    tel = elks(http)
    number = tel.provision_number("SE", "dev-phone-agent-loc-1")
    assert (number.e164, number.provider_id) == ("+46766860001", "n1")
    method, url, headers, form = http.requests[0]
    assert form == {"country": "se", "capabilities": "voice", "name": "dev-phone-agent-loc-1"}
    assert headers["Authorization"] == "Basic dTpw"
    sip = SipTarget("sip:+46766860001@sip.rtc.elevenlabs.io:5060;transport=tcp")
    tel.set_inbound("n1", sip, "+46812345678")
    assert json.loads(http.requests[1][3]["voice_start"]) == {
        "connect": sip.uri, "failed": {"connect": "+46812345678"}, "busy": {"connect": "+46812345678"}}
    tel.release_number("n1")
    assert http.requests[2][3] == {"active": "no"}
    assert forward_action("sip:x", None) == {"connect": "sip:x"}


def test_46elks_errors_and_unsupported():
    with pytest.raises(TelephonyError) as raised:
        elks(Http([("POST", "/a1/numbers", 403, {})])).provision_number("SE", "x")
    assert raised.value.code == "number_unavailable"
    with pytest.raises(TelephonyError):
        elks(Http([])).provision_number("NO", "x")
    with pytest.raises(TelephonyError):
        elks(Http([])).transfer("c", "+46812345678", 25)
    assert elks(Http([])).verify_forwarding("+46812345678").status == "unsupported"


def test_46elks_callbacks_need_its_address_and_the_token():
    tel = elks(Http([]))
    hangup = "id=c1&direction=incoming&from=%2B46701112233&to=%2B46766860001&duration=61&state=success"
    [event] = tel.parse_webhook({}, hangup, source_ip="176.10.154.199", query={"token": "wt"})
    assert (event.kind, event.call_ref, event.data["duration_s"]) == ("ended", "c1", 61)
    inbound = "callid=c2&direction=incoming&from=%2B46701112233&to=%2B46766860001&created=2026-11-13T10:00:00Z"
    [event] = tel.parse_webhook({}, inbound, source_ip="85.24.146.132", query={"token": "wt"})
    assert event.kind == "inbound" and event.data["to"] == "+46766860001"
    for ip, token in (("1.2.3.4", "wt"), ("176.10.154.199", "bad"), ("176.10.154.199", None)):
        with pytest.raises(TelephonyError):
            tel.parse_webhook({}, hangup, source_ip=ip, query={"token": token} if token else {})


# --- Bedrock -----------------------------------------------------------------------------------------

def test_messages_and_tools_map_to_bedrock_converse():
    messages = [{"role": "user", "content": "Hej"},
                {"role": "assistant", "content": [{"type": "text", "text": "Jag kollar."},
                                                  {"type": "tool_use", "id": "t1", "name": "get_menu",
                                                   "input": {"query": "vegetariskt"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                              "content": json.dumps({"summary": "Lasagne 189 kr"})}]},
                {"role": "assistant", "content": []},
                {"role": "user", "content": "Tack"}]
    out = to_bedrock_messages(messages)
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant", "user"]
    assert out[1]["content"][1] == {"toolUse": {"toolUseId": "t1", "name": "get_menu",
                                                "input": {"query": "vegetariskt"}}}
    assert out[2]["content"][0]["toolResult"]["content"] == [{"json": {"summary": "Lasagne 189 kr"}}]
    assert out[3]["content"] == [{"text": "..."}]          # Bedrock refuses empty content
    tools = to_bedrock_tools(TOOLS)["tools"]
    assert len(tools) == len(TOOLS) and tools[2]["toolSpec"]["inputSchema"]["json"]["required"] == [
        "date", "time", "partySize"]


def test_converse_stream_is_read_into_one_turn():
    stream = [{"messageStart": {"role": "assistant"}},
              {"contentBlockDelta": {"delta": {"text": "Ett ögonblick, "}}},
              {"contentBlockDelta": {"delta": {"text": "jag kollar."}}},
              {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "tu1", "name": "hold_table"}}}},
              {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"date": "2026-11-03", '}}}},
              {"contentBlockDelta": {"delta": {"toolUse": {"input": '"time": "19:00", "partySize": 4}'}}}},
              {"contentBlockStop": {}},
              {"messageStop": {"stopReason": "tool_use"}}]
    turn = read_stream(stream)
    assert turn.text == "Ett ögonblick, jag kollar." and turn.stop_reason == "tool_use"
    assert turn.tool_uses == (ToolUse("tu1", "hold_table", {"date": "2026-11-03", "time": "19:00",
                                                             "partySize": 4}),)


class FakeBedrock:
    def __init__(self, stream=None, error=None):
        self.stream, self.error, self.args = stream or [], error, None

    def converse_stream(self, **args):
        self.args = args
        if self.error:
            raise self.error
        return {"stream": self.stream}


def test_real_agent_model_calls_converse_stream_on_the_configured_model():
    bedrock = FakeBedrock([{"contentBlockDelta": {"delta": {"text": "Hej!"}}}, {"messageStop": {"stopReason":
                                                                                                "end_turn"}}])
    model = RealAgentModel(client=bedrock, model_id="eu.anthropic.claude-haiku")
    turn = model.converse("system", [{"role": "user", "content": "Hej"}], TOOLS, max_tokens=400)
    assert turn.text == "Hej!"
    assert bedrock.args["modelId"] == "eu.anthropic.claude-haiku" and bedrock.args["system"] == [{"text": "system"}]
    assert bedrock.args["inferenceConfig"]["maxTokens"] == 400 and "toolConfig" in bedrock.args


def test_real_agent_model_errors():
    from botocore.exceptions import ClientError
    throttled = ClientError({"Error": {"Code": "ThrottlingException"}}, "ConverseStream")
    denied = ClientError({"Error": {"Code": "AccessDeniedException"}}, "ConverseStream")
    for error, code, retry in ((throttled, "unavailable", True), (denied, "rejected", False)):
        with pytest.raises(AgentModelError) as raised:
            RealAgentModel(client=FakeBedrock(error=error), model_id="m").converse("s", [], [], max_tokens=5)
        assert (raised.value.code, raised.value.retryable) == (code, retry)
