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


def turn_log(result, *, total_ms, lang, previous, decision, held=0):
    """One log line per agent turn (EMF): the metrics TurnLatencyMs and
    TurnRounds as before, and as plain fields (no new metric, no cost) the
    measured model, tool and total time, prompt cache tokens and the
    language decision. Never any transcript text."""
    print(json.dumps({
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
            "Namespace": NAMESPACE, "Dimensions": [[]],
            "Metrics": [{"Name": "TurnLatencyMs", "Unit": "Milliseconds"}, {"Name": "TurnRounds", "Unit": "Count"}]}]},
        "event": "phoneTurn", "TurnLatencyMs": result.latency_ms, "TurnRounds": result.rounds,
        "modelMs": result.model_ms, "toolMs": result.tool_ms, "agentMs": result.latency_ms, "totalMs": int(total_ms),
        "rounds": result.rounds, "cacheReadTokens": result.cache_read, "cacheWriteTokens": result.cache_write,
        "cut": bool(result.cut), "error": bool(result.error), "tools": len(result.tool_calls), "held": int(held),
        "lang": lang, "from": previous, "switched": lang != previous, "reason": decision.reason,
        "ask": decision.ask, "unsupported": decision.unsupported}))


def local_now(location) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo((location or {}).get("timezone") or "Europe/Stockholm"))


def local_today(location) -> date:
    return local_now(location).date()


def now_clock(location):
    """"HH:MM" local now, for the day local_today() answers (None when they differ, as in tests)."""
    now = local_now(location)
    return now.strftime("%H:%M") if now.date() == local_today(location) else None


def allowed_languages(config, call=None):
    """The languages a call may switch to (owner 2026-11-13 and 2026-11-17:
    whatever the caller speaks): on the Amazon Connect line (callRef
    connect-...) the AWS list (a Lex locale and a Polly voice each), else
    every language the configured ElevenLabs voice model speaks. CONFIG
    `languages` is not a filter yet: the admin has no language picker, so
    every saved config lists the full list of the day it was saved, and
    honouring it would shut out languages added since."""
    return language.call_languages((call or {}).get("callRef"))


def language_locked(call) -> bool:
    """A call that used its language switches (language.MAX_SWITCHES, owner
    2026-11-20: at most three per call) stays in its language."""
    return int((call or {}).get("languageSwitches") or 0) >= language.MAX_SWITCHES


def proper_names(tenant_row, location, restaurant=None):
    """The call's proper names (restaurant, location, company): never a sign of the caller's language."""
    return [x for x in (restaurant, (location or {}).get("name"), (tenant_row or {}).get("name"),
                        (tenant_row or {}).get("displayName")) if isinstance(x, str) and x.strip()]


def _since(state):
    """Caller turns since the call's last language switch, else None."""
    since = state.get("langSince")
    return None if since is None else int(state.get("turn") or 0) - int(since)


def _read_back_again(state, at_turn):
    """The caller switched language: whatever was read back before counts as
    looked up in `at_turn`, so create, move or cancel needs a read-back in
    the new language and a clear yes in a later utterance (the same rule as
    executor.not_heard; the proposals keep their day, time and party)."""
    for proposal in (state.get("proposals") or {}).values():
        if isinstance(proposal, dict) and proposal.get("turn") is not None and int(proposal["turn"]) < at_turn:
            proposal["turn"] = at_turn
    found = state.get("foundTurn") or {}
    for rid, seen in list(found.items()):
        if seen is not None and int(seen) < at_turn:
            found[rid] = at_turn


def _turn_setup(lang, caller_text, state, disclosed, allowed, *, names=(), switches=0):
    """(language the prompt asks for, the fast hint, whether the model must
    say the disclosure sentence itself) before the model runs. The hint sets
    the prompt's language only when it already is strong evidence
    (language.pre_switch)."""
    hint = language.hint(language.without_names(caller_text, names))
    prompt_lang = language.pre_switch(lang, hint, caller_text, allowed=allowed, names=names,
                                      turns_since_switch=_since(state), switches=switches)
    disclose_now = bool(state.pop("discloseNext", None)) or (
        prompt_lang not in disclosed and copy.disclosure_short(prompt_lang) is None)
    return prompt_lang, hint, disclose_now


