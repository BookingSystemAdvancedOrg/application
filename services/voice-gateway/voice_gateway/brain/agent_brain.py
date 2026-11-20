"""AgentBrain: one call's agent, the orchestrator's only door into
shared/features/phone_agent. Owner: builder 'brain'.

Per call: one StreamingAgentModel (its own sink), the call row in the
PhoneAgent table exactly as today (calls.start via app.start_call, the
same agentMessages, toolState, turns, disclosure evidence, outcome,
metering at the end via app.finish). Blocking work (DynamoDB, Lambda
tool invokes, Bedrock) runs in worker threads (asyncio.to_thread); the
event loop never blocks.

turn(): the body of app.run_call_turn with the per-call model:
  location pending -> session.choose_location (a reply means: speak it,
  done); max call length -> app.finish + copy.MAX_LENGTH + end_call; else
  session.call_turn(call, text, model=<this call's StreamingAgentModel>,
  invoke=app.invoker(), voice=GatewayVoice, tenant_row, location, loc,
  config, at, add_callback=app.add_callback_for(...), send_sms=app.sms_sender(),
  asr_locale=<Transcribe code of the stream>). A transfer is NOT finished
  here (run_call_turn finishes when the deferred provider says answered):
  the line transfers after the reply and transfer_result() reports it.
  While it runs, AgentSentence / ToolStarted / ToolFinished stream out; at
  the end the reconciliation (streaming.reconcile) and one AgentTurnDone
  with the actions, the outcome and the context the next caller turn
  needs (awaiting_confirmation: a proposal of this turn in toolState
  without a booked outcome; asked_question: the last sentence ends in "?"
  and is a yes/no, a number or an open question by simple rules).

Language: the call row's `language` is the authority. set_language()
applies a switch the listener decided acoustically BEFORE the turn
(calls.repeat_disclosure with new_language=True, calls.set_status language
+ languageSwitches + 1, as session.call_turn does) and returns the short
disclosure sentence to say first (None when the model must say it: the
toolState gets discloseNext, which session.call_turn turns into
disclose_now). A pending read-back heard in the old language no longer
counts: the same not-heard rule as below is applied to toolState
(MULTILINGUAL.md gap 9). A switch session.call_turn makes on its own shows
as AgentTurnDone.language.

Interruptions: mark_not_heard(heard) after a barge-in on an assistant
reply (or a reply dropped because the caller kept talking): the same
rule as tools.Executor.not_heard (read-backs of this turn count as not
heard, so create_reservation(confirmed=true) is refused until a new
read-back and a later clear yes), applied to the stored toolState by
calling Executor.not_heard on a minimal stand-in (state, turn); `resume`
is popped again (a barge-in is not a deadline cut); the last assistant
message becomes what the caller heard plus " ..." (agent.replace_last_reply).
greeting_interrupted(): calls.repeat_disclosure (the short sentence, said
before the next reply), as the voice webhook does today.

Non-streaming fallback (BrainConfig.stream_sentences False, or a model
that is not a StreamingAgentModel such as shared.agent_model's
simulator): the same turn(), sentences cut from the final reply only, no
tool events (they would come too late to cover anything).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import AsyncIterator, Callable, Literal

from shared import tenant as shared_tenant
from shared.agent_model import AgentModel
from shared.features import phone_agent as pa
from shared.features.phone_agent import agent as pa_agent
from shared.features.phone_agent import calls, copy, language as pa_language, session
from shared.features.phone_agent import locations as locations_mod
from shared.features.phone_agent.tools import Executor

from voice_gateway import languages, log
from voice_gateway.audio.adapter import CallInfo
from voice_gateway.brain.streaming import (StreamBridge, StreamingAgentModel, StreamSink, reconcile,
                                           split_sentences)
from voice_gateway.clock import Clock
from voice_gateway.config import GatewayConfig
from voice_gateway.events import AgentSentence, AgentTurnDone, EventBus, ProviderAction, ToolFinished, ToolStarted

FIXED = {"still_there": copy.STILL_THERE, "error": copy.ERROR, "transferring": copy.TRANSFERRING,
         "max_length": copy.MAX_LENGTH, "transfer_unanswered": copy.TRANSFER_UNANSWERED}
ACTIONS = ("end_call", "transfer_to_number", "language_detection")
SETTLED = ("booked", "moved", "cancelled", "transferred")


@dataclass(frozen=True)
class CallStart:
    call_id: str | None              # None: not a Booqy number (hang up, nothing said)
    tenant_id: str | None
    language: str                    # "sv" at start
    greeting: str | None             # the full greeting with the disclosure, Booqy wording
    refused: bool = False            # the greeting is the refusal sentence; hang up after it
    location_pending: bool = False   # shared number: the greeting asks which location
    names: tuple[str, ...] = ()      # restaurant and location names (LID proper noun strip, turn vocab)
    allowed: tuple[str, ...] = ()    # languages the call may switch to (session.allowed_languages)
    greeting_sentences: tuple[str, ...] = ()   # the greeting cut into sentences (played one by one)
    disclosure_until: int = -1       # index of the greeting sentence that completes the disclosure


def fixed_text(key: str, lang: str) -> tuple[str, str]:
    """(text, its language) of Booqy copy: `lang`, else English, else
    Swedish (copy.text), with the language it is actually written in so the
    native voice reads it."""
    table = FIXED[key]
    for code in (lang, "en", "sv"):
        if table.get(code):
            return table[code], code
    raise KeyError(key)


# --- simple question rules (AgentTurnDone.asked_question) ------------------------------------------

_NUMBER_Q = re.compile(r"\b(hur många|vilken tid|vilket klockslag|vilket nummer|telefonnummer|vilket datum|"
                       r"how many|what time|which time|what number|phone number|which date|wie viele|um wie viel)\b",
                       re.I)
_YES_NO_START = re.compile(r"^(stämmer|ska|vill|får|passar|är|har|kan|går|blir|önskar|behöver|"
                           r"is|shall|should|would|do|does|did|can|could|may|will|are|have|"
                           r"ist|soll|möchten|passt|haben|können)\b", re.I)
_YES_NO_ANY = re.compile(r"\b(stämmer det|går det bra|är det rätt|ska jag boka|is that (right|correct)|"
                         r"shall i book|okej\?|ok\?)", re.I)


def asked_question(reply: str) -> Literal["yes_no", "open", "number", None]:
    sentences = split_sentences(reply)
    last = sentences[-1] if sentences else ""
    if not last.endswith(("?", "？", "؟")):
        return None
    if _NUMBER_Q.search(last):
        return "number"
    if _YES_NO_START.match(last) or _YES_NO_ANY.search(last):
        return "yes_no"
    return "open"


def awaiting_confirmation(state: dict) -> bool:
    """A proposal read back in this turn (or carried to the next by not
    heard) that is not booked yet: the caller's next words may be the yes."""
    if state.get("outcome") in SETTLED:
        return False
    turn = int(state.get("turn") or 0)
    for proposal in (state.get("proposals") or {}).values():
        if proposal.get("reservationId") or proposal.get("turn") is None:
            continue
        if int(proposal["turn"]) >= turn:
            return True
    return False


