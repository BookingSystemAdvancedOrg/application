"""phone-agent-call

TRIGGER:
    API Gateway -- Auth: NONE (provider signature, throttle `public`)
    POST /public/phone-agent/telephony/webhook   L13 SIP provider events (inbound, ended)
    POST /public/phone-agent/voice/webhook       L14 voice provider events (call_started, transcript,
                                                     language_detected, interrupted, transfer_result, call_ended)
    Lambda Function URL -- POST /v1/chat/completions (OpenAI-compatible; header x-booqy-agent-key =
                                                     the `${prefix}phone-agent/llm-endpoint-key` secret)
                                                     the agent turn endpoint: one caller utterance -> reply

PURPOSE:
    The real-time side of the AI phone agent (ADR-L decisions 3, 5, 6, 8, 9,
    10): call rows and their state machine, the art. 50 disclosure (said by
    Booqy before the model's first turn, repeated on interruption and once
    per new language), the guards (entitlement, location, tenant pause, kill
    switches, agent hours, minutes cap), the agent loop with its tools, and
    metering at call end. A Booqy outage never means a dead number: the SIP
    provider rings the restaurant's own phone when our side fails.

    After call_started the voice provider echoes `tenantId` and `callId`
    (dynamic variables returned by L14 at call start) in every event and turn
    request inside its signed body; both are checked against the call row.

ENV_VARS:
    ENVIRONMENT, PHONE_AGENT_TABLE_NAME, NUMBER_INDEX_NAME, TENANT_TABLE_NAME,
    LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME, USER_TABLE_NAME, USER_TENANT_INDEX_NAME,
    EVENT_BUS_NAME, NOTIFICATION_FUNCTION_NAME, LLM_ENDPOINT_KEY_SECRET_ARN,
    VOICE_CLIENT, TELEPHONY_CLIENT, AGENT_MODEL_CLIENT, PHONE_AGENT_MODEL_ID
"""

import hmac
import json
import logging
import os
from datetime import timedelta

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import agent_model, http, telephony, tenant, voice_provider
from shared.features import phone_agent as pa
from shared.features.phone_agent import calls, copy, session
from shared.features.phone_agent.tools import lambda_invoker

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_clients = {}
_secret = {}


def client(name):
    """External ports and AWS clients, one per container (tests replace them)."""
    if name not in _clients:
        _clients[name] = {"voice": voice_provider.client, "telephony": telephony.client,
                          "model": agent_model.client}.get(name, lambda: boto3.client(name))()
    return _clients[name]


def invoker():
    if "invoke" not in _clients:
        _clients["invoke"] = lambda_invoker(client("lambda"))
    return _clients["invoke"]


def _ok(body=None):
    return http.respond(200, body or {})


def _not_found():
    return http.respond(404, {"error": "not found", "code": "not_found"})


# --- lookups -----------------------------------------------------------------------------

def by_number(e164):
    """(tenant_id, location_id) of a Booqy number (GSI byNumber), else (None, None)."""
    if not isinstance(e164, str) or not e164:
        return None, None
    items = pa.table().query(IndexName=os.environ.get("NUMBER_INDEX_NAME", "byNumber"),
                             KeyConditionExpression=Key("GSI1PK").eq(f"NUMBER#{e164}")).get("Items") or []
    if len(items) != 1:
        return None, None
    return items[0].get("tenantId"), items[0].get("locationId")


def _context(tenant_id, location_id):
    tenant_row = tenant.get_tenant(tenant_id, fresh=True) or {}
    location = tenant.get_location(tenant_id, location_id) or {}
    return tenant_row, location, pa.get_config(tenant_id), pa.get_loc(tenant_id, location_id)


def _call_of(data, call_ref=None, at=None):
    """The call an event belongs to: by the echoed tenantId/callId, else by
    the called number and the provider's call reference."""
    tenant_id, call_id = data.get("tenantId"), data.get("callId")
    if isinstance(tenant_id, str) and isinstance(call_id, str):
        call = calls.get_call(tenant_id, call_id)
        return call if call and (call_ref is None or call.get("callRef") == call_ref) else None
    tenant_id, _ = by_number(data.get("to"))
    if not tenant_id or not call_ref:
        return None
    at = at or pa.now()
    for when in (at, at - timedelta(days=1)):  # a call that started before midnight
        call = calls.get_call(tenant_id, pa.call_id_for(call_ref, when))
        if call and call.get("callRef") == call_ref:
            return call
    return None


def add_callback_for(call):
    def add(*, name, phone, topic, preferred=None):
        at = pa.now()
        callback_id = call["callId"] + "-cb"
        item = {**pa.callback_key(call["tenantId"], callback_id), "tenantId": call["tenantId"],
                "callbackId": callback_id, "locationId": call["locationId"], "callId": call["callId"],
                "name": name or None, "phone": phone or None, "phoneHash": pa.phone_hash(call["tenantId"], phone)
                if phone else None, "topic": topic, "preferredTime": preferred, "status": "open",
                "createdAt": pa.iso(at), "version": 1}
        try:
            pa.table().put_item(Item={k: v for k, v in item.items() if v is not None},
                                ConditionExpression="attribute_not_exists(PK)")
            calls.publish("PhoneCallback.Requested", {"tenantId": call["tenantId"], "locationId": call["locationId"],
                                                      "callbackId": callback_id, "callId": call["callId"]})
        except ClientError as exc:
            if not pa.conditional_failed(exc):
                raise
        return callback_id
    return add