def _after_turn(lang, hint, result, caller_text, state, allowed, locked=False, *, names=(), prompt_lang=None,
                switches=0):
    """The language decision (language.Decision; it unpacks to (language,
    unsupported)). A locked call (language_locked) or a failed turn keeps
    its language whatever was said."""
    decided = language.decision(lang, hint, result.language, caller_text, allowed=allowed,
                                previous_tag=state.get("lastTag"), names=names, turns_since_switch=_since(state),
                                switches=switches, max_switches=language.MAX_SWITCHES,
                                asked_switch=state.get("askedSwitch"), prompt_lang=prompt_lang)
    state["lastTag"] = result.language
    if result.error:
        return language.Decision(lang, None, "error")
    if locked:
        return language.Decision(lang, None, "capped")
    return decided


def _after_cut(result, state, disclose_now):
    """A turn cut by the deadline: the disclosure sentence the model was to say is
    said in the next turn (the resume flag is set by the executor)."""
    if result.cut:
        metric("TurnCut", 1)
        if disclose_now:
            state["discloseNext"] = True


def _unsupported_reply(executor, state, code, messages, connect=False, current="sv"):
    """A language the line cannot speak: the fixed sentences in the caller's
    language (when the call's voice can read it) and in English, asking one
    question: whether to continue in English (state unsupportedStep); never a
    silent Swedish answer. `current` is the language of the voice that speaks
    it (on the Connect line the call switches to English first, so the
    English voice says the English sentence). When the caller again speaks
    the language (not a yes or a no), Booqy asks the restaurant to call back
    (topic language:<code>), says so and ends."""
    again = state.get("unsupported") == code
    state["unsupported"] = code
    if again and not state.get("callbackId"):
        executor.request_callback({"topic": f"language:{code}"})
    reply = copy.unsupported_language(code, callback=again, connect=connect, current=current)
    if again:
        state.pop("unsupportedStep", None)
        state.setdefault("providerActions", []).append({"name": "end_call", "arguments": {"reason": "callback"}})
    else:
        state["unsupportedStep"] = "english"
    agent.replace_last_reply(messages, reply)
    return reply


def _unsupported_answer(executor, state, answer, messages, connect=False, current="sv"):
    """The caller's yes or no (language.yes_no, any language) to the one
    question Booqy asked after a language the line cannot speak, answered
    without the model (live test 2026-11-20: "بله" to a three-way offer was
    taken as the callback). Continue in English? yes: English; no: may the
    restaurant call back? yes: the callback, confirmed (in English, and in
    the caller's language when the voice reads it), and the end; no:
    English. Returns (reply, the language to continue in or None)."""
    code = state.get("unsupported")
    step = state.pop("unsupportedStep", None)
    target = None
    if step == "english" and answer == "yes":
        state.pop("unsupported", None)
        reply, target = copy.UNSUPPORTED_CONTINUE["en"], "en"
    elif step == "english":
        state["unsupportedStep"] = "callback"
        reply = copy.unsupported_language(code, connect=connect, current=current, ask_callback=True)
    elif answer == "yes":
        if not state.get("callbackId"):
            executor.request_callback({"topic": f"language:{code}"})
        reply = copy.unsupported_language(code, callback=True, connect=connect, current=current)
        state.setdefault("providerActions", []).append({"name": "end_call", "arguments": {"reason": "callback"}})
    else:
        state.pop("unsupported", None)
        reply, target = copy.UNSUPPORTED_NO_CALLBACK["en"], "en"
    messages.append({"role": "assistant", "content": reply})
    return reply, target


