"""One agent turn for a live call (phone-agent-call, the turn endpoint) or
for the wizard's text simulator (phone-agent L6, dry run), on the same
agent loop and tools. The scripted call simulator in the tests drives
these functions text-in/text-out (ADR-L test plan)."""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

from shared.features import phone_agent as pa
from shared.features.phone_agent import agent, calls, copy
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


def call_turn(call, caller_text, *, model, invoke, voice, tenant_row, location, loc, config, at, add_callback):
    """Runs the caller's utterance through the agent loop and persists the
    conversation, the tool state and the transcript turns on the call."""
    lang = call.get("language") or "sv"
    messages = agent.loads(call.get("agentMessages"))
    state = json.loads(call.get("toolState") or "{}")
    executor = Executor(tenant_id=call["tenantId"], tenant_row=tenant_row, location=location, loc_config=loc,
                        invoke=invoke, state=state, caller_phone=call.get("from"), voice=voice,
                        call_ref=call.get("callRef"), callbacks=add_callback, today=local_today(location))
    system = agent.system_prompt(lang, config.get("agentName"), location.get("name"), executor.today)
    result = agent.run_turn(model, messages, caller_text, executor, system=system)
    state["counterOffer"] = executor.counter_offer()
    metric("TurnLatencyMs", result.latency_ms, "Milliseconds")
    calls.append_turn(call, "caller", caller_text, at)
    for tool in result.tool_calls:
        calls.append_turn(call, "tool", tool["name"], at, tool_name=tool["name"], tool_ok=tool["ok"])
    calls.append_turn(call, "agent", result.reply, at)
    current = calls.get_call(call["tenantId"], call["callId"])
    fields = {"agentMessages": agent.dumps(messages), "toolState": json.dumps(state, default=str),
              "counterOffer": state["counterOffer"]}
    transfer = state.get("transfer")
    if transfer:
        fields["transfer"] = {**transfer, "at": pa.iso(at)}
    updated = calls.set_status(current, "talking", ["answered", "disclosed", "talking", "transferring"], **fields)
    return result, updated or current, state


def sim_turn(*, tenant_id, tenant_row, location, loc, config, session_id, text, language, model, invoke):
    """Route L6: text in, text out, nothing written to bookings (dry run).
    The same sessionId and text again answers the stored reply."""
    key = pa.sim_key(tenant_id, session_id)
    row = pa.table().get_item(Key=key, ConsistentRead=True).get("Item") or {}
    if row and row.get("tenantId") != tenant_id:
        raise pa.PhoneAgentError(404, "not found")
    if row.get("lastText") == text and row.get("lastReply") is not None:
        return json.loads(row["lastReply"])
    lang = language or row.get("language") or "sv"
    messages = agent.loads(row.get("messages"))
    state = json.loads(row.get("toolState") or "{}")
    first = not messages
    executor = Executor(tenant_id=tenant_id, tenant_row=tenant_row, location=location, loc_config=loc,
                        invoke=invoke, state=state, dry_run=True, today=local_today(location))
    system = agent.system_prompt(lang, config.get("agentName"), location.get("name"), executor.today)
    result = agent.run_turn(model, messages, text, executor, system=system)
    reply = result.reply
    if first:  # the simulator shows the greeting with the disclosure first, like a call
        greeting = copy.greeting(lang, config.get("agentName"), location.get("name"),
                                 loc.get("greetingExtraSv" if lang == "sv" else "greetingExtraEn"),
                                 recording=bool((config.get("recording") or {}).get("enabled")))
        reply = f"{greeting} {reply}".strip()
    out = {"reply": reply, "toolCalls": result.tool_calls, "state": {
        "outcome": state.get("outcome"), "counterOffer": executor.counter_offer(), "turns": len(messages)},
        "disclosure": first}
    item = {**key, "tenantId": tenant_id, "language": lang, "messages": agent.dumps(messages),
            "toolState": json.dumps(state, default=str), "lastText": text, "lastReply": json.dumps(out),
            "ttl": int((datetime.now() + timedelta(seconds=pa.SIM_TTL_SECONDS)).timestamp())}
    pa.table().put_item(Item=item)
    return out
