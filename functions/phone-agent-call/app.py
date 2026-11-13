"""phone-agent-call

TRIGGER:
    Lambda Function URL -- Auth: NONE (provider signature)
    POST /public/phone-agent/telephony/webhook   L13 SIP provider events (inbound, ended)
    POST /public/phone-agent/voice/webhook       L14 voice provider events (call_started, transcript,
                                                     language_detected, interrupted, transfer_result, call_ended)
        (CONTRACTS.md lists L13/L14 as API Gateway routes; feature-routes.tf sends a feature's
        routes to its API Lambda only, so until that hotspot allows a second integration the
        providers call these paths on this function's URL; reported in STATUS.md.)
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

import base64
import hmac
import time
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
from shared.features.phone_agent import locations as locations_mod
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

def number_row(e164):
    """The NUMBER# pointer of a Booqy number (GSI byNumber), else None."""
    if not isinstance(e164, str) or not e164:
        return None
    items = pa.table().query(IndexName=os.environ.get("NUMBER_INDEX_NAME", "byNumber"),
                             KeyConditionExpression=Key("GSI1PK").eq(f"NUMBER#{e164}")).get("Items") or []
    return items[0] if len(items) == 1 else None


def by_number(e164):
    """(tenant_id, location_id) of a Booqy number (GSI byNumber), else (None, None).
    location_id is the location the number belongs to (on a shared number, its owner)."""
    row = number_row(e164)
    return (row.get("tenantId"), row.get("locationId")) if row else (None, None)


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

def _candidates(tenant_row, config, row, at):
    """The locations a call on this number may be about: every location on
    the number that would answer now, in the number's order. Returns
    (candidates[(location, loc)], refusal reason of the owner location)."""
    tenant_id = row["tenantId"]
    ids = list(row.get("locationIds") or [row["locationId"]])
    if row["locationId"] not in ids:
        ids.insert(0, row["locationId"])
    found, reason = [], None
    for location_id in ids:
        location = {**(tenant.get_location(tenant_id, location_id) or {}), "locationId": location_id}
        loc = pa.get_loc(tenant_id, location_id)
        why = pa.refusal({**tenant_row, "tenantId": tenant_id}, config, loc, at,
                         timezone_name=location.get("timezone") or "Europe/Stockholm")
        if why is None:
            found.append((location, loc))
        elif location_id == row["locationId"] or reason is None:
            reason = why
    return found, reason or "entitlement"


def start_call(*, call_ref, caller, to, at):
    """A new call on a Booqy number: the row, then the guards; refused calls
    hear the refusal sentence and end (0 minutes, counted in the list). On a
    number several locations share, the greeting asks which one."""
    row = number_row(to)
    if not row:
        return None
    tenant_id = row["tenantId"]
    call, created = calls.start(tenant_id=tenant_id, location_id=row["locationId"], call_ref=call_ref,
                                caller=caller, at=at)
    if not created or call.get("status") != "ringing":
        return call
    tenant_row = tenant.get_tenant(tenant_id, fresh=True) or {}
    config = pa.get_config(tenant_id)
    candidates, reason = _candidates(tenant_row, config, row, at)
    voice = client("voice")
    if not candidates:
        call = calls.refuse(call, reason, at)
        session.metric("CallsRefused", 1)
        if not voice.inline_speech:   # an inline provider says it as the call's first message
            try:
                voice.say(call_ref, copy.REFUSED["sv"])
                voice.end(call_ref, "refused")
            except voice_provider.VoiceProviderError as exc:
                logger.warning(json.dumps({"voice": "refuse_failed", "code": exc.code}))
        return calls.process(calls.end(call, at, "refused"), at=at)
    lang = "sv"
    recording = bool((config.get("recording") or {}).get("enabled"))
    if len(candidates) == 1:
        location, loc = candidates[0]
        restaurant = location.get("name") or ""
        greeting = copy.greeting(lang, config.get("agentName"), restaurant, loc.get("greetingExtraSv"),
                                 recording=recording)
        if location["locationId"] != call["locationId"]:
            call = calls.set_status(call, "ringing", ["ringing"], locationId=location["locationId"]) or call
    else:
        restaurant = tenant_row.get("name") or ""
        names = [locations_mod.label(x) for x, _ in candidates]
        greeting = copy.greeting(lang, config.get("agentName"), restaurant, copy.which_location(lang, names),
                                 recording=recording)
        call = calls.set_status(call, "ringing", ["ringing"], locationPending=True,
                                candidateLocations=[x["locationId"] for x, _ in candidates]) or call
    call, _ = calls.answer(call, greeting=greeting, lang=lang, at=at, restaurant=restaurant)
    session.metric("CallsAnswered", 1)
    if not voice.inline_speech:
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

