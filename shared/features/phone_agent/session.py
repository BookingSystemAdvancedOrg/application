"""One agent turn for a live call (phone-agent-call, the turn endpoint) or
for the wizard's text simulator (phone-agent L6, dry run), on the same
agent loop and tools. The scripted call simulator in the tests drives
these functions text-in/text-out (ADR-L test plan)."""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

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


def local_today(location) -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo((location or {}).get("timezone") or "Europe/Stockholm")).date()


def allowed_languages(config):
    """The languages a call may switch to: the tenant's list, Swedish always."""
    chosen = set(config.get("languages") or pa.LANGUAGES) | {"sv"}
    return tuple(x for x in pa.LANGUAGES if x in chosen)


def call_turn(call, caller_text, *, model, invoke, voice, tenant_row, location, loc, config, at, add_callback):
    """Runs the caller's utterance through the agent loop and persists the
    conversation, the tool state and the transcript turns on the call.

    The caller's language is decided first (language.next_language): on a
    switch the short art. 50 disclosure is said once in the new language
    before the reply, the system prompt asks for that language and the
    provider is told to switch (`actions`: its language_detection system
    tool). Returns (result, updated_call, state, actions)."""
    calls.append_turn(call, "caller", caller_text, at)
    current = calls.get_call(call["tenantId"], call["callId"]) or call
    lang = current.get("language") or "sv"
    actions, spoken = [], []
    new_lang = language.next_language(lang, caller_text, allowed_languages(config))
    if new_lang != lang:
        current, said = calls.repeat_disclosure(current, new_lang, at, new_language=True)
        current = calls.set_status(current, current["status"], [current["status"]], language=new_lang) or current
        lang = new_lang
        actions.append({"name": "language_detection", "arguments": {
            "reason": f"The caller speaks {language.NAMES[lang]}", "language": lang}})
        if said:
            spoken.append(said)
    messages = agent.loads(current.get("agentMessages"))
    state = json.loads(current.get("toolState") or "{}")
    executor = Executor(tenant_id=call["tenantId"], tenant_row=tenant_row, location=location, loc_config=loc,
                        invoke=invoke, state=state, caller_phone=call.get("from"), voice=voice,
                        call_ref=call.get("callRef"), callbacks=add_callback, today=local_today(location),
                        language=lang)
    system = agent.system_prompt(lang, config.get("agentName"), location.get("name"), executor.today,
                                 config.get("tone"))
    result = agent.run_turn(model, messages, caller_text, executor, system=system)
    state["counterOffer"] = executor.counter_offer()
    actions.extend(state.pop("providerActions", None) or [])
    metric("TurnLatencyMs", result.latency_ms, "Milliseconds")
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
    lang = start_lang = language_ or row.get("language") or "sv"
    switched = None
    if not language_:
        new_lang = language.next_language(lang, text, allowed_languages(config))
        if new_lang != lang:
            switched = copy.disclosure_short(new_lang, location.get("name"))
            lang = new_lang
    messages = agent.loads(row.get("messages"))
    state = json.loads(row.get("toolState") or "{}")
    first = not messages
    executor = Executor(tenant_id=tenant_id, tenant_row=tenant_row, location=location, loc_config=loc,
                        invoke=invoke, state=state, dry_run=True, today=local_today(location), language=lang)
    system = agent.system_prompt(lang, config.get("agentName"), location.get("name"), executor.today,
                                 config.get("tone"))
    result = agent.run_turn(model, messages, text, executor, system=system)
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
            "ttl": int((datetime.now() + timedelta(seconds=pa.SIM_TTL_SECONDS)).timestamp())}
    pa.table().put_item(Item=item)
    return out
