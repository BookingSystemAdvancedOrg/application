"""The real voice provider: ElevenLabs Agents Platform (Conversational AI)
over a SIP trunk, with its "custom LLM" pointed at Booqy's agent turn
endpoint (ADR-L decision 3). ElevenLabs does speech in and out, turn-taking,
barge-in and language detection; every word the agent says is produced by
Booqy's agent loop on Bedrock, so tools and tenant data never leave Booqy.

One ElevenLabs agent per location (idempotent by name). The agent:
  * starts in Swedish (`agent.language = "sv"`) with a multilingual low-latency
    TTS model (`PHONE_AGENT_TTS_MODEL`, default `eleven_flash_v2_5`) and a stock
    library voice (`PHONE_AGENT_VOICE_ID`, never a clone);
  * has `language_presets` for every other language the voice speaks and
    the `language_detection` system tool, which Booqy's turn endpoint calls when
    the caller switches language (the agent loop decides, ElevenLabs follows);
  * keeps barge-in on, but the first message (the art. 50 disclosure) cannot be
    interrupted (`disable_first_message_interruptions`);
  * gets its first message per call from Booqy's conversation initiation
    webhook (the greeting with the disclosure, or the refusal sentence), which
    also hands over `tenantId` and `callId` as dynamic variables and as the
    custom LLM extra body;
  * has `end_call` and (with a transfer number) `transfer_to_number`, called by
    the turn endpoint as OpenAI tool calls (`deferred_tools`).

ElevenLabs cannot inject speech outside an LLM turn, so `say()` is not used:
Booqy queues such sentences on the call row and says them first in the next
reply (`inline_speech`).

Secret `${prefix}phone-agent/voice-provider` (VOICE_SECRET_ARN, set by the
owner, JSON): `apiKey` (xi-api-key), `webhookSecret` (the post-call webhook's
HMAC secret from the ElevenLabs workspace), `initiationToken` (a random string
Booqy expects in `x-booqy-webhook-token` on the initiation webhook), `callUrl`
(the phone-agent-call Function URL, Terraform output `phone_agent_turn_url`).
The custom LLM key is LLM_ENDPOINT_KEY_SECRET_ARN; both tokens are stored in
the ElevenLabs workspace as secrets and referenced by id, never in plain text
in an agent.

Marked [VERIFY IN SANDBOX] where the ElevenLabs docs did not confirm a shape.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3

from shared.voice_provider import SipTarget, TransferResult, VoiceEvent, VoiceProvider, VoiceProviderError, _header

API_BASE = "https://api.elevenlabs.io"          # EU residency: https://api.eu.residency.elevenlabs.io
SIP_HOST = "sip.rtc.elevenlabs.io"
TTS_MODEL = "eleven_flash_v2_5"                  # low latency, Swedish among its languages
# 46elks' SIP egress addresses (46elks.com/docs/ip-addresses): 46elks cannot do
# SIP digest auth, so the ElevenLabs trunk allows these addresses over TCP only.
ELKS_SIP_ADDRESSES = ("176.10.154.205", "85.24.146.136")
SIGNATURE_HEADER = "elevenlabs-signature"
TOKEN_HEADER = "x-booqy-webhook-token"
SIGNATURE_TOLERANCE_S = 30 * 60
TIMEOUT_S = 8
CALL_MARKER = "booqy-call tenant={{tenantId}} call={{callId}}"
_MARKER = re.compile(r"booqy-call tenant=([0-9A-Za-z_-]+) call=(\d{8}[0-9a-f]{16})")


def call_ids_from_prompt(text):
    """(tenantId, callId) from the agent prompt ElevenLabs sends with every
    turn (the dynamic variables are substituted), else (None, None)."""
    m = _MARKER.search(str(text or ""))
    return (m.group(1), m.group(2)) if m else (None, None)


def _urllib_transport(method, url, headers, body):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"},
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, TimeoutError, OSError):
        raise VoiceProviderError("unavailable", True) from None


def _load_secret(arn):
    if not arn:
        raise VoiceProviderError("not_configured")
    try:
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
    except Exception:  # noqa: BLE001 - any failure reads as "not configured", the key never logged
        raise VoiceProviderError("not_configured") from None
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    return parsed if isinstance(parsed, dict) else {"value": raw}


class RealVoiceProvider(VoiceProvider):
    inline_speech = True
    deferred_tools = True

    def __init__(self, *, secret=None, llm_key=None, transport=None, now=None):
        self._secret = secret
        self._llm_key = llm_key
        self._transport = transport or _urllib_transport
        self._now = now or time.time
        self._workspace_secrets = {}
        self.base = os.environ.get("ELEVENLABS_API_BASE") or API_BASE
        self.sip_host = os.environ.get("ELEVENLABS_SIP_HOST") or SIP_HOST
        self.voice_id = os.environ.get("PHONE_AGENT_VOICE_ID") or ""
        self.tts_model = os.environ.get("PHONE_AGENT_TTS_MODEL") or TTS_MODEL
        self.transfer_type = os.environ.get("PHONE_AGENT_TRANSFER_TYPE") or "sip_refer"

    # -- plumbing ---------------------------------------------------------------------

    @property
    def secret(self):
        if self._secret is None:
            self._secret = _load_secret(os.environ.get("VOICE_SECRET_ARN"))
        return self._secret

    @property
    def llm_key(self):
        if self._llm_key is None:
            self._llm_key = _load_secret(os.environ.get("LLM_ENDPOINT_KEY_SECRET_ARN")).get("value") or ""
        return self._llm_key

    def _api(self, method, path, body=None, *, ok=(200, 201, 204)):
        key = self.secret.get("apiKey")
        if not key:
            raise VoiceProviderError("not_configured")
        status, answer = self._transport(method, self.base + path, {"xi-api-key": key}, body)
        if status == 404:
            raise VoiceProviderError("not_found")
        if status in (429,) or status >= 500:
            raise VoiceProviderError("unavailable", True)
        if status not in ok:
            raise VoiceProviderError("rejected")
        return answer if isinstance(answer, dict) else {"items": answer}

    def _call_url(self):
        url = str(self.secret.get("callUrl") or "")
        if not url.startswith("https://"):
            raise VoiceProviderError("not_configured")
        return url.rstrip("/") + "/"

    def _workspace_secret(self, name, value):
        """The id of a workspace secret holding `value` (created once by name)
        [VERIFY IN SANDBOX: /v1/convai/secrets]."""
        if not value:
            raise VoiceProviderError("not_configured")
        if name not in self._workspace_secrets:
            listed = self._api("GET", "/v1/convai/secrets").get("secrets") or []
            found = next((s.get("secret_id") for s in listed if s.get("name") == name), None)
            if not found:
                found = self._api("POST", "/v1/convai/secrets", {"type": "new", "name": name,
                                                                 "value": value}).get("secret_id")
            if not found:
                raise VoiceProviderError("rejected")
            self._workspace_secrets[name] = found
        return self._workspace_secrets[name]

    # -- the agent per location -------------------------------------------------------

    def agent_body(self, config):
        name = config.get("name") if isinstance(config, dict) else None
        if not name:
            raise VoiceProviderError("invalid_config")
        if not self.voice_id:
            raise VoiceProviderError("not_configured")
        prefix = name.rsplit("phone-agent-", 1)[0]
        url = self._call_url()
        llm_secret = self._workspace_secret(f"{prefix}booqy-llm-endpoint-key", self.llm_key)
        init_secret = self._workspace_secret(f"{prefix}booqy-webhook-token", self.secret.get("initiationToken"))
        greeting_sv = str(config.get("greetingSv") or "")
        greeting_en = str(config.get("greetingEn") or greeting_sv)
        # Every language the voice speaks: the call follows whatever the caller speaks.
        from shared.features.phone_agent.language import VOICE_LANGUAGES
        others = [lang for lang in VOICE_LANGUAGES if lang != "sv"]
        tools = {"end_call": {"type": "system", "name": "end_call", "params": {"system_tool_type": "end_call"}}}
        if others:
            tools["language_detection"] = {"type": "system", "name": "language_detection",
                                           "params": {"system_tool_type": "language_detection"}}
        if config.get("transferTo"):
            tools["transfer_to_number"] = {
                "type": "system", "name": "transfer_to_number",
                "params": {"system_tool_type": "transfer_to_number", "enable_client_message": True,
                           "transfers": [{
                               # [VERIFY IN SANDBOX: transfer_destination shape]
                               "transfer_destination": {"type": "phone", "phone_number": config["transferTo"]},
                               "condition": "The caller wants to speak to a person at the restaurant.",
                               "transfer_type": self.transfer_type}]}}
        conversation_config = {
            "agent": {
                "first_message": greeting_sv,
                "language": "sv",
                "disable_first_message_interruptions": True,
                "dynamic_variables": {"dynamic_variable_placeholders": {"tenantId": "", "callId": ""}},
                "prompt": {
                    "prompt": CALL_MARKER,
                    "llm": "custom-llm",
                    "custom_llm": {"url": url + "v1", "model_id": "booqy-phone-agent",
                                   "api_key": {"secret_id": llm_secret}, "api_type": "chat_completions"},
                    "built_in_tools": tools,
                },
            },
            "tts": {"voice_id": self.voice_id, "model_id": self.tts_model},
            # Barge-in stays on (the default); a patient turn-taker for people on the phone.
            "turn": {"turn_eagerness": "normal"},
            "conversation": {"max_duration_seconds": int(config.get("maxDurationSeconds") or 480)},
            "language_presets": {lang: {"overrides": {"agent": {"first_message": greeting_en}}} for lang in others},
        }
        platform_settings = {
            "overrides": {
                "enable_conversation_initiation_client_data_from_webhook": True,
                "custom_llm_extra_body": True,
                "conversation_config_override": {"agent": {"first_message": True, "language": True}},
            },
            "workspace_overrides": {"conversation_initiation_client_data_webhook": {
                "url": url + "public/phone-agent/voice/webhook",
                "request_headers": {TOKEN_HEADER: {"secret_id": init_secret}}}},
            # Booqy keeps the transcript; no audio at ElevenLabs (recording is Booqy's opt-in).
            "privacy": {"record_voice": False, "delete_audio": True, "retention_days": 30},
        }
        return {"name": name, "conversation_config": conversation_config, "platform_settings": platform_settings}

    def _find_agent(self, name):
        query = urllib.parse.urlencode({"search": name, "page_size": 100})
        listed = self._api("GET", f"/v1/convai/agents?{query}").get("agents") or []
        return next((a.get("agent_id") for a in listed if a.get("name") == name), None)

    def create_agent(self, config):
        body = self.agent_body(config)
        existing = self._find_agent(body["name"])
        if existing:
            self._api("PATCH", f"/v1/convai/agents/{existing}", body)
            return existing
        agent_id = self._api("POST", "/v1/convai/agents/create", body).get("agent_id")
        if not agent_id:
            raise VoiceProviderError("rejected")
        return agent_id

    def update_agent(self, provider_agent_id, config):
        self._api("PATCH", f"/v1/convai/agents/{provider_agent_id}", self.agent_body(config))

    def delete_agent(self, provider_agent_id):
        try:
            self._api("DELETE", f"/v1/convai/agents/{provider_agent_id}")
        except VoiceProviderError as exc:
            if exc.code != "not_found":
                raise

    def sip_target(self, provider_agent_id, e164=None):
        """Imports the Booqy number as an ElevenLabs SIP trunk number assigned
        to the agent (once; later calls re-assign it) and answers the URI the
        telephony provider forwards to (TCP, 46elks' addresses only)."""
        if not e164:
            raise VoiceProviderError("invalid_config")
        listed = self._api("GET", "/v1/convai/phone-numbers")
        numbers = listed.get("items") if "items" in listed else listed.get("phone_numbers") or []
        found = next((n for n in numbers or [] if n.get("phone_number") == e164), None)
        if found:
            self._api("PATCH", f"/v1/convai/phone-numbers/{found['phone_number_id']}", {"agent_id": provider_agent_id})
        else:
            self._api("POST", "/v1/convai/phone-numbers", {
                "phone_number": e164, "label": f"booqy {e164}", "provider": "sip_trunk", "agent_id": provider_agent_id,
                "inbound_trunk_config": {"allowed_addresses": list(ELKS_SIP_ADDRESSES),
                                         "media_encryption": "allowed"}})
        return SipTarget(f"sip:{e164}@{self.sip_host}:5060;transport=tcp")

    # -- during a call ------------------------------------------------------------------

    def say(self, call_ref, text):
        raise VoiceProviderError("inline_only")     # callers check `inline_speech` first

    def transfer(self, call_ref, to_e164, timeout_s):
        # Done by the transfer_to_number system tool in the turn endpoint's answer.
        return TransferResult(True)

    def end(self, call_ref, reason):
        return None                                 # the end_call system tool in the next answer

    def recording_url(self, call_ref):
        return None                                 # record_voice is off at ElevenLabs (privacy above)

    # -- webhooks ---------------------------------------------------------------------

    def _verify_signature(self, header, raw):
        secret = str(self.secret.get("webhookSecret") or "")
        parts = dict(p.split("=", 1) for p in str(header or "").split(",") if "=" in p)
        stamp, given = parts.get("t", ""), parts.get("v0", "")
        if not secret or not stamp.isdigit() or not given:
            raise VoiceProviderError("unauthorized")
        if abs(self._now() - int(stamp)) > SIGNATURE_TOLERANCE_S:
            raise VoiceProviderError("unauthorized")
        wanted = hmac.new(secret.encode(), f"{stamp}.{raw}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(wanted, given):
            raise VoiceProviderError("unauthorized")

    def parse_webhook(self, headers, body, **request):
        raw = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body or "")
        signature = _header(headers, SIGNATURE_HEADER)
        if signature:
            self._verify_signature(signature, raw)
            return self._post_call(_json(raw))
        token = str(_header(headers, TOKEN_HEADER) or "")
        expected = str(self.secret.get("initiationToken") or "")
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            raise VoiceProviderError("unauthorized")
        data = _json(raw)
        call_ref = data.get("conversation_id") or data.get("call_sid")
        if not call_ref or not data.get("called_number"):
            raise VoiceProviderError("unauthorized")
        return [VoiceEvent("call_started", str(call_ref), {"from": data.get("caller_id"),
                                                           "to": data.get("called_number")})]

    @staticmethod
    def _post_call(payload):
        """post_call_transcription -> the disclosure evidence (the first agent
        message) and call_ended; every other webhook type is acknowledged."""
        if payload.get("type") != "post_call_transcription":
            return []
        data = payload.get("data") or {}
        call_ref = data.get("conversation_id")
        dynamic = ((data.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {})
        ids = {k: dynamic[k] for k in ("tenantId", "callId") if isinstance(dynamic.get(k), str)}
        meta = data.get("metadata") or {}
        start = int(meta.get("start_time_unix_secs") or payload.get("event_timestamp") or time.time())
        seconds = int(meta.get("call_duration_secs") or 0)
        events = []
        first_agent = next((t for t in data.get("transcript") or [] if t.get("role") == "agent" and t.get("message")),
                           None)
        if first_agent:
            at = start + int(first_agent.get("time_in_call_secs") or 0)
            events.append(VoiceEvent("transcript", call_ref, {**ids, "role": "agent", "text": first_agent["message"],
                                                               "final": True, "at": _iso(at)}))
        events.append(VoiceEvent("call_ended", call_ref, {**ids, "at": _iso(start + seconds), "usage": {
            "seconds": seconds}, "reason": "hangup"}))
        return events

    def webhook_response(self, answer):
        """Booqy's call-start answer as ElevenLabs' conversation initiation data."""
        variables = (answer or {}).get("dynamicVariables")
        if not variables:
            return answer
        agent = {"language": answer.get("language") or "sv"}
        if answer.get("firstMessage"):
            agent["first_message"] = answer["firstMessage"]
        return {"type": "conversation_initiation_client_data", "dynamic_variables": dict(variables),
                "conversation_config_override": {"agent": agent},
                "custom_llm_extra_body": {"booqy": dict(variables)}}


def _iso(seconds):
    return datetime.fromtimestamp(int(seconds), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(raw):
    try:
        out = json.loads(raw or "{}")
    except ValueError:
        raise VoiceProviderError("unauthorized") from None
    if not isinstance(out, dict):
        raise VoiceProviderError("unauthorized")
    return out