def not_heard_state(state: dict) -> dict:
    """tools.Executor.not_heard applied to a stored toolState (the reply of
    the last turn was not heard): its read-backs count as not read back, a
    write needs a new read-back and a later clear yes. `resume` is popped
    again: a barge-in is not a deadline cut."""
    had_resume = "resume" in state
    Executor.not_heard(SimpleNamespace(state=state, turn=state.get("turn"), _live=lambda: (None, None)))
    if not had_resume:
        state.pop("resume", None)
    return state


class AgentBrain:
    def __init__(self, cfg: GatewayConfig, bus: EventBus, clock: Clock, *, app=None,
                 model_factory: Callable[[StreamSink], AgentModel] | None = None) -> None:
        """`app`: the loaded phone-agent-call module (lambda_app.load()), injectable.
        `model_factory(sink)`: the call's AgentModel; None builds a
        StreamingAgentModel on PHONE_AGENT_MODEL_ID. A model that is not a
        StreamingAgentModel (shared.agent_model's simulator, a scripted test
        model) runs the non-streaming path: sentences from the final reply."""
        self.cfg, self.bus, self.clock = cfg, bus, clock
        self._app = app
        self._model_factory = model_factory
        self.model: AgentModel | None = None
        self.bridge: StreamBridge | None = None
        self.tenant_id: str | None = None
        self.call_id: str | None = None
        self._language = cfg.language.start_language
        self._allowed: tuple[str, ...] = ()
        self._greeting: str | None = None
        self._turn = 0
        self._lock = asyncio.Lock()
        self.tag: str | None = None

    # -- helpers -----------------------------------------------------------------------------

    @property
    def app(self):
        if self._app is None:
            from voice_gateway.brain import lambda_app
            self._app = lambda_app.load()
        return self._app

    @property
    def streaming(self) -> bool:
        return isinstance(self.model, StreamingAgentModel) and self.bridge is not None

    def _call(self) -> dict:
        call = calls.get_call(self.tenant_id, self.call_id)
        if call is None:
            raise LookupError("call row missing")
        return call

    def _make_model(self) -> AgentModel:
        if self._model_factory is not None:
            return self._model_factory(self.bridge)
        if not self.cfg.brain.stream_sentences:
            return self.app.client("model")
        return StreamingAgentModel(self.bridge, self.cfg.brain,
                                   model_id=os.environ.get(self.cfg.brain.model_id_env) or None)

    # -- call start --------------------------------------------------------------------------

    async def start(self, info: CallInfo) -> CallStart:
        loop = asyncio.get_running_loop()
        self.bridge = StreamBridge(loop, self.bus, self.clock, self.cfg.brain, self.cfg.speak)
        self.model = self._make_model()
        call_ref = self.cfg.brain.call_ref_prefix + info.call_ref
        self.tag = log.call_tag(call_ref)
        call = await asyncio.to_thread(self.app.start_call, call_ref=call_ref, caller=info.caller, to=info.dialed,
                                       at=pa.now())
        if call is None:
            log.event("brain_start", call=self.tag, booqy_number=False)
            return CallStart(None, None, self._language, None)
        self.tenant_id, self.call_id = call["tenantId"], call["callId"]
        if call.get("outcome") == "refused" or call.get("status") not in calls.OPEN:
            log.event("brain_start", call=self.tag, refused=True)
            refusal = copy.REFUSED["sv"]
            return CallStart(self.call_id, self.tenant_id, "sv", refusal, refused=True,
                             greeting_sentences=tuple(split_sentences(refusal)))
        self._language = call.get("language") or "sv"
        self._greeting = (call.get("disclosure") or {}).get("text") or ""
        self._allowed = tuple(x for x in session.allowed_languages({}, call) if languages.can_speak(x))
        names = await asyncio.to_thread(self._names, call)
        sentences = tuple(split_sentences(self._greeting))
        log.event("brain_start", call=self.tag, location_pending=bool(call.get("locationPending")))
        return CallStart(self.call_id, self.tenant_id, self._language, self._greeting,
                         location_pending=bool(call.get("locationPending")), names=names, allowed=self._allowed,
                         greeting_sentences=sentences, disclosure_until=self._disclosure_index(sentences))

    def _names(self, call) -> tuple[str, ...]:
        names = [call.get("restaurant")]
        for location_id in call.get("candidateLocations") or [call.get("locationId")]:
            row = shared_tenant.get_location(call["tenantId"], location_id) or {}
            names.append(locations_mod.label({**row, "locationId": location_id}) if row else None)
        out = []
        for name in names:
            name = " ".join(str(name or "").split())
            if name and name not in out:
                out.append(name)
        return tuple(out)

    def _disclosure_index(self, sentences: tuple[str, ...]) -> int:
        """The greeting sentence that completes the art. 50 disclosure (its
        "digital assistant" sentence): a barge-in before it ends repeats the
        short disclosure; after it, the disclosure counts as heard."""
        ends = {pa_language_last(copy.DISCLOSURE.get(self._language) or ""),
                pa_language_last(copy.DISCLOSURE_NO_NAME.get(self._language) or "")}
        for i, sentence in enumerate(sentences):
            if calls.normalized_text(sentence) in ends:
                return i
        return len(sentences) - 1

    # -- the turn ----------------------------------------------------------------------------

    async def turn(self, text: str, *, language: str, asr_locale: str | None = None
                   ) -> AsyncIterator[AgentSentence | ToolStarted | ToolFinished | AgentTurnDone]:
        """Runs one caller turn; the iterator ends after AgentTurnDone. Every
        item is also published on the bus."""
        async with self._lock:
            self._turn += 1
            number = self._turn
            started = self.clock.now_ms()
            at = pa.now()
            call = await asyncio.to_thread(self._call)
            lang = call.get("language") or self._language
            disclosed = tuple(pa.plain(call.get("disclosure") or {}).get("languages") or [lang])
            hint = pa_language.hint(text)
            prompt_lang = hint if hint in self._allowed else lang
            streaming = self.streaming
            if streaming:
                self.bridge.open_turn(number, prompt_lang, self._allowed, disclosed=disclosed)
            work = asyncio.ensure_future(asyncio.to_thread(self._turn_sync, call, text, at, asr_locale))
            streamed: list[AgentSentence] = []
            closed = False
            try:
                if streaming:
                    async for item in self._stream_items(work):
                        if isinstance(item, AgentSentence):
                            streamed.append(item)
                        yield item
                try:
                    out = await asyncio.shield(work)
                except Exception as exc:   # noqa: BLE001  (a turn never ends in silence)
                    log.event("brain_turn_failed", level="error", call=self.tag, turn=number,
                              error=type(exc).__name__)
                    out = None
                if streaming:
                    tail = self._close_stream()
                    closed = True
                    for item in tail:
                        if isinstance(item, AgentSentence):
                            streamed.append(item)
                        yield item
            finally:
                if streaming and not closed:
                    self._close_stream()
            now = self.clock.now_ms()
            if out is None:
                error, error_lang = fixed_text("error", prompt_lang)
                sentence = AgentSentence(now, number, len(streamed), error, error_lang, "fixed")
                self.bus.publish(sentence)
                yield sentence
                done = AgentTurnDone(now, number, self._language, 0, False, True, latency_ms=now - started)
                self.bus.publish(done)
                yield done
                return
            self._language = out["language"]
            reply_lang = out["reply_language"]
            for sentence in reconcile(out["reply"], streamed, turn=number, language=reply_lang, at_ms=now,
                                      start_index=len(streamed)):
                self.bus.publish(sentence)
                yield sentence
            done = AgentTurnDone(
                self.clock.now_ms(), number, self._language, out["rounds"], out["cut"], out["error"],
                tuple(ProviderAction(a["name"], dict(a.get("arguments") or {})) for a in out["actions"]
                      if a.get("name") in ACTIONS),
                out["state"].get("outcome"), awaiting_confirmation(out["state"]), asked_question(out["reply"]),
                self.clock.now_ms() - started, tuple(out["tools"]))
            log.event("brain_turn", call=self.tag, turn=number, rounds=done.rounds, cut=done.cut, error=done.error,
                      tools=list(done.tools), language=done.language, streamed=len(streamed),
                      actions=[a.name for a in done.actions], ms=done.latency_ms)
            self.bus.publish(done)
            yield done

    def _close_stream(self) -> list:
        """Ends the bridge's turn; returns what it emitted that the turn has
        not yielded yet (the queue's rest plus the last fragment)."""
        rest = []
        self.bridge.close_turn()
        while not self.bridge.items.empty():
            rest.append(self.bridge.items.get_nowait())
        return rest

    async def _stream_items(self, work: asyncio.Future):
        items = self.bridge.items
        while True:
            if not items.empty():
                yield items.get_nowait()
                continue
            if work.done():
                return
            getter = asyncio.ensure_future(items.get())
            await asyncio.wait({getter, work}, return_when=asyncio.FIRST_COMPLETED)
            if getter.done():
                yield getter.result()
            else:
                getter.cancel()

    def _turn_sync(self, call, text, at, asr_locale) -> dict:
        """app.run_call_turn with this call's model (worker thread)."""
        app = self.app
        lang = call.get("language") or "sv"
        if call.get("locationPending"):
            options = [{**(shared_tenant.get_location(call["tenantId"], x) or {}), "locationId": x}
                       for x in call.get("candidateLocations") or []]
            call, reply = session.choose_location(call, text, locations=options, at=at, lang=lang)
            if reply is not None:
                return self._out(reply, lang, [], {}, None, lang)
        tenant_row, location, config, loc = app._context(call["tenantId"], call["locationId"])
        location = {**location, "locationId": call["locationId"]}
        started = pa.parse_iso(call.get("answeredAt")) or at
        max_seconds = int(loc.get("maxCallSeconds") or pa.MAX_CALL_DEFAULT)
        if at - started > timedelta(seconds=max_seconds):
            app.finish(call, at, "max_length")
            return self._out(copy.text(copy.MAX_LENGTH, lang), lang,
                             [{"name": "end_call", "arguments": {"reason": "max_length"}}], {}, None, lang)
        result, updated, state, actions = session.call_turn(
            call, text, model=self.model, invoke=app.invoker(), voice=app.client("voice"), tenant_row=tenant_row,
            location=location, loc=loc, config=config, at=at, add_callback=app.add_callback_for(app.updated_or(call)),
            send_sms=app.sms_sender(), asr_locale=asr_locale)
        if result.error:
            session.metric("ProviderErrors", 1)
        now_lang = (updated or {}).get("language") or lang
        said = result.language if result.language and languages.can_speak(result.language) else now_lang
        if state.get("unsupported") and result.language == state.get("unsupported"):
            said = now_lang
        return self._out(result.reply, now_lang, actions, state, result, said)

    @staticmethod
    def _out(reply, lang, actions, state, result, reply_lang) -> dict:
        return {"reply": reply or "", "language": lang, "actions": list(actions or []), "state": state or {},
                "rounds": getattr(result, "rounds", 0), "cut": bool(getattr(result, "cut", False)),
                "error": bool(getattr(result, "error", False)),
                "tools": [t["name"] for t in getattr(result, "tool_calls", None) or []],
                "reply_language": reply_lang}

    # -- language ----------------------------------------------------------------------------

    async def set_language(self, code: str, at_ms: int, reason: str) -> str | None:
        code = pa_language.normalise(code)
        if not languages.can_speak(code) or (self._allowed and code not in self._allowed):
            return None
        async with self._lock:
            said = await asyncio.to_thread(self._set_language_sync, code)
        if said is not False:
            self._language = code
            log.event("brain_language", call=self.tag, language=code, reason=reason, at_ms=at_ms)
        return said or None

    def _set_language_sync(self, code: str):
        call = self._call()
        if (call.get("language") or "sv") == code or session.language_locked(call):
            return False
        already = code in (pa.plain(call.get("disclosure") or {}).get("languages") or [])
        call, said = calls.repeat_disclosure(call, code, pa.now(), new_language=True)
        state = not_heard_state(json.loads(call.get("toolState") or "{}"))
        if said is None and not already:
            state["discloseNext"] = True          # no fixed sentence: the model says it in its next reply
        calls.set_status(call, call["status"], [call["status"]], language=code,
                         languageSwitches=int(call.get("languageSwitches") or 0) + 1,
                         toolState=json.dumps(state, default=str))
        return said

    @property
    def language(self) -> str:
        return self._language

    # -- interruptions -----------------------------------------------------------------------

    async def mark_not_heard(self, heard: list[str]) -> None:
        if not self.call_id:
            return
        async with self._lock:
            await asyncio.to_thread(self._not_heard_sync, list(heard))
        log.event("brain_not_heard", call=self.tag, heard=len(heard))

    def _not_heard_sync(self, heard: list[str]) -> None:
        call = self._call()
        if call.get("status") not in calls.OPEN:
            return
        state = not_heard_state(json.loads(call.get("toolState") or "{}"))
        messages = pa_agent.loads(call.get("agentMessages"))
        pa_agent.replace_last_reply(messages, (" ".join(heard) + " ...").strip())
        calls.set_status(call, call["status"], [call["status"]], toolState=json.dumps(state, default=str),
                         agentMessages=pa_agent.dumps(messages))

    async def greeting_interrupted(self) -> str | None:
        if not self.call_id:
            return None
        call = await asyncio.to_thread(self._call)
        if call.get("status") not in ("answered", "disclosed", "talking"):
            return None
        _, said = await asyncio.to_thread(calls.repeat_disclosure, call, call.get("language") or "sv", pa.now())
        return said

    async def greeting_played(self, spoken: str | None = None) -> None:
        """The disclosure evidence (calls.disclosed with the greeting text),
        once the greeting's audio finished on the line. `spoken`: what was
        played when it was not the greeting (the short disclosure after an
        interrupted greeting)."""
        if not self.call_id:
            return
        call = await asyncio.to_thread(self._call)
        if (call.get("disclosure") or {}).get("playedAt"):
            return
        await asyncio.to_thread(calls.disclosed, call, pa.now(), spoken or self._greeting)

    async def transfer_result(self, answered: bool) -> str | None:
        """Records the transfer; returns copy.TRANSFER_UNANSWERED when not answered."""
        if not self.call_id:
            return None
        await asyncio.to_thread(self._transfer_sync, bool(answered))
        log.event("brain_transfer", call=self.tag, answered=bool(answered))
        return None if answered else fixed_text("transfer_unanswered", self._language)[0]

    def _transfer_sync(self, answered: bool) -> None:
        call = self._call()
        if call.get("status") not in calls.OPEN:
            return
        state = json.loads(call.get("toolState") or "{}")
        state["transfer"] = {"answered": answered}
        if answered:
            state["outcome"] = "transferred"
        elif state.get("outcome") == "transferred":
            state.pop("outcome")
        calls.set_status(call, "talking", ["answered", "disclosed", "talking", "transferring"],
                         transfer={"answered": answered, "at": pa.iso(pa.now())},
                         toolState=json.dumps(state, default=str))

    async def finish(self, reason: Literal["hangup", "agent_end", "transferred", "max_length", "error",
                                          "refused", "still_there"]) -> None:
        if not self.call_id:
            return
        async with self._lock:
            call = await asyncio.to_thread(calls.get_call, self.tenant_id, self.call_id)
            if call is None or call.get("status") == "processed":
                return
            await asyncio.to_thread(self.app.finish, call, pa.now(), reason)
        log.event("brain_finish", call=self.tag, reason=reason)

    def fixed(self, key: Literal["still_there", "error", "transferring", "max_length"], language: str) -> str:
        """Booqy-authored copy in `language` (copy.STILL_THERE, copy.ERROR, ...)."""
        return fixed_text(key, language)[0]


def pa_language_last(text: str) -> str:
    """The normalised last sentence of a copy text."""
    sentences = split_sentences(text)
    return calls.normalized_text(sentences[-1]) if sentences else ""