def _raw_body(event):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return body


def _request(event):
    """What a provider adapter may check besides headers and body."""
    return {"source_ip": ((event.get("requestContext") or {}).get("http") or {}).get("sourceIp"),
            "query": event.get("queryStringParameters") or {}}


def telephony_webhook(event):
    try:
        found = client("telephony").parse_webhook(event.get("headers") or {}, _raw_body(event), **_request(event))
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


def _first_message(call):
    """What the caller hears first: the greeting with the disclosure, or the refusal."""
    if call.get("outcome") == "refused":
        return copy.REFUSED["sv"]
    return (call.get("disclosure") or {}).get("text") or ""


def voice_webhook(event):
    voice = client("voice")
    try:
        found = voice.parse_webhook(event.get("headers") or {}, _raw_body(event), **_request(event))
    except voice_provider.VoiceProviderError:
        return _not_found()
    answer = {}
    for item in found:
        at = pa.parse_iso(item.data.get("at")) or pa.now()
        if item.kind == "call_started":
            call = start_call(call_ref=item.call_ref, caller=item.data.get("from"), to=item.data.get("to"), at=at)
            if call is None:
                return _not_found()
            answer = {"dynamicVariables": {"tenantId": call["tenantId"], "callId": call["callId"]},
                      "firstMessage": _first_message(call), "language": "sv"}
            continue
        call = _call_of(item.data, item.call_ref, at)
        if not call:
            continue
        if item.kind == "transcript":
            # Art. 50 evidence: the first final agent transcript of the call is
            # the greeting Booqy played before the model's first turn. The
            # provider's own text is stored as it came (calls.disclosed marks
            # whether it matches the greeting once normalised); it is never
            # required to match exactly, since a transcript of played speech
            # rarely does. A provider that reports transcripts after the call
            # (ElevenLabs' post-call webhook) records the same evidence then.
            if item.data.get("role") == "agent" and item.data.get("final") and \
                    not (call.get("disclosure") or {}).get("playedAt") and call.get("outcome") != "refused":
                calls.disclosed(call, at, spoken=item.data.get("text"))
        elif item.kind == "interrupted" and call.get("status") == "answered":
            call, said = calls.repeat_disclosure(call, call.get("language") or "sv", at)
            if said and not voice.inline_speech:
                voice.say(item.call_ref, said)
        elif item.kind == "language_detected" and item.data.get("lang") in pa.LANGUAGES:
            lang = item.data["lang"]
            call, said = calls.repeat_disclosure(call, lang, at, new_language=True)
            calls.set_status(call, call["status"], [call["status"]], language=lang)
            if said and not voice.inline_speech:
                voice.say(item.call_ref, said)
        elif item.kind == "transfer_result":
            transfer = {"answered": bool(item.data.get("answered")), "at": pa.iso(at)}
            calls.set_status(call, "talking", ["transferring", "talking"], transfer=transfer)
        elif item.kind == "call_ended":
            finish(call, at, item.data.get("reason") or "hangup")
    return _ok(voice.webhook_response(answer))


# --- the agent turn endpoint (Function URL) -----------------------------------------------

def _endpoint_key():
    if "key" not in _secret:
        _secret["key"] = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["LLM_ENDPOINT_KEY_SECRET_ARN"])["SecretString"]
    return _secret["key"]


def _content(m):
    content = m.get("content")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return " ".join(str(content or "").split())


def _last_caller_text(messages):
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return _content(m)
    return ""


def _call_ids(body):
    """tenantId and callId of a turn request: Booqy's own field, the custom
    LLM extra body ElevenLabs forwards, else the call marker in the agent
    prompt (dynamic variables substituted by the provider)."""
    for meta in (body.get("booqy"), (body.get("elevenlabs_extra_body") or {}).get("booqy"), body.get("extra_body")):
        if isinstance(meta, dict) and meta.get("tenantId") and meta.get("callId"):
            return {"tenantId": meta["tenantId"], "callId": meta["callId"]}
    from shared.voice_provider_real import call_ids_from_prompt
    for m in body.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "system":
            tenant_id, call_id = call_ids_from_prompt(_content(m))
            if tenant_id:
                return {"tenantId": tenant_id, "callId": call_id}
    return {}


def _offered_tools(body):
    names = set()
    for t in body.get("tools") or []:
        if isinstance(t, dict):
            names.add((t.get("function") or {}).get("name") or t.get("name"))
    return names