# --- call start and end ------------------------------------------------------------------

def start_call(*, call_ref, caller, to, at):
    """A new call on a Booqy number: the row, then the guards; refused calls
    hear the refusal sentence and end (0 minutes, counted in the list)."""
    tenant_id, location_id = by_number(to)
    if not tenant_id:
        return None
    call, created = calls.start(tenant_id=tenant_id, location_id=location_id, call_ref=call_ref, caller=caller,
                                at=at)
    if not created or call.get("status") != "ringing":
        return call
    tenant_row, location, config, loc = _context(tenant_id, location_id)
    reason = pa.refusal({**tenant_row, "tenantId": tenant_id}, config, loc, at,
                        timezone_name=location.get("timezone") or "Europe/Stockholm")
    voice = client("voice")
    if reason:
        call = calls.refuse(call, reason, at)
        session.metric("CallsRefused", 1)
        try:
            voice.say(call_ref, copy.REFUSED["sv"])
            voice.end(call_ref, "refused")
        except voice_provider.VoiceProviderError as exc:
            logger.warning(json.dumps({"voice": "refuse_failed", "code": exc.code}))
        return calls.process(calls.end(call, at, "refused"), at=at)
    lang = "sv"
    recording = bool((config.get("recording") or {}).get("enabled"))
    greeting = copy.greeting(lang, config.get("agentName"), location.get("name"), loc.get("greetingExtraSv"),
                             recording=recording)
    call, _ = calls.answer(call, greeting=greeting, lang=lang, at=at, restaurant=location.get("name") or "")
    session.metric("CallsAnswered", 1)
    try:
        voice.say(call_ref, greeting)
    except voice_provider.VoiceProviderError as exc:
        session.metric("ProviderErrors", 1)
        logger.warning(json.dumps({"voice": "say_failed", "code": exc.code}))
    return call


def _warn(tenant_id, minutes, cap):
    """phone-agent.cap-warning to every active owner (notification:live, Event)."""
    table_name, index = os.environ.get("USER_TABLE_NAME"), os.environ.get("USER_TENANT_INDEX_NAME")
    if not table_name or not index:
        return
    owners = boto3.resource("dynamodb").Table(table_name).query(
        IndexName=index, KeyConditionExpression=Key("tenantId").eq(tenant_id)).get("Items") or []
    for owner in owners:
        if owner.get("role") != "owner_user" or owner.get("status") != "active" or not owner.get("email"):
            continue
        notice = {"version": 1, "kind": "phone-agent.cap-warning", "tenantId": tenant_id, "channel": "email",
                  "to": owner["email"], "subject": "Telefonassistenten: 80 % av minuterna använda",
                  "text": f"Telefonassistenten har använt {minutes} av {cap} minuter den här perioden. "
                          "När taket nås svarar den inte längre i telefon.", "replyTo": None}
        try:
            client("lambda").invoke(FunctionName=os.environ["NOTIFICATION_FUNCTION_NAME"], InvocationType="Event",
                                    Payload=json.dumps({"notice": notice}).encode("utf-8"))
        except (BotoCoreError, ClientError, KeyError) as exc:
            logger.warning(json.dumps({"notice": "cap-warning", "invoke": "failed", "type": type(exc).__name__}))


def finish(call, at, reason="hangup"):
    """ended -> processed (metered once)."""
    if call.get("status") == "processed":
        return call
    ended = calls.end(call, at, reason) if call.get("status") in calls.OPEN else call
    state = json.loads(ended.get("toolState") or "{}")
    tenant_row = {**(tenant.get_tenant(ended["tenantId"], fresh=True) or {}), "tenantId": ended["tenantId"]}
    config = pa.get_config(ended["tenantId"])
    done = calls.process(ended, state=state, at=at,
                         notify_cap=lambda t, end: calls.check_cap(tenant_row, config, end, warn=_warn))
    session.metric("BilledMinutes", int(done.get("billedMinutes") or 0))
    return done


# --- webhooks ----------------------------------------------------------------------------

def telephony_webhook(event):
    try:
        found = client("telephony").parse_webhook(event.get("headers") or {}, event.get("body") or "")
    except telephony.TelephonyError:
        return _not_found()
    for item in found:
        at = pa.parse_iso(item.data.get("at")) or pa.now()
        if item.kind == "inbound":
            if start_call(call_ref=item.call_ref, caller=item.data.get("from"), to=item.data.get("to"), at=at) is None:
                return _not_found()
        elif item.kind == "ended":
            call = _call_of(item.data, item.call_ref, at)
            if call:
                finish(call, at, item.data.get("cause") or "hangup")
    return _ok()


