"""One agent turn for a live call (phone-agent-call, the turn endpoint) or
for the wizard's text simulator (phone-agent L6, dry run), on the same
agent loop and tools. The scripted call simulator in the tests drives
these functions text-in/text-out (ADR-L test plan)."""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

from botocore.exceptions import ClientError

from shared.features import phone_agent as pa
from shared.features.phone_agent import agent, calls, copy, language
from shared.features.phone_agent import locations as locations_mod
from shared.features.phone_agent.tools import Executor

NAMESPACE = "Booqy/PhoneAgent"


def metric(name, value, unit="Count", **dims):
    """CloudWatch Embedded Metric Format (a log line; no PutMetricData)."""
    print(json.dumps({"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
        "Namespace": NAMESPACE, "Dimensions": [list(dims)] if dims else [[]],
        "Metrics": [{"Name": name, "Unit": unit}]}]}, name: value, **dims}))


def local_now(location) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo((location or {}).get("timezone") or "Europe/Stockholm"))


def local_today(location) -> date:
    return local_now(location).date()


def now_clock(location):
    """"HH:MM" local now, for the day local_today() answers (None when they differ, as in tests)."""
    now = local_now(location)
    return now.strftime("%H:%M") if now.date() == local_today(location) else None


def allowed_languages(config):
    """The languages a call may switch to: every language the configured
    voice model speaks (owner 2026-11-13 and 2026-11-17: whatever the caller
    speaks). CONFIG `languages` is not a filter yet: the admin has no
    language picker, so every saved config lists the full list of the day it
    was saved, and honouring it would shut out languages added since."""
    return language.voice_languages()


def _turn_setup(lang, caller_text, state, disclosed, allowed):
    """(language the prompt asks for, the fast hint, whether the model must
    say the AI sentence itself) before the model runs."""
    hint = language.hint(caller_text)
    prompt_lang = hint if hint in allowed else lang
    disclose_now = bool(state.pop("discloseNext", None)) or (
        prompt_lang not in disclosed and copy.disclosure_short(prompt_lang) is None)
    return prompt_lang, hint, disclose_now


def _after_turn(lang, hint, result, caller_text, state, allowed):
    """(the call's language now, a language the voice cannot speak or None)."""
    new_lang, unsupported = language.decide(lang, hint, result.language, caller_text, allowed=allowed,
                                            previous_tag=state.get("lastTag"))
    state["lastTag"] = result.language
    if result.error:
        return lang, None
    return new_lang, unsupported


def _after_cut(result, state, disclose_now):
    """A turn cut by the deadline: the AI sentence the model was to say is
    said in the next turn (the resume flag is set by the executor)."""
    if result.cut:
        metric("TurnCut", 1)
        if disclose_now:
            state["discloseNext"] = True


def _unsupported_reply(executor, state, code, messages):
    """A language the voice cannot speak: the fixed sentences in Swedish,
    English and Arabic; the second time the caller still cannot continue,
    Booqy asks the restaurant to call back (topic language:<code>) and ends."""
    again = state.get("unsupported") == code
    state["unsupported"] = code
    if again and not state.get("callbackId"):
        executor.request_callback({"topic": f"language:{code}"})
    reply = copy.unsupported_language(callback=again)
    if again:
        state.setdefault("providerActions", []).append({"name": "end_call", "arguments": {"reason": "callback"}})
    agent.replace_last_reply(messages, reply)
    return reply