def _sse(text, status=200, tool_calls=()):
    """An OpenAI chat-completions stream: the reply, then any system tool calls
    (ElevenLabs: language_detection, transfer_to_number, end_call)."""
    base = {"id": "booqy-" + str(int(time.time() * 1000)), "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "booqy-phone-agent"}
    chunks = []
    if text or not tool_calls:
        chunks.append({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                            "finish_reason": None if tool_calls else "stop"}]})
    for i, use in enumerate(tool_calls):
        chunks.append({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": i, "id": f"call_{i}", "type": "function",
            "function": {"name": use["name"], "arguments": json.dumps(use.get("arguments") or {},
                                                                      ensure_ascii=False)}}]},
            "finish_reason": None}]})
    if tool_calls:
        chunks.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return {"statusCode": status, "headers": {"Content-Type": "text/event-stream", "Cache-Control": "no-store"},
            "body": body}


def _supplied_key(event):
    supplied = http.header(event, "x-booqy-agent-key") or ""
    if not supplied:
        auth = http.header(event, "authorization") or ""
        supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return supplied


def turn_endpoint(event):
    supplied = _supplied_key(event)
    if not supplied or not hmac.compare_digest(supplied.encode(), _endpoint_key().encode()):
        return {"statusCode": 401, "body": ""}
    try:
        body = json.loads(_raw_body(event) or "{}")
    except ValueError:
        return {"statusCode": 400, "body": ""}
    if not isinstance(body, dict):
        return {"statusCode": 400, "body": ""}
    call = _call_of(_call_ids(body))
    offered = _offered_tools(body)
    if call and call.get("status") in ("ended", "processed") and "end_call" in offered:
        return _sse("", tool_calls=[{"name": "end_call", "arguments": {"reason": "call_ended"}}])
    if not call or call.get("status") not in ("answered", "disclosed", "talking", "transferring"):
        return {"statusCode": 404, "body": ""}  # unknown or ended call
    text = _last_caller_text(body.get("messages"))
    if not text:
        return _sse("")
    reply, actions = run_call_turn(call, text, pa.now())
    return _sse(reply, tool_calls=[a for a in actions if a["name"] in offered])


def run_call_turn(call, text, at):
    """One caller utterance -> (reply, provider actions)."""
    lang = call.get("language") or "sv"
    if call.get("locationPending"):
        options = [{**(tenant.get_location(call["tenantId"], x) or {}), "locationId": x}
                   for x in call.get("candidateLocations") or []]
        call, reply = session.choose_location(call, text, locations=options, at=at, lang=lang)
        if reply is not None:
            return reply, []
    tenant_row, location, config, loc = _context(call["tenantId"], call["locationId"])
    location = {**location, "locationId": call["locationId"]}
    started = pa.parse_iso(call.get("answeredAt")) or at
    max_seconds = int(loc.get("maxCallSeconds") or pa.MAX_CALL_DEFAULT)
    if at - started > timedelta(seconds=max_seconds):
        finish(call, at, "max_length")
        return copy.pick(copy.MAX_LENGTH, lang), [{"name": "end_call", "arguments": {"reason": "max_length"}}]
    result, updated, state, actions = session.call_turn(
        call, text, model=client("model"), invoke=invoker(), voice=client("voice"), tenant_row=tenant_row,
        location=location, loc=loc, config=config, at=at, add_callback=add_callback_for(updated_or(call)))
    if result.error:
        session.metric("ProviderErrors", 1)
    if (state.get("transfer") or {}).get("answered"):
        finish(updated, at, "transferred")
    return result.reply, actions


def updated_or(call):
    return calls.get_call(call["tenantId"], call["callId"]) or call


# --- dispatch ----------------------------------------------------------------------------

ROUTES = {
    "POST /public/phone-agent/telephony/webhook": telephony_webhook,
    "POST /public/phone-agent/voice/webhook": voice_webhook,
}


def handler(event, context=None):
    method, path = http.route(event)
    try:
        if event.get("routeKey") in (None, "$default"):  # the Function URL: route by path
            path = event.get("rawPath") or ""
            if path.rstrip("/") in ("", "/v1", "/chat/completions", "/v1/chat/completions"):
                return turn_endpoint(event) if method == "POST" else _not_found()
        fn = ROUTES.get(f"{method} {path}")
        if fn is None:
            return _not_found()
        return fn(event)
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"route": path, "aws": type(exc).__name__}))
        return http.respond(503, {"error": "service_unavailable", "code": "service_unavailable"})