def voice_webhook(event):
    try:
        found = client("voice").parse_webhook(event.get("headers") or {}, event.get("body") or "")
    except voice_provider.VoiceProviderError:
        return _not_found()
    answer = {}
    for item in found:
        at = pa.parse_iso(item.data.get("at")) or pa.now()
        if item.kind == "call_started":
            call = start_call(call_ref=item.call_ref, caller=item.data.get("from"), to=item.data.get("to"), at=at)
            if call is None:
                return _not_found()
            answer = {"dynamicVariables": {"tenantId": call["tenantId"], "callId": call["callId"]}}
            continue
        call = _call_of(item.data, item.call_ref, at)
        if not call:
            continue
        voice = client("voice")
        if item.kind == "transcript":
            text = (call.get("disclosure") or {}).get("text") or ""
            if item.data.get("role") == "agent" and item.data.get("final") and text and \
                    item.data.get("text", "").strip() == text.strip():
                calls.disclosed(call, at)
        elif item.kind == "interrupted" and call.get("status") == "answered":
            call, said = calls.repeat_disclosure(call, call.get("language") or "sv", at)
            if said:
                voice.say(item.call_ref, said)
        elif item.kind == "language_detected" and item.data.get("lang") in pa.LANGUAGES:
            lang = item.data["lang"]
            call, said = calls.repeat_disclosure(call, lang, at, new_language=True)
            calls.set_status(call, call["status"], [call["status"]], language=lang)
            if said:
                voice.say(item.call_ref, said)
        elif item.kind == "transfer_result":
            transfer = {"answered": bool(item.data.get("answered")), "at": pa.iso(at)}
            calls.set_status(call, "talking", ["transferring", "talking"], transfer=transfer)
        elif item.kind == "call_ended":
            finish(call, at, item.data.get("reason") or "hangup")
    return _ok(answer)


# --- the agent turn endpoint (Function URL) -----------------------------------------------

def _endpoint_key():
    if "key" not in _secret:
        _secret["key"] = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["LLM_ENDPOINT_KEY_SECRET_ARN"])["SecretString"]
    return _secret["key"]


def _last_caller_text(messages):
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            return " ".join(str(content or "").split())
    return ""


def _sse(text, status=200):
    chunk = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant",
                                                                                    "content": text},
                                                              "finish_reason": "stop"}]}
    return {"statusCode": status, "headers": {"Content-Type": "text/event-stream", "Cache-Control": "no-store"},
            "body": f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"}


def turn_endpoint(event):
    supplied = http.header(event, "x-booqy-agent-key") or ""
    if not supplied or not hmac.compare_digest(supplied.encode(), _endpoint_key().encode()):
        return {"statusCode": 401, "body": ""}
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return {"statusCode": 400, "body": ""}
    meta = body.get("booqy") or body.get("extra_body") or {}
    call = _call_of(meta) if isinstance(meta, dict) else None
    if not call or call.get("status") not in ("answered", "disclosed", "talking", "transferring"):
        return {"statusCode": 404, "body": ""}  # unknown or ended call
    text = _last_caller_text(body.get("messages"))
    if not text:
        return _sse("")
    reply = run_call_turn(call, text, pa.now())
    return _sse(reply)


def run_call_turn(call, text, at):
    tenant_row, location, config, loc = _context(call["tenantId"], call["locationId"])
    location = {**location, "locationId": call["locationId"]}
    started = pa.parse_iso(call.get("answeredAt")) or at
    max_seconds = int(loc.get("maxCallSeconds") or pa.MAX_CALL_DEFAULT)
    lang = call.get("language") or "sv"
    if at - started > timedelta(seconds=max_seconds):
        finish(call, at, "max_length")
        return copy.MAX_LENGTH[lang]
    result, updated, state = session.call_turn(
        call, text, model=client("model"), invoke=invoker(), voice=client("voice"), tenant_row=tenant_row,
        location=location, loc=loc, config=config, at=at, add_callback=add_callback_for(call))
    if result.error:
        session.metric("ProviderErrors", 1)
    if (state.get("transfer") or {}).get("answered"):
        finish(updated, at, "transferred")
    return result.reply


# --- dispatch ----------------------------------------------------------------------------

ROUTES = {
    "POST /public/phone-agent/telephony/webhook": telephony_webhook,
    "POST /public/phone-agent/voice/webhook": voice_webhook,
}


def handler(event, context=None):
    method, path = http.route(event)
    try:
        if event.get("routeKey") in (None, "$default") and event.get("rawPath") == "/v1/chat/completions":
            return turn_endpoint(event) if method == "POST" else _not_found()
        fn = ROUTES.get(f"{method} {path}")
        if fn is None:
            return _not_found()
        return fn(event)
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"route": path, "aws": type(exc).__name__}))
        return http.respond(503, {"error": "service_unavailable", "code": "service_unavailable"})
