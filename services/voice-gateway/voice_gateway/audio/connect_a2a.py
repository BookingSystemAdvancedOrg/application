"""Amazon Connect agent-to-agent (A2A) audio adapter. Owner: builder 'listen'.

The gateway as an external A2A agent in "talk to the customer" mode with
audio streaming (handoffAgentConfiguration with audioStreamingEnabled and
immediateHandoff). Connect Customer opens one secure WebSocket per voice
contact, streams the caller's PCM to us and plays the PCM we stream back.
Setup, IAM and open questions: CONNECT-A2A.md. Protocol source (read
2026-11-20): Connect admin guide a2a-external-server / a2a-how-it-works /
a2a-quotas / a2a-setup-external, dev guide a2a-developer-guide and
a2a-sample-events (A2A v1.0, JSON-RPC binding, extension
https://docs.aws.amazon.com/connect/a2a/ext/v1).

Wire rules implemented here (doc section in brackets):
  * upgrade: `Authorization: Bearer <apiKey>` checked in constant time, 401
    when missing, 403 when wrong; `A2A-Extensions` echoed back; W3C
    `traceparent` / `tracestate` kept for the spans [Transport and discovery];
  * INIT_SESSION -> INIT_SESSION_RESPONSE with the request's id; audio is
    LINEAR_PCM 16-bit mono at 8, 16 or 24 kHz; we counter-propose 16 kHz
    (the gateway's canonical rate) in a direction where Connect proposed
    another rate, so Connect resamples, not us [Session initiation];
  * no output before CHANNEL_STATE READY [Channel readiness];
  * caller audio: SendMessage requests with one `raw` part, mediaType
    audio/lpcm, no turn markers; cut into 20 ms canonical frames, gaps
    concealed with silence (adapter.py timing contract) [A.3b];
  * assistant audio: artifactUpdate AUDIO_RESPONSE_CHUNK on a task, one
    artifact per spoken response, never two open at once, first chunk
    append false, then append true, the last lastChunk true; then the
    turn's single TRACING_SPAN artifact, then the terminal statusUpdate
    TASK_STATE_COMPLETED [Turn lifecycle, Audio streaming, A.4b, A.10b];
    short gaps inside a response are bridged with silence (an open artifact
    that runs dry is an underrun and Connect closes the connection);
  * barge-in (clear): INTERRUPTION USER_AUDIO_INPUT first so Connect
    flushes, no lastChunk for the cut artifact, then its trace and the
    terminal status; the next response gets a fresh task and artifact [A.7];
  * hang-up and transfer: FINISH COMPLETE or ESCALATE (only types listed
    in supportedFinishTypes), after the queued audio played; nothing is
    sent after FINISH [Finishing];
  * reconnect: close codes 1005, 1006, 1012, 1013, 1014 (or no close
    frame) keep the call alive for `reconnect_wait_ms`; Connect reconnects
    once with a new INIT_SESSION for the same contextId, which re-attaches
    the running call; a second drop ends it [Connection retries];
  * FRAGMENT reassembly in, fragmentation of oversized trace frames out,
    never for audio [Transport fragmentation];
  * JSON-RPC errors carry google.rpc.ErrorInfo [Errors].

Text (transcripts, sentences) goes into the trace spans because Connect
requires the turn's input and output there (and keeps it as the contact's
conversation history); it is never logged. Phone numbers are never logged.

Concurrency: one ConnectA2ACall per contact (a TelephonyAdapter), a 20 ms
ticker per call (silence concealment, gap bridging, reconnect deadline,
fragment expiry), and one reader per WebSocket. The transport is behind a
tiny `Transport` protocol so tests drive it with a fake Connect client.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Protocol

from voice_gateway import log
from voice_gateway.audio import frames as fr
from voice_gateway.audio.adapter import CallInfo, TransferOutcome
from voice_gateway.audio.frames import AudioFormat, Frame
from voice_gateway.clock import Clock

EXT = "https://docs.aws.amazon.com/connect/a2a/ext/v1"
EVENT_TYPE = f"{EXT}/eventType"
AUDIO_MEDIA_TYPE = "audio/lpcm"
RATES = (8000, 16000, 24000)
RECONNECT_CODES = frozenset({1005, 1006, 1012, 1013, 1014})
NO_CLOSE_FRAME = 1006
CLOSE_NORMAL, CLOSE_POLICY, CLOSE_RESTART = 1000, 1008, 1012
FINISH_COMPLETE, FINISH_ESCALATE = "COMPLETE", "ESCALATE"
SPAN_SERVER, SPAN_CLIENT = 2, 3
STATUS_OK, STATUS_ERROR = 1, 2
_CUE_KINDS = ("backchannel", "filler")


@dataclass(frozen=True)
class A2AConfig:
    """Adapter tunables (ms). Kept here, not in config.py (frozen shape)."""
    preferred_rate: int = 16000         # counter-proposed for both directions (no resampling in the gateway)
    counter_propose: bool = True        # False: take Connect's proposal and resample here
    init_timeout_ms: int = 5000         # INIT_SESSION after the upgrade
    ready_timeout_ms: int = 10000       # CHANNEL_STATE READY after INIT_SESSION
    reconnect_wait_ms: int = 8000       # a dropped voice socket may come back this long (Connect retries once)
    conceal_after_ms: int = 100         # caller audio missing this long: silence frames keep the stream continuous
    bridge_ms: int = 400                # a gap inside a response is filled with silence up to this, then it closes
    finish_drain_max_ms: int = 4000     # FINISH waits for queued audio to play, at most this
    finish_close_ms: int = 2000         # after FINISH, close the socket ourselves if Connect has not
    max_frame_bytes: int = 60000        # larger outbound JSON frames (traces) go as FRAGMENT parts
    max_inbound_bytes: int = 1 << 20    # largest reassembled inbound payload
    fragment_ttl_ms: int = 10000
    max_calls: int = 20                 # sessions per task; the upgrade answers 503 above it
    agent_name: str = "booqy-voice-gateway"
    escalate_on: tuple[str, ...] = ("error",)   # hangup reasons sent as ESCALATE (the rest COMPLETE)
    allowed_instance_arns: tuple[str, ...] = ()  # empty: any instance that holds the API key


# --- auth and handshake (pure) ----------------------------------------------------------------

def check_auth(headers: dict, api_key: str) -> int | None:
    """None when the bearer token equals the API key, else the HTTP status
    (401 missing or malformed, 403 wrong). Constant-time comparison."""
    value = headers.get("authorization") or ""
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return 401
    if not api_key or not hmac.compare_digest(token.strip().encode(), api_key.encode()):
        return 403
    return None


def echo_headers(headers: dict) -> dict:
    """Response headers for the upgrade: the A2A-Extensions activation echo."""
    ext = headers.get("a2a-extensions")
    return {"A2A-Extensions": ext} if ext else {}


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    parent_span_id: str | None
    tracestate: str | None = None

    @classmethod
    def from_headers(cls, headers: dict) -> "TraceContext":
        parts = (headers.get("traceparent") or "").strip().split("-")
        if len(parts) == 4 and len(parts[1]) == 32 and len(parts[2]) == 16 and _hex(parts[1]) and _hex(parts[2]) \
                and set(parts[1]) != {"0"} and set(parts[2]) != {"0"}:
            return cls(parts[1].lower(), parts[2].lower(), headers.get("tracestate"))
        return cls(secrets.token_hex(16), None, headers.get("tracestate"))


def _hex(s: str) -> bool:
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


# --- JSON-RPC framing (pure) --------------------------------------------------------------------

def _rid() -> str:
    return secrets.token_hex(4)


def rpc_result(value_key: str, value: dict, rid: str | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": rid or _rid(), "result": {value_key: value}}


def rpc_error(rid, code: int, message: str, reason: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message, "data": {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason, "domain": "a2a-protocol.org"}}}


def event_type(message: dict) -> str | None:
    return (message.get("metadata") or {}).get(EVENT_TYPE)


def data_part(message: dict, key: str) -> dict | None:
    for part in message.get("parts") or ():
        data = part.get("data")
        if isinstance(data, dict) and key in data:
            return data[key] if isinstance(data[key], dict) else {}
    return None


def audio_parts(message: dict) -> list[bytes]:
    out = []
    for part in message.get("parts") or ():
        if "raw" in part and str(part.get("mediaType", AUDIO_MEDIA_TYPE)).startswith(AUDIO_MEDIA_TYPE):
            out.append(base64.b64decode(part["raw"]))
    return out


def _audio_config(rate: int) -> dict:
    return {"encoding": "LINEAR_PCM", "sampleRateHertz": rate, "sampleSizeBits": 16, "channelCount": 1}


def _valid_audio(cfg) -> bool:
    return (isinstance(cfg, dict) and cfg.get("encoding") == "LINEAR_PCM" and cfg.get("sampleSizeBits") == 16
            and cfg.get("channelCount") == 1 and cfg.get("sampleRateHertz") in RATES)


@dataclass(frozen=True)
class InitSession:
    request_id: object
    context_id: str
    instance_arn: str
    contact_arn: str
    supported_finish: tuple[str, ...]
    tracing: bool
    in_rate: int
    out_rate: int
    counter_in: bool
    counter_out: bool
    customer: str | None
    system: str | None
    channel: str | None
    history_len: int

    @property
    def contact_id(self) -> str:
        return self.contact_arn.rsplit("/", 1)[-1] if self.contact_arn else self.context_id


class ProtocolError(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


def parse_init(frame: dict, cfg: A2AConfig) -> InitSession:
    """INIT_SESSION -> InitSession; ProtocolError for anything we cannot serve."""
    message = (frame.get("params") or {}).get("message") or {}
    init = data_part(message, "initSession")
    if frame.get("method") != "SendMessage" or event_type(message) != "INIT_SESSION" or init is None:
        raise ProtocolError(-32600, "INIT_SESSION_EXPECTED")
    context_id = message.get("contextId")
    if not context_id:
        raise ProtocolError(-32602, "CONTEXT_ID_MISSING")
    if cfg.allowed_instance_arns and init.get("instanceArn") not in cfg.allowed_instance_arns:
        raise ProtocolError(-32602, "INSTANCE_NOT_ALLOWED")
    ain, aout = init.get("audioInputConfiguration"), init.get("audioOutputConfiguration")
    if ain is None or aout is None:
        raise ProtocolError(-32602, "AUDIO_REQUIRED")          # chat or text streaming: not served by the gateway
    if not _valid_audio(ain) or not _valid_audio(aout):
        raise ProtocolError(-32602, "UNSUPPORTED_AUDIO_CONFIGURATION")
    want = cfg.preferred_rate if cfg.preferred_rate in RATES else 16000
    in_rate, out_rate = ain["sampleRateHertz"], aout["sampleRateHertz"]
    counter_in = cfg.counter_propose and in_rate != want
    counter_out = cfg.counter_propose and out_rate != want
    ctx = init.get("contactContext") or {}
    return InitSession(
        request_id=frame.get("id"), context_id=str(context_id), instance_arn=str(init.get("instanceArn") or ""),
        contact_arn=str(init.get("contactArn") or ""),
        supported_finish=tuple(init.get("supportedFinishTypes") or (FINISH_COMPLETE, FINISH_ESCALATE)),
        tracing=init.get("subscribeToTracingEvents") is not False,
        in_rate=want if counter_in else in_rate, out_rate=want if counter_out else out_rate,
        counter_in=counter_in, counter_out=counter_out,
        customer=_address(ctx, "customer"), system=_address(ctx, "system"),
        channel=_first(ctx, ("channel", "Channel")), history_len=len(init.get("history") or ()))


def _first(d: dict, keys) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _address(ctx: dict, who: str) -> str | None:
    """contactContext's customer or system address (the field shape is not
    in the samples: a string, or an object with an address)."""
    for key in (f"{who}Address", f"{who}Endpoint", f"{who.capitalize()}Address", f"{who.capitalize()}Endpoint"):
        v = ctx.get(key)
        if isinstance(v, dict):
            v = _first(v, ("address", "Address", "value"))
        if isinstance(v, str) and v.strip():
            v = v.strip()
            return v if v.startswith("+") else None
    return None


def init_response(init: InitSession) -> dict:
    body: dict = {"statusCode": "200"}
    if init.counter_in:
        body["audioInputConfiguration"] = _audio_config(init.in_rate)
    if init.counter_out:
        body["audioOutputConfiguration"] = _audio_config(init.out_rate)
    return rpc_result("message", {
        "messageId": f"m-{_rid()}", "contextId": init.context_id, "role": "ROLE_AGENT",
        "parts": [{"data": {"initSessionResponse": body}, "mediaType": "application/json"}],
        "extensions": [EXT], "metadata": {EVENT_TYPE: "INIT_SESSION_RESPONSE"}}, rid=init.request_id)


def fragment_frames(frame: dict, limit: int) -> list[str]:
    """One JSON frame, or FRAGMENT frames when it exceeds `limit` bytes."""
    text = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode()) <= limit:
        return [text]
    payload = base64.b64encode(text.encode()).decode("ascii")
    size = max(1024, limit - 1024)
    chunks = [payload[i:i + size] for i in range(0, len(payload), size)]
    fid = f"f-{_rid()}"
    return [json.dumps(rpc_result("message", {
        "messageId": f"m-{_rid()}", "role": "ROLE_AGENT",
        "parts": [{"data": {"fragment": {"fragmentId": fid, "partNumber": n, "totalParts": len(chunks),
                                         "payload": chunk, "encoding": "base64"}},
                   "mediaType": "application/json"}],
        "metadata": {EVENT_TYPE: "FRAGMENT"}}, rid=frame.get("id")), separators=(",", ":")) for n, chunk in
        enumerate(chunks)]


class Reassembler:
    """Inbound FRAGMENT parts -> the original JSON frame."""

    def __init__(self, max_bytes: int, ttl_ms: int) -> None:
        self.max_bytes, self.ttl_ms = max_bytes, ttl_ms
        self._parts: dict[str, dict] = {}

    def add(self, fragment: dict, now_ms: int) -> dict | None:
        fid, total, n = fragment.get("fragmentId"), fragment.get("totalParts"), fragment.get("partNumber")
        if not fid or not isinstance(total, int) or not isinstance(n, int) or not 0 <= n < total <= 4096:
            raise ProtocolError(-32602, "BAD_FRAGMENT")
        entry = self._parts.setdefault(fid, {"total": total, "parts": {}, "bytes": 0, "at": now_ms})
        payload = str(fragment.get("payload") or "")
        entry["parts"][n] = payload
        entry["bytes"] += len(payload)
        if entry["bytes"] > self.max_bytes * 4 // 3 + 4:
            self._parts.pop(fid, None)
            raise ProtocolError(-32602, "FRAGMENT_TOO_LARGE")
        if len(entry["parts"]) < entry["total"]:
            return None
        self._parts.pop(fid, None)
        joined = "".join(entry["parts"][i] for i in range(entry["total"]))
        return json.loads(base64.b64decode(joined))

    def expire(self, now_ms: int) -> None:
        for fid in [f for f, e in self._parts.items() if now_ms - e["at"] > self.ttl_ms]:
            self._parts.pop(fid, None)


# --- traces (pure) ------------------------------------------------------------------------------

def _attr(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _message_event(at_ns: int, role: str, participant: str, content: str, **extra) -> dict:
    attrs = [_attr("aws.connect.span.message.role", role), _attr("aws.connect.span.message.type", "TEXT_MESSAGE"),
             _attr("aws.connect.span.message.participant", participant),
             _attr("aws.connect.span.message.content", content)]
    attrs += [_attr(k, v) for k, v in extra.items()]
    return {"name": "aws.connect.span.message", "timeUnixNano": str(at_ns), "attributes": attrs}


@dataclass
class _Played:
    item_id: str
    kind: str
    start_ms: int
    end_ms: int | None = None
    interrupted: bool = False


@dataclass
class _Tool:
    name: str
    start_ms: int
    end_ms: int | None = None
    ok: bool | None = None


class TurnTracer:
    """Builds the one TRACING_SPAN document per task from what the bus and
    the speaker report: the caller's turn text (input, attributed once to
    the first response that carries speech), the spoken sentences (output),
    a segment span per played item (A.10b latency) and a span per tool."""

    def __init__(self, agent_name: str, model: str | None, wall0_ms: float) -> None:
        self.agent_name, self.model, self.wall0_ms = agent_name, model, wall0_ms
        self.texts: dict[str, str] = {}
        self._input: tuple[str, int] | None = None
        self._tools: list[_Tool] = []

    def ns(self, ms: int) -> int:
        return int((self.wall0_ms + ms) * 1_000_000)

    # inputs from the bus (synchronous handlers) and the speaker tap
    def caller_turn(self, text: str, at_ms: int) -> None:
        if self._input is not None:                 # the earlier one was never answered: merged, never lost
            text = f"{self._input[0]} {text}".strip()
            at_ms = self._input[1]
        self._input = (text, at_ms)

    def note_text(self, item_id: str, text: str) -> None:
        self.texts[item_id] = text

    def tool_started(self, name: str, at_ms: int) -> None:
        self._tools.append(_Tool(name, at_ms))

    def tool_finished(self, name: str, at_ms: int, ok: bool | None) -> None:
        for t in reversed(self._tools):
            if t.name == name and t.end_ms is None:
                t.end_ms, t.ok = at_ms, ok
                return

    def build(self, ctx: TraceContext, task_start_ms: int, end_ms: int, items: list[_Played], *,
              interrupted: bool, error: str | None = None) -> dict:
        spoken = [i for i in items if i.kind not in _CUE_KINDS and i.item_id in self.texts]
        input_ = None
        if spoken and self._input is not None:
            input_, self._input = self._input, None
        tools = [t for t in self._tools if t.end_ms is not None or input_ is not None]
        self._tools = [t for t in self._tools if t not in tools]
        start = min([task_start_ms] + ([input_[1]] if input_ else []) + [t.start_ms for t in tools])
        root_id = secrets.token_hex(8)
        events = []
        if input_ is not None:
            events.append(_message_event(self.ns(input_[1]), "input", "user", input_[0]))
        events.append(_message_event(self.ns(end_ms), "output", "assistant",
                                     " ".join(self.texts[i.item_id] for i in spoken),
                                     **({"booqy.interrupted": True} if interrupted else {})))
        attrs = [_attr("gen_ai.agent.name", self.agent_name), _attr("gen_ai.operation.name", "invoke_agent")]
        if self.model:
            attrs.append(_attr("gen_ai.request.model", self.model))
        root = {"traceId": ctx.trace_id, "spanId": root_id, "name": f"invoke_agent {self.agent_name}",
                "kind": SPAN_SERVER, "startTimeUnixNano": str(self.ns(start)), "endTimeUnixNano": str(self.ns(end_ms)),
                "attributes": attrs, "events": events,
                "status": {"code": STATUS_ERROR, "message": error} if error else {"code": STATUS_OK}}
        if ctx.parent_span_id:
            root["parentSpanId"] = ctx.parent_span_id
        spans = [root]
        for n, item in enumerate(items, 1):
            seg_attrs = [_attr("gen_ai.operation.name", "chat"), _attr("booqy.audio.kind", item.kind)]
            if self.model:
                seg_attrs.append(_attr("gen_ai.request.model", self.model))
            if item.interrupted:
                seg_attrs.append(_attr("booqy.interrupted", True))
            spans.append({"traceId": ctx.trace_id, "spanId": secrets.token_hex(8), "parentSpanId": root_id,
                          "name": f"chat sentence-{n}", "kind": SPAN_CLIENT,
                          "startTimeUnixNano": str(self.ns(item.start_ms)),
                          "endTimeUnixNano": str(self.ns(max(item.start_ms, item.end_ms or end_ms))),
                          "attributes": seg_attrs, "status": {"code": STATUS_OK}})
        for n, tool in enumerate(tools, 1):
            t_end = tool.end_ms if tool.end_ms is not None else end_ms
            tool_id = f"{root_id}-{n}"
            # The bus carries the tool's name and result status only (the arguments stay in the brain):
            # see CONNECT-A2A.md, open points.
            spans.append({"traceId": ctx.trace_id, "spanId": secrets.token_hex(8), "parentSpanId": root_id,
                          "name": f"execute_tool {tool.name}", "kind": SPAN_CLIENT,
                          "startTimeUnixNano": str(self.ns(tool.start_ms)), "endTimeUnixNano": str(self.ns(t_end)),
                          "attributes": [_attr("gen_ai.operation.name", "execute_tool"),
                                         _attr("gen_ai.tool.name", tool.name),
                                         _attr("aws.connect.span.message.tool_id", tool_id)],
                          "events": [
                              _message_event(self.ns(tool.start_ms), "input", "assistant",
                                             json.dumps({"tool": tool.name}),
                                             **{"aws.connect.span.message.tool_id": tool_id}),
                              _message_event(self.ns(t_end), "output", "assistant",
                                             json.dumps({"ok": tool.ok, "ms": t_end - tool.start_ms}),
                                             **{"aws.connect.span.message.tool_id": tool_id})],
                          "status": {"code": STATUS_ERROR, "message": "tool failed"} if tool.ok is False
                          else {"code": STATUS_OK}})
        return {"otlp": {"resourceSpans": [{
            "resource": {"attributes": [_attr("service.name", self.agent_name)]},
            "scopeSpans": [{"scope": {"name": "booqy.voice-gateway", "version": "1"}, "spans": spans}]}]}}


# --- transport ----------------------------------------------------------------------------------

class TransportClosed(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class Transport(Protocol):
    async def recv(self) -> str:
        """Next text frame; raises TransportClosed(code) when the socket ends."""

    async def send(self, text: str) -> None: ...

    async def close(self, code: int, reason: str = "") -> None: ...


# --- one contact ------------------------------------------------------------------------------

@dataclass
class _Task:
    task_id: str
    artifact_id: str
    start_ms: int
    chunks: int = 0
    last_real_ms: int = 0
    audio_done: bool = False            # lastChunk sent (FINISH waits for the playout after it)
    items: list[_Played] = field(default_factory=list)


class ConnectA2ACall:
    """One Connect voice contact as a TelephonyAdapter (see the module docstring)."""

    def __init__(self, clock: Clock, cfg: A2AConfig, init: InitSession, ctx: TraceContext, *,
                 model: str | None = None, wall: Callable[[], float] = time.time) -> None:
        self.clock, self.cfg, self.init, self.trace_ctx = clock, cfg, init, ctx
        self.context_id = init.context_id
        self.info = CallInfo(init.contact_id, init.system or "", init.customer, AudioFormat("pcm16", init.in_rate))
        self.tracer = TurnTracer(cfg.agent_name, model, wall() * 1000 - clock.now_ms())
        self.reconnects = 0
        self.dropped_out_ms = 0
        self.finish_sent: tuple[str, str] | None = None
        self._transport: Transport | None = None
        self._ready = asyncio.Event()
        self._closed = False
        self._ever_ready = False
        self._finishing = False
        self._lost_at: int | None = None
        self._frames: asyncio.Queue = asyncio.Queue()
        self._in_buf = bytearray()
        self._next_in_ts = clock.now_ms()
        self._last_in_ms = clock.now_ms()
        self._task: _Task | None = None
        self._line_end = clock.now_ms()
        self._reasm = Reassembler(cfg.max_inbound_bytes, cfg.fragment_ttl_ms)
        self._ticker: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._set_rates(init)

    def _set_rates(self, init: InitSession) -> None:
        from voice_gateway.audio.resample import Resampler
        self.in_rate, self.out_rate = init.in_rate, init.out_rate
        self._in_rs = Resampler(init.in_rate, fr.CANONICAL_RATE) if init.in_rate != fr.CANONICAL_RATE else None
        self._out_rs = Resampler(fr.CANONICAL_RATE, init.out_rate) if init.out_rate != fr.CANONICAL_RATE else None

    # -- connection management (called by the endpoint) -----------------------------------------

    def attach(self, transport: Transport, init: InitSession | None = None, ctx: TraceContext | None = None) -> None:
        """First connection, or Connect's one reconnect (new INIT_SESSION, same contextId)."""
        if init is not None:
            self.init = init
            self._set_rates(init)
            self._in_buf.clear()
        if ctx is not None:
            self.trace_ctx = ctx
        self._transport, self._lost_at = transport, None
        self._ready.clear()

    def connection_lost(self, transport: Transport, code: int) -> None:
        if transport is not self._transport:
            return
        self._transport = None
        self._ready.clear()
        now = self.clock.now_ms()
        self._task = None                          # its artifact died with the socket; a new one opens after READY
        self._line_end = now
        if self.finish_sent is not None or self._closed:
            self._close()
        elif code in RECONNECT_CODES and self.reconnects < 1:
            self._lost_at = now
            log.event("a2a_connection_lost", call=log.call_tag(self.context_id), code=code, waiting=True)
        else:
            log.event("a2a_connection_closed", call=log.call_tag(self.context_id), code=code)
            self._close()

    @property
    def connected(self) -> bool:
        return self._transport is not None

    # -- TelephonyAdapter ------------------------------------------------------------------------

    async def accept(self) -> CallInfo:
        if not await self._wait(self._ready, self.cfg.ready_timeout_ms):
            raise TimeoutError("CHANNEL_STATE READY not received")
        self._ever_ready = True
        if self._ticker is None:
            now = self.clock.now_ms()
            if self._frames.empty() and not self._in_buf:
                self._next_in_ts = now
            self._last_in_ms = now
            self._ticker = asyncio.ensure_future(self._tick_loop())
        return self.info

    @property
    def closed(self) -> bool:
        return self._closed

    async def caller_frames(self) -> AsyncIterator[Frame]:
        while True:
            frame = await self._frames.get()
            if frame is None:
                return
            yield frame

    async def play(self, frame: Frame) -> None:
        if self._closed or self._finishing:
            return
        if self._transport is None or not self._ready.is_set():
            self.dropped_out_ms += frame.duration_ms          # never output before READY or while reconnecting
            return
        now = self.clock.now_ms()
        if self._task is None:
            self._task = _Task(f"task-{_rid()}", f"resp-{_rid()}-audio", now)
        self._task.last_real_ms = now
        await self._send_audio(frame.pcm, frame.duration_ms, last=False)

    async def clear(self) -> int:
        now = self.clock.now_ms()
        dropped = max(0, self._line_end - now)
        if self._transport is not None and self._ready.is_set() and (self._task is not None or dropped):
            await self._send(rpc_result("message", {
                "messageId": f"m-{_rid()}", "contextId": self.context_id, "role": "ROLE_AGENT",
                "parts": [{"data": {"interruption": {"type": "USER_AUDIO_INPUT", "reason": "user_speech"}},
                           "mediaType": "application/json"}],
                "extensions": [EXT], "metadata": {EVENT_TYPE: "INTERRUPTION"}}))
            if self._task is not None:
                for item in self._task.items:
                    if item.end_ms is None:
                        item.interrupted = True
                await self._close_task(now, interrupted=True)
        self._task = None
        self._line_end = now
        return dropped

    async def hangup(self, reason: str) -> None:
        if self._closed or self.finish_sent is not None:
            return
        if not self._ever_ready:                         # READY never came: no output allowed, fail the collaboration
            transport = self._transport
            self._close()
            if transport is not None:
                await _quiet_close(transport, 1011, "channel not ready")
            return
        kind = FINISH_ESCALATE if reason in self.cfg.escalate_on else FINISH_COMPLETE
        if kind not in self.init.supported_finish:
            kind = FINISH_COMPLETE if FINISH_COMPLETE in self.init.supported_finish else kind
        await self._finish(kind, reason, error="gateway error" if reason == "error" else None)

    async def transfer(self, to_e164: str, timeout_s: int) -> TransferOutcome:
        """Connect owns the line: a transfer is FINISH ESCALATE, and the
        flow's Escalate path connects the restaurant (CONNECT-A2A.md). The
        gateway cannot see whether staff answer."""
        if FINISH_ESCALATE not in self.init.supported_finish or self._closed:
            return TransferOutcome(False, "escalate_unsupported" if not self._closed else "closed")
        await self._finish(FINISH_ESCALATE, "transfer_to_restaurant")
        return TransferOutcome(True, "escalated_to_flow")

    # -- inbound -----------------------------------------------------------------------------------

    async def on_frame(self, frame: dict) -> None:
        """One inbound JSON-RPC frame after INIT_SESSION."""
        if "method" not in frame:
            return                                      # a response to something of ours: nothing to do
        if frame.get("method") != "SendMessage":
            await self._send(rpc_error(frame.get("id"), -32601, "Method not found", "METHOD_NOT_FOUND"))
            return
        message = (frame.get("params") or {}).get("message") or {}
        kind = event_type(message)
        if kind == "FRAGMENT" or data_part(message, "fragment") is not None:
            whole = self._reasm.add(data_part(message, "fragment") or {}, self.clock.now_ms())
            if whole is not None:
                await self.on_frame(whole)
            return
        audio = audio_parts(message)
        if audio:
            for pcm in audio:
                self._on_audio(pcm)
            return
        if kind == "CHANNEL_STATE":
            state = (data_part(message, "channelState") or {}).get("state")
            if state == "READY":
                self._ready.set()
                if self._ticker is not None:
                    log.event("a2a_ready_again", call=log.call_tag(self.context_id), reconnects=self.reconnects)
            return
        if kind == "INTERRUPTION":
            itype = (data_part(message, "interruption") or {}).get("type")
            log.event("a2a_interruption_in", call=log.call_tag(self.context_id), type=itype)
            if self._task is not None:                  # stop the response Connect cut (our own VAD handles speech)
                await self._close_task(self.clock.now_ms(), interrupted=True)
            self._line_end = self.clock.now_ms()
            return
        if kind == "ERROR":
            err = data_part(message, "error") or {}
            log.event("a2a_error_in", level="warning", call=log.call_tag(self.context_id),
                      code=err.get("errorCode"), category=err.get("errorCategory"))
            return
        if kind == "INIT_SESSION":
            await self._send(rpc_error(frame.get("id"), -32600, "Session already initialised", "DUPLICATE_INIT"))
            return
        # DTMF_INPUT_COMPLETE, text input (<EMPTY_USER_INPUT>) and unknown events: the gateway listens to audio only
        log.event("a2a_ignored", call=log.call_tag(self.context_id), type=kind or "text")

    def _on_audio(self, pcm: bytes) -> None:
        if self._closed:
            return
        if self._in_rs is not None:
            pcm = self._in_rs.process(pcm)
        self._in_buf.extend(pcm)
        size = fr.frame_bytes()
        while len(self._in_buf) >= size:
            chunk = bytes(self._in_buf[:size])
            del self._in_buf[:size]
            self._frames.put_nowait(Frame(chunk, self._next_in_ts))
            self._next_in_ts += fr.FRAME_MS
        self._last_in_ms = self.clock.now_ms()

    # -- outbound ----------------------------------------------------------------------------------

    async def _send(self, frame: dict) -> bool:
        transport = self._transport
        if transport is None:
            return False
        try:
            async with self._send_lock:
                for text in fragment_frames(frame, self.cfg.max_frame_bytes):
                    await transport.send(text)
            return True
        except Exception as exc:   # noqa: BLE001  (the reader sees the close and decides)
            log.event("a2a_send_failed", level="warning", call=log.call_tag(self.context_id),
                      error=type(exc).__name__)
            return False

    async def _send_audio(self, pcm16k: bytes, ms: int, *, last: bool) -> None:
        task = self._task
        assert task is not None
        payload = self._out_rs.process(pcm16k) if self._out_rs is not None else pcm16k
        frame = rpc_result("artifactUpdate", {
            "taskId": task.task_id, "contextId": self.context_id,
            "artifact": {"artifactId": task.artifact_id,
                         "parts": [{"raw": base64.b64encode(payload).decode("ascii"), "mediaType": AUDIO_MEDIA_TYPE}],
                         "metadata": {EVENT_TYPE: "AUDIO_RESPONSE_CHUNK"}},
            "append": task.chunks > 0, "lastChunk": last})
        task.chunks += 1
        now = self.clock.now_ms()
        self._line_end = max(self._line_end, now) + ms
        await self._send(frame)

    async def _close_task(self, now: int, *, interrupted: bool, finish: tuple[str, str] | None = None,
                          error: str | None = None) -> None:
        """Ends the open response: lastChunk (unless cut), the turn's trace, the terminal status."""
        task, self._task = self._task, None
        if task is None:
            return
        if not interrupted and not task.audio_done:
            self._task = task                           # _send_audio needs it
            await self._send_audio(bytes(fr.frame_bytes()), fr.FRAME_MS, last=True)
            self._task = None
        end = max(now, self.clock.now_ms())
        for item in task.items:
            if item.end_ms is None and not interrupted:
                item.end_ms = max(item.start_ms, self._line_end)
        if self.init.tracing:
            doc = self.tracer.build(self.trace_ctx, task.start_ms, end, task.items, interrupted=interrupted,
                                    error=error)
            await self._send(rpc_result("artifactUpdate", {
                "taskId": task.task_id, "contextId": self.context_id,
                "artifact": {"artifactId": f"trace-{_rid()}",
                             "parts": [{"data": {"tracingSpan": doc}, "mediaType": "application/json"}],
                             "metadata": {EVENT_TYPE: "TRACING_SPAN"}},
                "append": False, "lastChunk": True}))
        status: dict = {"state": "TASK_STATE_COMPLETED", "timestamp": _iso(self.tracer.ns(end))}
        if finish is not None:
            status["message"] = _finish_message(*finish)
        await self._send(rpc_result("statusUpdate", {"taskId": task.task_id, "contextId": self.context_id,
                                                     "status": status}))

    async def _finish(self, kind: str, reason: str, *, error: str | None = None) -> None:
        if self.finish_sent is not None:
            return
        self._finishing = True
        if self._task is not None and self._transport is not None and not self._task.audio_done:
            await self._send_audio(bytes(fr.frame_bytes()), fr.FRAME_MS, last=True)   # no underrun while we wait
            self._task.audio_done = True
        now = self.clock.now_ms()
        if self._line_end > now:                        # the goodbye is still on Connect's buffer: let it play
            await self.clock.sleep_until(min(self._line_end, now + self.cfg.finish_drain_max_ms))
        self.finish_sent = (kind, reason)
        if self._transport is not None:
            if self._task is not None:
                await self._close_task(self.clock.now_ms(), interrupted=False, finish=(kind, reason), error=error)
            else:
                msg = _finish_message(kind, reason)
                msg.update({"contextId": self.context_id, "extensions": [EXT]})
                await self._send(rpc_result("message", msg))
        log.event("a2a_finish", call=log.call_tag(self.context_id), type=kind, reason=reason)
        self._close()
        transport = self._transport
        if transport is not None:
            asyncio.ensure_future(self._close_later(transport))

    async def _close_later(self, transport: Transport) -> None:
        await self.clock.sleep_ms(self.cfg.finish_close_ms)
        if self._transport is transport:
            try:
                await transport.close(CLOSE_NORMAL, "finished")
            except Exception:   # noqa: BLE001
                pass

    # -- trace inputs ------------------------------------------------------------------------------

    def attach_bus(self, bus) -> None:
        """The per-call EventBus (via the speaker factory hook, see `components_for_call`)."""
        from voice_gateway.events import (AgentSentence, PlaybackFinished, PlaybackStarted, ToolFinished,
                                          ToolStarted, TurnEnd)

        def on_event(ev) -> None:
            if isinstance(ev, TurnEnd):
                self.tracer.caller_turn(ev.text, ev.eos_ms)
            elif isinstance(ev, AgentSentence):
                self.tracer.note_text(f"t{ev.turn}-s{ev.index}", ev.text)
            elif isinstance(ev, PlaybackStarted):
                if self._task is not None:
                    self._task.items.append(_Played(ev.item_id, ev.kind, ev.at_ms))
            elif isinstance(ev, PlaybackFinished):
                for item in self._task.items if self._task is not None else ():
                    if item.item_id == ev.item_id and item.end_ms is None:
                        item.end_ms, item.interrupted = ev.at_ms, ev.interrupted
            elif isinstance(ev, ToolStarted):
                self.tracer.tool_started(ev.name, ev.at_ms)
            elif isinstance(ev, ToolFinished):
                self.tracer.tool_finished(ev.name, ev.at_ms, ev.ok)

        bus.listen(on_event, TurnEnd, AgentSentence, PlaybackStarted, PlaybackFinished, ToolStarted, ToolFinished)

    def note_text(self, item_id: str, text: str) -> None:
        self.tracer.note_text(item_id, text)

    # -- ticker ------------------------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        try:
            while not self._closed:
                await self.clock.sleep_ms(fr.FRAME_MS)
                await self.tick()
        except asyncio.CancelledError:
            pass

    async def tick(self) -> None:
        now = self.clock.now_ms()
        self._reasm.expire(now)
        if self._lost_at is not None and now - self._lost_at >= self.cfg.reconnect_wait_ms:
            log.event("a2a_reconnect_timeout", call=log.call_tag(self.context_id))
            self._close()
            return
        # caller audio missing (network, reconnect): silence keeps the 20 ms stream continuous
        horizon = now - self.cfg.conceal_after_ms
        if now - self._last_in_ms >= self.cfg.conceal_after_ms:
            while self._next_in_ts + fr.FRAME_MS <= horizon:
                self._frames.put_nowait(fr.silence(self._next_in_ts))
                self._next_in_ts += fr.FRAME_MS
        task = self._task
        if task is None or self._finishing or self._transport is None:
            return
        if now - task.last_real_ms >= self.cfg.bridge_ms:
            await self._close_task(now, interrupted=False)
        elif self._line_end - now < 2 * fr.FRAME_MS:
            await self._send_audio(bytes(fr.frame_bytes()), fr.FRAME_MS, last=False)

    # -- helpers -----------------------------------------------------------------------------------

    def _close(self) -> None:
        if not self._closed:
            self._closed = True
            self._frames.put_nowait(None)
        if self._ticker is not None and self._ticker is not asyncio.current_task():
            self._ticker.cancel()

    async def _wait(self, event: asyncio.Event, ms: int) -> bool:
        if event.is_set():
            return True
        waiter = asyncio.ensure_future(event.wait())
        timer = asyncio.ensure_future(self.clock.sleep_ms(ms))
        try:
            await asyncio.wait({waiter, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            timer.cancel()
        return event.is_set()


def _finish_message(kind: str, reason: str) -> dict:
    return {"messageId": f"m-{_rid()}", "role": "ROLE_AGENT",
            "parts": [{"data": {"finish": {"type": kind, "reason": reason}}, "mediaType": "application/json"}],
            "metadata": {EVENT_TYPE: "FINISH"}}


def _iso(ns: int) -> str:
    secs, rem = divmod(ns, 1_000_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(secs)) + f".{rem // 1_000_000:03d}Z"


# --- the endpoint (all contacts of one task) ----------------------------------------------------

class A2AEndpoint:
    """Accepts Connect's connections, owns the contextId registry, yields a
    ConnectA2ACall for every new contact (`calls()`); a reconnect for a
    known contextId re-attaches the running call instead."""

    def __init__(self, clock_factory: Callable[[], Clock], api_key: str, cfg: A2AConfig = A2AConfig(), *,
                 model: str | None = None) -> None:
        if not api_key:
            raise ValueError("an API key is required")
        self.clock_factory, self.api_key, self.cfg, self.model = clock_factory, api_key, cfg, model
        self.sessions: dict[str, ConnectA2ACall] = {}
        self.transports: dict[str, Transport] = {}
        self.draining = False
        self._new: asyncio.Queue = asyncio.Queue()

    @property
    def active(self) -> int:
        return sum(1 for c in self.sessions.values() if not c.closed)

    def upgrade_status(self, path: str, headers: dict) -> int | None:
        """HTTP status to refuse the WebSocket upgrade with, or None to accept."""
        if self.draining:
            return 503                               # Connect retries an upgrade once on 503
        status = check_auth(headers, self.api_key)
        if status is not None:
            return status
        if self.active >= self.cfg.max_calls:
            return 503
        return None

    async def calls(self) -> AsyncIterator[ConnectA2ACall]:
        while True:
            call = await self._new.get()
            if call is None:
                return
            yield call

    async def handle(self, transport: Transport, headers: dict) -> None:
        """One WebSocket connection (already authenticated) until it closes."""
        ctx = TraceContext.from_headers(headers)
        for cid in [c for c, call in self.sessions.items() if call.closed]:
            self.sessions.pop(cid, None)
        try:
            raw = await asyncio.wait_for(transport.recv(), self.cfg.init_timeout_ms / 1000)
        except (asyncio.TimeoutError, TransportClosed):
            await _quiet_close(transport, CLOSE_POLICY, "no INIT_SESSION")
            return
        try:
            frame = json.loads(raw)
            reasm = Reassembler(self.cfg.max_inbound_bytes, self.cfg.fragment_ttl_ms)
            while data_part((frame.get("params") or {}).get("message") or {}, "fragment") is not None:
                part = data_part(frame["params"]["message"], "fragment")
                whole = reasm.add(part, 0)
                if whole is not None:
                    frame = whole
                    break
                frame = json.loads(await asyncio.wait_for(transport.recv(), self.cfg.init_timeout_ms / 1000))
            init = parse_init(frame, self.cfg)
        except ProtocolError as exc:
            await _quiet_send(transport, rpc_error(_id(raw), exc.code, "Invalid INIT_SESSION", exc.reason))
            await _quiet_close(transport, CLOSE_POLICY, exc.reason)
            log.event("a2a_init_refused", reason=exc.reason)
            return
        except Exception as exc:   # noqa: BLE001  (malformed JSON, timeout while reassembling)
            await _quiet_send(transport, rpc_error(None, -32700, "Parse error", "PARSE_ERROR"))
            await _quiet_close(transport, CLOSE_POLICY, "parse error")
            log.event("a2a_init_refused", reason=type(exc).__name__)
            return

        call = self.sessions.get(init.context_id)
        if call is not None and not call.closed:
            if call.reconnects >= 1:
                await _quiet_send(transport, rpc_error(init.request_id, -32600, "Second reconnect", "RECONNECT_LIMIT"))
                await _quiet_close(transport, CLOSE_POLICY, "reconnect limit")
                return
            call.reconnects += 1
            old = self.transports.get(init.context_id)
            call.attach(transport, init, ctx)
            if old is not None and old is not transport:
                await _quiet_close(old, CLOSE_NORMAL, "replaced")
            log.event("a2a_reconnected", call=log.call_tag(init.context_id))
            is_new = False
        else:
            if self.draining:
                await _quiet_send(transport, rpc_error(init.request_id, -32000, "Draining", "UNAVAILABLE"))
                await _quiet_close(transport, CLOSE_RESTART, "draining")
                return
            clock = self.clock_factory()
            call = ConnectA2ACall(clock, self.cfg, init, ctx, model=self.model)
            call.attach(transport)
            self.sessions[init.context_id] = call
            is_new = True
            log.event("a2a_session", call=log.call_tag(init.context_id), in_rate=init.in_rate, out_rate=init.out_rate,
                      tracing=init.tracing, history=init.history_len, has_dialed=bool(init.system))
        self.transports[init.context_id] = transport
        if not await _quiet_send(transport, init_response(init)):
            call.connection_lost(transport, NO_CLOSE_FRAME)
            return
        if is_new:
            self._new.put_nowait(call)
        code = NO_CLOSE_FRAME
        try:
            while True:
                raw = await transport.recv()
                try:
                    frame = json.loads(raw)
                except ValueError:
                    await _quiet_send(transport, rpc_error(None, -32700, "Parse error", "PARSE_ERROR"))
                    continue
                try:
                    await call.on_frame(frame)
                except ProtocolError as exc:
                    await _quiet_send(transport, rpc_error(frame.get("id"), exc.code, "Invalid request", exc.reason))
        except TransportClosed as exc:
            code = exc.code
        except Exception as exc:   # noqa: BLE001
            log.event("a2a_reader_failed", level="error", call=log.call_tag(init.context_id),
                      error=type(exc).__name__)
            await _quiet_close(transport, 1011, "internal error")
            code = 1011
        finally:
            if self.transports.get(init.context_id) is transport:
                self.transports.pop(init.context_id, None)
            call.connection_lost(transport, code)
            if call.closed:
                self.sessions.pop(init.context_id, None)

    async def shutdown(self, drain_ms: int, clock: Clock | None = None) -> None:
        """Deployment stop: refuse new contacts, let running calls end for
        up to drain_ms, then close the rest with 1012 (Connect reconnects
        once; the new task starts the call afresh, CONNECT-A2A.md)."""
        self.draining = True
        deadline = time.monotonic() + drain_ms / 1000
        while self.active and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        for transport in list(self.transports.values()):
            await _quiet_close(transport, CLOSE_RESTART, "service restart")
        self._new.put_nowait(None)


def _id(raw) -> object:
    try:
        return json.loads(raw).get("id")
    except Exception:   # noqa: BLE001
        return None


async def _quiet_send(transport: Transport, frame: dict) -> bool:
    try:
        await transport.send(json.dumps(frame, separators=(",", ":")))
        return True
    except Exception:   # noqa: BLE001
        return False


async def _quiet_close(transport: Transport, code: int, reason: str) -> None:
    try:
        await transport.close(code, reason)
    except Exception:   # noqa: BLE001
        pass


# --- the orchestrator hook ----------------------------------------------------------------------

def components_for_call(comps, adapter: ConnectA2ACall, clock: Clock):
    """The call's Components with its own clock and a speaker factory that
    hands the call's bus and every queued sentence's text to the adapter
    (the trace needs the spoken text; the orchestrator and speaker stay
    untouched)."""
    import dataclasses
    inner = comps.speaker_factory

    def speaker_factory(cfg, line, tts, clips, bus, clk, language, rng_seed):
        if inner is not None:
            speaker = inner(cfg, line, tts, clips, bus, clk, language, rng_seed)
        else:
            from voice_gateway.speak.speaker import Speaker
            speaker = Speaker(cfg, line, tts, clips, bus, clk, language=language, rng_seed=rng_seed)
        adapter.attach_bus(bus)
        say = speaker.say

        def tapped(text, language, **kw):
            item_id = say(text, language, **kw)
            adapter.note_text(item_id, text)
            return item_id

        speaker.say = tapped
        return speaker

    return dataclasses.replace(comps, clock=clock, speaker_factory=speaker_factory)


# --- WebSocket server ---------------------------------------------------------------------------

class _WsTransport:
    def __init__(self, ws) -> None:
        self.ws = ws

    async def recv(self) -> str:
        from websockets.exceptions import ConnectionClosed
        try:
            msg = await self.ws.recv()
        except ConnectionClosed as exc:
            raise TransportClosed(exc.rcvd.code if exc.rcvd is not None else NO_CLOSE_FRAME) from None
        return msg if isinstance(msg, str) else msg.decode("utf-8")

    async def send(self, text: str) -> None:
        await self.ws.send(text)

    async def close(self, code: int, reason: str = "") -> None:
        await self.ws.close(code, reason)


def api_key_from_env() -> str:
    """VG_A2A_API_KEY (ECS injects it from the Secrets Manager secret that
    Connect reads), else VG_A2A_API_KEY_SECRET_ARN fetched once. Never logged."""
    key = os.environ.get("VG_A2A_API_KEY", "").strip()
    arn = os.environ.get("VG_A2A_API_KEY_SECRET_ARN", "").strip()
    if not key and arn:
        import boto3
        region = arn.split(":")[3] if arn.count(":") >= 6 else None
        key = boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=arn)["SecretString"].strip()
    return key


async def serve(endpoint: A2AEndpoint, host: str, port: int, health_body: Callable[[], dict] | None = None,
                ready: asyncio.Event | None = None, stop: asyncio.Event | None = None) -> None:
    """The wss endpoint behind the ALB (TLS ends at the ALB; the task speaks
    ws on `port`). GET /health answers on the same port."""
    from websockets.asyncio.server import serve as ws_serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    def lower(request) -> dict:
        return {k.lower(): v for k, v in request.headers.raw_items()}

    def process_request(connection, request):
        path = request.path.split("?")[0]
        if path == "/health":
            ok = not endpoint.draining
            body = json.dumps((health_body() if health_body else {}) | {"status": "ok" if ok else "draining"}).encode()
            return Response(200 if ok else 503, "OK" if ok else "Service Unavailable",
                            Headers([("Content-Type", "application/json"), ("Content-Length", str(len(body)))]), body)
        headers = lower(request)
        status = endpoint.upgrade_status(path, headers)
        if status is not None:
            # Names only, never values: tells Connect (A2A-Extensions, traceparent) from internet scanners.
            log.event("a2a_upgrade_refused", status=status, path=path[:64],
                      headers=sorted(h[:40] for h in headers)[:24], connect="a2a-extensions" in headers)
            return connection.respond(status, "")
        return None

    def process_response(connection, request, response):
        for k, v in echo_headers(lower(request)).items():
            response.headers[k] = v
        return response

    async def handler(ws) -> None:
        await endpoint.handle(_WsTransport(ws), lower(ws.request))

    async with ws_serve(handler, host, port, process_request=process_request, process_response=process_response,
                        max_size=endpoint.cfg.max_inbound_bytes, compression=None, ping_interval=20,
                        ping_timeout=20) as server:
        endpoint.bound_port = server.sockets[0].getsockname()[1] if server.sockets else port
        log.event("a2a_listening", port=endpoint.bound_port)
        if ready is not None:
            ready.set()
        await (stop.wait() if stop is not None else asyncio.Future())