def call_turn(call, caller_text, *, model, invoke, voice, tenant_row, location, loc, config, at, add_callback,
              send_sms=None):
    """Runs the caller's utterance through the agent loop and persists the
    conversation, the tool state and the transcript turns on the call.

    The language: the fast hint sets the prompt's language before the model
    runs; the model's `[xx]` tag decides after it (language.decide). On a
    switch the short art. 50 sentence is said once in the new language
    before the reply (by Booqy from copy.DISCLOSURE_SHORT, else by the model
    when the prompt asked for it), and the provider is told to switch
    (`actions`: its language_detection system tool). A language the voice
    cannot speak gets copy.UNSUPPORTED_LANGUAGE instead of the reply.
    `send_sms` texts the caller (the card guarantee link; None: a party
    that needs a card gets a callback). Returns (result, updated_call,
    state, actions)."""
    calls.append_turn(call, "caller", caller_text, at)
    current = calls.get_call(call["tenantId"], call["callId"]) or call
    lang = current.get("language") or "sv"
    actions, spoken = [], []
    state = json.loads(current.get("toolState") or "{}")
    state["turn"] = int(state.get("turn") or 0) + 1       # tools.Executor: a write needs an earlier read-back
    allowed = allowed_languages(config)
    disclosed = list(pa.plain(current.get("disclosure") or {}).get("languages") or [lang])
    prompt_lang, hint, disclose_now = _turn_setup(lang, caller_text, state, disclosed, allowed)
    messages = agent.loads(current.get("agentMessages"))
    executor = Executor(tenant_id=call["tenantId"], tenant_row=tenant_row, location=location, loc_config=loc,
                        invoke=invoke, state=state, caller_phone=call.get("from"), voice=voice,
                        call_ref=call.get("callRef"), callbacks=add_callback, today=local_today(location),
                        now_time=now_clock(location), language=prompt_lang, send_sms=send_sms)
    system = agent.system_prompt(prompt_lang, config.get("agentName"), location.get("name"), executor.today,
                                 config.get("tone"), last_day=executor.last_day, disclose_now=disclose_now,
                                 phone_known=executor.caller_known, unsupported=state.get("unsupported"),
                                 resume=bool(state.pop("resume", None)))
    result = agent.run_turn(model, messages, caller_text, executor, system=system, lang=prompt_lang,
                            keep_preamble=disclose_now)
    _after_cut(result, state, disclose_now)
    new_lang, unsupported = _after_turn(lang, hint, result, caller_text, state, allowed)
    if unsupported:
        result.reply = _unsupported_reply(executor, state, unsupported, messages)
    elif new_lang != lang:
        current, said = calls.repeat_disclosure(current, new_lang, at, new_language=True)
        if said is None and not (disclose_now and prompt_lang == new_lang):
            state["discloseNext"] = True    # the model says it in the next turn
        current = calls.set_status(current, current["status"], [current["status"]], language=new_lang) or current
        actions.append({"name": "language_detection", "arguments": {
            "reason": f"The caller speaks {language.name(new_lang)}", "language": new_lang}})
        if said:
            spoken.append(said)
    state["counterOffer"] = executor.counter_offer()
    actions.extend(state.pop("providerActions", None) or [])
    metric("TurnLatencyMs", result.latency_ms, "Milliseconds")
    metric("TurnRounds", result.rounds)
    for tool in result.tool_calls:
        calls.append_turn(call, "tool", tool["name"], at, tool_name=tool["name"], tool_ok=tool["ok"])
    calls.append_turn(call, "agent", result.reply, at)
    result.reply = " ".join(spoken + [result.reply]).strip()
    current = calls.get_call(call["tenantId"], call["callId"])
    fields = {"agentMessages": agent.dumps(messages), "toolState": json.dumps(state, default=str),
              "counterOffer": state["counterOffer"]}
    transfer = state.get("transfer")
    if transfer:
        fields["transfer"] = {**transfer, "at": pa.iso(at)}
    updated = calls.set_status(current, "talking", ["answered", "disclosed", "talking", "transferring"], **fields)
    return result, updated or current, state, actions


def choose_location(call, caller_text, *, locations, at, lang):
    """A call on a shared number before the caller named the location: the
    location they named, or the question again. Returns (call, reply|None):
    a reply means the caller still has to say which; None means the location
    is set and the agent loop takes the same utterance."""
    chosen = pick_location(caller_text, locations)
    if not chosen:
        calls.append_turn(call, "caller", caller_text, at)
        reply = copy.which_location(lang, [locations_mod.label(x) for x in locations], again=True)
        calls.append_turn(call, "agent", reply, at)
        return calls.get_call(call["tenantId"], call["callId"]) or call, reply
    current = calls.get_call(call["tenantId"], call["callId"]) or call
    updated = calls.set_status(current, current["status"], [current["status"]], locationId=chosen["locationId"],
                               locationPending=False, restaurant=str(chosen.get("name") or "")[:120])
    current = updated or calls.get_call(call["tenantId"], call["callId"]) or current
    if len(locations_mod.rest_of(caller_text, chosen, locations)) < 4:   # just the name: ask what they want
        calls.append_turn(current, "caller", caller_text, at)
        reply = copy.location_chosen(lang, locations_mod.label(chosen))
        calls.append_turn(current, "agent", reply, at)
        return calls.get_call(call["tenantId"], call["callId"]) or current, reply
    return current, None


def pick_location(text, locations):
    found = locations_mod.pick(text, locations)
    return next((x for x in locations if x["locationId"] == found), None) if found else None