def call_turn(call, caller_text, *, model, invoke, voice, tenant_row, location, loc, config, at, add_callback,
              send_sms=None, asr_locale=None, held=0):
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
    that needs a card gets a callback). `asr_locale` is the recognizer's
    locale of the caller's turn (the Connect line). A switch is counted
    (`languageSwitches`); after language.MAX_SWITCHES the call keeps its
    language. The switch rules are language.decision's; a reply in a language
    the call did not switch to is replaced by Booqy's short question whether
    to continue in it (copy.ask_switch). On a switch whatever was read back
    before must be read back again in the new language (_read_back_again).
    `held`: fragments the hesitation guard joined into `caller_text` (log).
    Returns (result, updated_call, state, actions)."""
    started = time.monotonic()
    current = calls.append_turn(call, "caller", caller_text, at, asr_locale=asr_locale)
    caller_seq = int((current or {}).get("turnCount") or 0)
    current = calls.get_call(call["tenantId"], call["callId"]) or call
    lang = current.get("language") or "sv"
    actions, spoken = [], []
    state = json.loads(current.get("toolState") or "{}")
    state["turn"] = int(state.get("turn") or 0) + 1       # tools.Executor: a write needs an earlier read-back
    locked = language_locked(current)
    switches = int(current.get("languageSwitches") or 0)
    allowed = (lang,) if locked else allowed_languages(config, current)
    connect = language.is_connect(current.get("callRef"))
    disclosed = list(pa.plain(current.get("disclosure") or {}).get("languages") or [lang])
    restaurant = locations_mod.public_name(tenant_row, location)
    names = proper_names(tenant_row, location, restaurant)
    prompt_lang, hint, disclose_now = _turn_setup(lang, caller_text, state, disclosed, allowed, names=names,
                                                  switches=switches)
    pre_switched = prompt_lang != lang
    if pre_switched:
        _read_back_again(state, state["turn"])        # the read-back is said again in this reply
    switch_note = pre_switched or bool(state.pop("switchNote", None))
    messages = agent.loads(current.get("agentMessages"))
    executor = Executor(tenant_id=call["tenantId"], tenant_row=tenant_row, location=location, loc_config=loc,
                        invoke=invoke, state=state, caller_phone=call.get("from"), voice=voice,
                        call_ref=call.get("callRef"), callbacks=add_callback, today=local_today(location),
                        now_time=now_clock(location), language=prompt_lang, send_sms=send_sms)
    system = agent.system_prompt(prompt_lang, config.get("agentName"), restaurant, executor.today,
                                 config.get("tone"), last_day=executor.last_day, disclose_now=disclose_now,
                                 phone_known=executor.caller_known, unsupported=state.get("unsupported"),
                                 resume=bool(state.pop("resume", None)), switched=switch_note,
                                 asked=state.get("askedSwitch"), locked=locked)
    answer = language.yes_no(caller_text) if state.get("unsupportedStep") else None
    if answer:      # the yes or no to Booqy's own unsupported-language question: no model turn
        messages.append({"role": "user", "content": caller_text})
        reply, target = _unsupported_answer(executor, state, answer, messages, connect=connect, current=lang)
        result = agent.TurnResult(reply, language=lang)
        decided = language.Decision(target, None, "asked") if target and target != lang and target in allowed \
            else language.Decision(lang, None, "unsupportedAnswer")
    else:
        result = agent.run_turn(model, messages, caller_text, executor, system=system, lang=prompt_lang,
                                keep_preamble=disclose_now)
        _after_cut(result, state, disclose_now)
        decided = _after_turn(lang, hint, result, caller_text, state, allowed, locked, names=names,
                              prompt_lang=prompt_lang, switches=switches)
    new_lang, unsupported = decided
    wrote = any(c["ok"] and c["name"] in agent.WRITES for c in result.tool_calls)
    asking = None
    if not unsupported and new_lang == lang and decided.ask and not (wrote or result.cut or result.error):
        if decided.ask in allowed:
            asking = decided.ask        # the reply is in a language the call did not switch to: ask instead
            result.reply = copy.ask_switch(lang, asking, connect=connect, candidate_name=language.name(asking))
            agent.replace_last_reply(messages, result.reply)
        else:
            unsupported = decided.ask
    if asking:
        state["askedSwitch"] = asking
        metric("LanguageAsk", 1)
    else:
        state.pop("askedSwitch", None)
    if unsupported:
        # On the Connect line each language has its own voice: the call switches to English first, so the
        # English sentence is said by the English voice (live test 2026-11-20: Elin read it).
        english = (state.get("unsupported") != unsupported and connect and lang != "en" and "en" in allowed
                   and not locked)
        result.reply = _unsupported_reply(executor, state, unsupported, messages, connect=connect,
                                          current="en" if english else lang)
        new_lang = "en" if english else lang
    if (answer or unsupported or asking) and not result.cut:
        executor.heard(result.reply, new_lang)       # the caller heard Booqy's sentence, not the model's reply
    if new_lang != lang:
        state["langSince"] = state["turn"]
        if not pre_switched or prompt_lang != new_lang:
            _read_back_again(state, state["turn"] + 1)     # this reply may not have read it back in new_lang
            state["switchNote"] = True
        if caller_seq and not (answer or unsupported):
            calls.set_turn_language(current, caller_seq, new_lang)   # the caller spoke the new language
        current, said = calls.repeat_disclosure(current, new_lang, at, new_language=True)
        if said is None and not (disclose_now and prompt_lang == new_lang):
            state["discloseNext"] = True    # the model says it in the next turn
        current = calls.set_status(current, current["status"], [current["status"]], language=new_lang,
                                   languageSwitches=int(current.get("languageSwitches") or 0) + 1) or current
        actions.append({"name": "language_detection", "arguments": {
            "reason": f"The caller speaks {language.name(new_lang)}", "language": new_lang}})
        if said:
            spoken.append(said)
    state["counterOffer"] = executor.counter_offer()
    actions.extend(state.pop("providerActions", None) or [])
    for tool in result.tool_calls:
        calls.append_turn(call, "tool", tool["name"], at, tool_name=tool["name"], tool_ok=tool["ok"])
    calls.append_turn(call, "agent", result.reply, at, lang=new_lang)
    result.reply = " ".join(spoken + [result.reply]).strip()
    current = calls.get_call(call["tenantId"], call["callId"])
    fields = {"agentMessages": agent.dumps(messages), "toolState": json.dumps(state, default=str),
              "counterOffer": state["counterOffer"]}
    transfer = state.get("transfer")
    if transfer:
        fields["transfer"] = {**transfer, "at": pa.iso(at)}
    updated = calls.set_status(current, "talking", ["answered", "disclosed", "talking", "transferring"], **fields)
    turn_log(result, total_ms=(time.monotonic() - started) * 1000, lang=new_lang,
             previous=lang, decision=decided, held=held)
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
    names = proper_names(tenant_row, location, locations_mod.public_name(tenant_row, location))
    if language_:
        prompt_lang, hint, disclose_now = language_, language_, False
    else:
        prompt_lang, hint, disclose_now = _turn_setup(lang, text, state, disclosed, allowed, names=names)
    executor = Executor(tenant_id=tenant_id, tenant_row=tenant_row, location=location, loc_config=loc,
                        invoke=invoke, state=state, dry_run=True, today=local_today(location),
                        now_time=now_clock(location), language=prompt_lang)
    restaurant = locations_mod.public_name(tenant_row, location)
    system = agent.system_prompt(prompt_lang, config.get("agentName"), restaurant, executor.today,
                                 config.get("tone"), last_day=executor.last_day, disclose_now=disclose_now,
                                 unsupported=state.get("unsupported"), resume=bool(state.pop("resume", None)))
    result = agent.run_turn(model, messages, text, executor, system=system, lang=prompt_lang,
                            keep_preamble=disclose_now)
    _after_cut(result, state, disclose_now)
    switched = None
    if not language_:
        lang, unsupported = _after_turn(lang, hint, result, text, state, allowed, names=names, prompt_lang=prompt_lang)
        if lang != start_lang and "langSince" not in state:
            state["langSince"] = state["turn"]
        if unsupported:
            result.reply = _unsupported_reply(executor, state, unsupported, messages)
            executor.heard(result.reply, lang)
            state.pop("providerActions", None)
        elif lang not in disclosed:
            disclosed.append(lang)
            switched = copy.disclosure_short(lang)
    state["disclosed"] = disclosed
    reply = result.reply
    if first:  # the simulator shows the greeting with the disclosure first, like a call
        greeting = copy.greeting(start_lang, config.get("agentName"), restaurant,
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