def sim_turn(*, tenant_id, tenant_row, location, loc, config, session_id, text, language_=None, model, invoke):
    """Route L6: text in, text out, nothing written to bookings (dry run).
    The same sessionId and text again answers the stored reply."""
    key = pa.sim_key(tenant_id, session_id)
    row = pa.table().get_item(Key=key, ConsistentRead=True).get("Item") or {}
    if row and row.get("tenantId") != tenant_id:
        raise pa.PhoneAgentError(404, "not found")
    if row.get("lastText") == text and row.get("lastReply") is not None:
        return json.loads(row["lastReply"])
    turns = int(row.get("turns") or 0)
    if turns >= pa.SIM_TURNS_MAX:   # the UI starts a new sessionId (audit #61)
        raise pa.PhoneAgentError(409, "session_full")
    _count_sim_turn(tenant_id)
    lang = start_lang = language_ or row.get("language") or "sv"
    messages = agent.loads(row.get("messages"))
    state = json.loads(row.get("toolState") or "{}")
    state["turn"] = int(state.get("turn") or 0) + 1
    first = not messages
    allowed = allowed_languages(config)
    disclosed = list(state.get("disclosed") or [start_lang])
    if language_:
        prompt_lang, hint, disclose_now = language_, language_, False
    else:
        prompt_lang, hint, disclose_now = _turn_setup(lang, text, state, disclosed, allowed)
    executor = Executor(tenant_id=tenant_id, tenant_row=tenant_row, location=location, loc_config=loc,
                        invoke=invoke, state=state, dry_run=True, today=local_today(location),
                        now_time=now_clock(location), language=prompt_lang)
    system = agent.system_prompt(prompt_lang, config.get("agentName"), location.get("name"), executor.today,
                                 config.get("tone"), last_day=executor.last_day, disclose_now=disclose_now,
                                 unsupported=state.get("unsupported"), resume=bool(state.pop("resume", None)))
    result = agent.run_turn(model, messages, text, executor, system=system, lang=prompt_lang,
                            keep_preamble=disclose_now)
    _after_cut(result, state, disclose_now)
    switched = None
    if not language_:
        lang, unsupported = _after_turn(lang, hint, result, text, state, allowed)
        if unsupported:
            result.reply = _unsupported_reply(executor, state, unsupported, messages)
            state.pop("providerActions", None)
        elif lang not in disclosed:
            disclosed.append(lang)
            switched = copy.disclosure_short(lang)
    state["disclosed"] = disclosed
    reply = result.reply
    if first:  # the simulator shows the greeting with the disclosure first, like a call
        greeting = copy.greeting(start_lang, config.get("agentName"), location.get("name"),
                                 loc.get("greetingExtraSv" if start_lang == "sv" else "greetingExtraEn"),
                                 recording=bool((config.get("recording") or {}).get("enabled")))
        reply = " ".join(x for x in (greeting, switched, reply) if x)
    elif switched:   # the short disclosure once in the caller's new language, like a call
        reply = f"{switched} {reply}".strip()
    out = {"reply": reply, "language": lang, "toolCalls": result.tool_calls, "state": {
        "outcome": state.get("outcome"), "counterOffer": executor.counter_offer(), "turns": len(messages)},
        "disclosure": first}
    item = {**key, "tenantId": tenant_id, "language": lang, "messages": agent.dumps(messages),
            "toolState": json.dumps(state, default=str), "lastText": text, "lastReply": json.dumps(out),
            "turns": turns + 1,
            "ttl": int((datetime.now() + timedelta(seconds=pa.SIM_TTL_SECONDS)).timestamp())}
    pa.table().put_item(Item=item)
    return out


def _count_sim_turn(tenant_id):
    """One model turn of the simulator against the tenant's monthly quota
    (a conditional ADD on SIMUSE#<yyyy-mm>, like meta-ads COPYUSE)."""
    at = pa.now()
    month = at.strftime("%Y-%m")
    try:
        pa.table().update_item(
            Key=pa.simuse_key(tenant_id, month),
            UpdateExpression="ADD #c :one SET #ttl = if_not_exists(#ttl, :ttl), tenantId = :t",
            ConditionExpression="attribute_not_exists(#c) OR #c < :max",
            ExpressionAttributeNames={"#c": "count", "#ttl": "ttl"},
            ExpressionAttributeValues={":one": 1, ":max": pa.SIM_QUOTA_PER_MONTH, ":t": tenant_id,
                                       ":ttl": int((at + timedelta(days=pa.SIMUSE_TTL_DAYS)).timestamp())})
    except ClientError as exc:
        if pa.conditional_failed(exc):
            raise pa.PhoneAgentError(429, "quota_exceeded")
        raise
