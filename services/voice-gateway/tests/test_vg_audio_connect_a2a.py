"""Amazon Connect A2A audio adapter (audio/connect_a2a.py) against a fake
Connect client: the session, caller audio in, audio artifacts out, barge-in,
FINISH, reconnect, fragments, traces, and one whole call through the
orchestrator. Pure Python at 16 kHz (root CI); the 8 kHz path and the real
WebSocket server skip without numpy or websockets."""

import asyncio
import base64
import json

import pytest

from test_vg_listen_common import run, silence_pcm, tone
from voice_gateway.audio import connect_a2a as a2a
from voice_gateway.audio import frames as fr
from voice_gateway.audio.adapter import TelephonyAdapter
from voice_gateway.audio.frames import Frame

KEY = "test-key-not-a-secret"
TRACEPARENT = "00-550e8400e29b41d4a716446655440000-6ba7b8109dad11d1-01"
HEADERS = {"authorization": f"Bearer {KEY}", "a2a-extensions": a2a.EXT, "traceparent": TRACEPARENT,
           "tracestate": "connect=parentSpanId:00000000-0000-0000-0000-000000000000"}


# --- the fake Connect client -------------------------------------------------------------------

class FakeTransport:
    """One WebSocket as Connect's side sees it: `inbox` is what Connect sends us."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed: int | None = None

    async def recv(self) -> str:
        item = await self.inbox.get()
        if isinstance(item, int):
            raise a2a.TransportClosed(item)
        return item

    async def send(self, text: str) -> None:
        if self.closed is not None:
            raise ConnectionError("closed")
        self.sent.append(json.loads(text))

    async def close(self, code: int, reason: str = "") -> None:
        if self.closed is None:
            self.closed = code
            self.inbox.put_nowait(code)


def init_frame(context="ctx-1", in_rate=16000, out_rate=16000, finish=("COMPLETE", "ESCALATE"), tracing=True,
               rid="1", audio=True, system="+46100000001", customer="+46700000000"):
    init = {"instanceArn": "arn:aws:connect:eu-central-1:111122223333:instance/i-1",
            "contactArn": "arn:aws:connect:eu-central-1:111122223333:instance/i-1/contact/c-42",
            "supportedFinishTypes": list(finish), "subscribeToTracingEvents": tracing, "history": [],
            "contactContext": {"customerAddress": customer, "systemAddress": system, "channel": "VOICE"}}
    if audio:
        init["audioInputConfiguration"] = {"encoding": "LINEAR_PCM", "sampleRateHertz": in_rate, "sampleSizeBits": 16,
                                           "channelCount": 1}
        init["audioOutputConfiguration"] = {"encoding": "LINEAR_PCM", "sampleRateHertz": out_rate,
                                            "sampleSizeBits": 16, "channelCount": 1}
    return {"jsonrpc": "2.0", "method": "SendMessage", "id": rid, "params": {"message": {
        "messageId": "m-1", "contextId": context, "role": "ROLE_USER",
        "parts": [{"data": {"initSession": init}, "mediaType": "application/json"}],
        "metadata": {a2a.EVENT_TYPE: "INIT_SESSION"}}}}


def request(context, data, kind, rid="r"):
    return {"jsonrpc": "2.0", "method": "SendMessage", "id": rid, "params": {"message": {
        "messageId": "m", "contextId": context, "role": "ROLE_USER",
        "parts": [{"data": data, "mediaType": "application/json"}], "metadata": {a2a.EVENT_TYPE: kind}}}}


class FakeConnect:
    """Connect Customer's side of one contact (possibly over two sockets)."""

    def __init__(self, endpoint: a2a.A2AEndpoint, context="ctx-1") -> None:
        self.endpoint, self.context = endpoint, context
        self.t: FakeTransport | None = None
        self.sockets: list[FakeTransport] = []
        self.readers: list[asyncio.Task] = []
        self._n = 0

    async def connect(self, **kw) -> FakeTransport:
        self.t = FakeTransport()
        self.sockets.append(self.t)
        self.readers.append(asyncio.ensure_future(self.endpoint.handle(self.t, dict(HEADERS))))
        self.t.inbox.put_nowait(json.dumps(init_frame(self.context, **kw)))
        for _ in range(20):
            await asyncio.sleep(0)
        return self.t

    def send(self, frame: dict) -> None:
        self.t.inbox.put_nowait(json.dumps(frame))

    def ready(self) -> None:
        self.send(request(self.context, {"channelState": {"state": "READY"}}, "CHANNEL_STATE", "cs-1"))

    def audio(self, pcm: bytes) -> None:
        self._n += 1
        self.send({"jsonrpc": "2.0", "method": "SendMessage", "id": f"a-{self._n}", "params": {"message": {
            "messageId": f"m-a{self._n}", "contextId": self.context, "role": "ROLE_USER",
            "parts": [{"raw": base64.b64encode(pcm).decode(), "mediaType": "audio/lpcm"}], "metadata": {}}}})

    def drop(self, code=1006) -> None:
        self.t.inbox.put_nowait(code)

    @property
    def sent(self) -> list[dict]:
        return [f for s in self.sockets for f in s.sent]


def kind_of(frame: dict) -> str:
    if "error" in frame:
        return "RPC_ERROR"
    result = frame["result"]
    if "statusUpdate" in result:
        msg = result["statusUpdate"]["status"].get("message")
        return "STATUS+FINISH" if msg and msg["metadata"][a2a.EVENT_TYPE] == "FINISH" else "STATUS"
    if "artifactUpdate" in result:
        return result["artifactUpdate"]["artifact"]["metadata"][a2a.EVENT_TYPE]
    return result["message"]["metadata"][a2a.EVENT_TYPE]


def check_protocol(frames: list[dict]) -> None:
    """The rules Connect enforces on what we send (Audio streaming, Turn lifecycle, Finishing)."""
    open_artifact = None          # (task, artifact)
    traced: set = set()
    done: set = set()
    finished = False
    for f in frames:
        assert not finished, "nothing may follow FINISH"
        k = kind_of(f)
        if k == "AUDIO_RESPONSE_CHUNK":
            u = f["result"]["artifactUpdate"]
            key = (u["taskId"], u["artifact"]["artifactId"])
            assert u["taskId"] not in done, "audio on a closed task"
            if open_artifact is None:
                assert u["append"] is False, "the first chunk of an artifact sets append false"
                open_artifact = key
            else:
                assert key == open_artifact, "a second audio artifact while one is open"
                assert u["append"] is True
            raw = base64.b64decode(u["artifact"]["parts"][0]["raw"])
            assert raw and len(json.dumps(f)) < 60000
            if u["lastChunk"]:
                open_artifact = None
        elif k == "INTERRUPTION":
            open_artifact = None                       # the cut artifact ends without lastChunk
        elif k == "TRACING_SPAN":
            u = f["result"]["artifactUpdate"]
            assert u["taskId"] not in traced, "exactly one TRACING_SPAN per task"
            assert open_artifact is None, "the trace comes after the response"
            traced.add(u["taskId"])
        elif k in ("STATUS", "STATUS+FINISH"):
            s = f["result"]["statusUpdate"]
            assert s["taskId"] in traced, "the trace comes before the terminal status"
            assert s["status"]["state"] == "TASK_STATE_COMPLETED"
            assert open_artifact is None
            done.add(s["taskId"])
            finished = k == "STATUS+FINISH"
        elif k == "FINISH":
            assert open_artifact is None
            finished = True
    assert open_artifact is None or not finished


def endpoint(clock, **cfg) -> a2a.A2AEndpoint:
    return a2a.A2AEndpoint(lambda: clock, KEY, a2a.A2AConfig(**cfg), model="eu.test-model")


async def started(clock, **kw):
    ep = endpoint(clock, **{k: v for k, v in kw.items() if k in a2a.A2AConfig.__dataclass_fields__})
    connect = FakeConnect(ep)
    await connect.connect(**{k: v for k, v in kw.items() if k not in a2a.A2AConfig.__dataclass_fields__})
    call = await asyncio.wait_for(ep.calls().__anext__(), 1)
    connect.ready()
    info = await call.accept()
    return ep, connect, call, info


async def play_ms(call, clock, ms, start=0):
    """The mixer's pace: one 20 ms frame per 20 ms, 60 ms ahead."""
    for i in range(ms // 20):
        await call.play(Frame(tone(300, 20), start + i * 20))
        if i >= 3:
            await clock.sleep_ms(20)


# --- auth and handshake -------------------------------------------------------------------------

def test_bearer_token_is_checked_in_constant_time_terms():
    assert a2a.check_auth({"authorization": f"Bearer {KEY}"}, KEY) is None
    assert a2a.check_auth({}, KEY) == 401
    assert a2a.check_auth({"authorization": KEY}, KEY) == 401
    assert a2a.check_auth({"authorization": "Bearer wrong"}, KEY) == 403
    assert a2a.echo_headers({"a2a-extensions": a2a.EXT}) == {"A2A-Extensions": a2a.EXT}
    assert a2a.echo_headers({}) == {}


def test_upgrade_refused_without_key_while_draining_and_over_capacity():
    ep = a2a.A2AEndpoint(lambda: None, KEY, a2a.A2AConfig(max_calls=0))
    assert ep.upgrade_status("/", {}) == 401
    assert ep.upgrade_status("/", {"authorization": "Bearer x"}) == 403
    assert ep.upgrade_status("/", HEADERS) == 503                   # max_calls 0
    ep = a2a.A2AEndpoint(lambda: None, KEY)
    assert ep.upgrade_status("/", HEADERS) is None
    ep.draining = True
    assert ep.upgrade_status("/", HEADERS) == 503
    with pytest.raises(ValueError):
        a2a.A2AEndpoint(lambda: None, "")


def test_trace_context_adopts_traceparent_or_starts_a_new_trace():
    ctx = a2a.TraceContext.from_headers(HEADERS)
    assert (ctx.trace_id, ctx.parent_span_id) == ("550e8400e29b41d4a716446655440000", "6ba7b8109dad11d1")
    fresh = a2a.TraceContext.from_headers({"traceparent": "garbage"})
    assert len(fresh.trace_id) == 32 and fresh.parent_span_id is None


# --- session ------------------------------------------------------------------------------------

def test_init_session_response_echoes_id_and_counter_proposes_16k_then_waits_for_ready():
    async def body(clock):
        ep = endpoint(clock)
        connect = FakeConnect(ep)
        t = await connect.connect(in_rate=8000, out_rate=16000, rid="init-7")
        reply = t.sent[0]
        assert reply["id"] == "init-7" and kind_of(reply) == "INIT_SESSION_RESPONSE"
        msg = reply["result"]["message"]
        assert msg["contextId"] == "ctx-1" and msg["extensions"] == [a2a.EXT] and msg["role"] == "ROLE_AGENT"
        body_ = msg["parts"][0]["data"]["initSessionResponse"]
        assert body_["statusCode"] == "200"
        assert body_["audioInputConfiguration"]["sampleRateHertz"] == 16000      # counter-proposal (full declaration)
        assert "audioOutputConfiguration" not in body_                         # 16 kHz proposed: accepted
        call = await ep.calls().__anext__()
        assert isinstance(call, TelephonyAdapter)
        accepting = asyncio.ensure_future(call.accept())
        await clock.sleep_ms(500)
        assert not accepting.done()                                            # no output before READY
        await call.play(Frame(tone(300, 20), 0))
        assert len(t.sent) == 1 and call.dropped_out_ms == 20
        connect.ready()
        info = await accepting
        assert (info.call_ref, info.dialed, info.caller) == ("c-42", "+46100000001", "+46700000000")
        assert info.wire_format.sample_rate == 16000
    run(body)


def test_init_without_audio_or_with_a_bad_format_is_refused():
    async def body(clock):
        ep = endpoint(clock)
        for kw, reason in (({"audio": False}, "AUDIO_REQUIRED"), ({"in_rate": 44100}, "UNSUPPORTED_AUDIO_CONFIGURATION")):
            t = await FakeConnect(ep).connect(**kw)
            assert t.sent[0]["error"]["data"]["reason"] == reason
            assert t.sent[0]["error"]["data"]["@type"] == "type.googleapis.com/google.rpc.ErrorInfo"
            assert t.closed == a2a.CLOSE_POLICY
        ep2 = endpoint(clock, allowed_instance_arns=("arn:aws:connect:eu-central-1:999:instance/other",))
        t = await FakeConnect(ep2).connect()
        assert t.sent[0]["error"]["data"]["reason"] == "INSTANCE_NOT_ALLOWED"
        assert ep.active == 0
    run(body)


def test_ready_timeout_fails_the_collaboration_without_output():
    async def body(clock):
        ep = endpoint(clock, ready_timeout_ms=1000)
        connect = FakeConnect(ep)
        t = await connect.connect()
        call = await ep.calls().__anext__()
        with pytest.raises(TimeoutError):
            await call.accept()
        await call.hangup("error")                     # what the orchestrator does after a failed accept
        assert t.closed == 1011 and call.closed and len(t.sent) == 1
    run(body)


# --- caller audio in ----------------------------------------------------------------------------

def test_caller_audio_becomes_continuous_20ms_frames_and_gaps_are_concealed():
    async def body(clock):
        ep, connect, call, _ = await started(clock)
        got = []

        async def collect():
            async for f in call.caller_frames():
                got.append(f)
        reader = asyncio.ensure_future(collect())
        speech = tone(300, 90)                           # chunks not on 20 ms borders: 30 ms each
        for i in range(3):
            connect.audio(speech[i * 960:(i + 1) * 960])
        await clock.sleep_ms(10)
        assert [f.ts_ms - got[0].ts_ms for f in got] == [0, 20, 40, 60]
        assert b"".join(f.pcm for f in got) == speech[:4 * 640]
        await clock.sleep_ms(400)                        # Connect sends nothing: silence keeps the stream going
        ts = [f.ts_ms for f in got]
        assert ts == list(range(ts[0], ts[0] + 20 * len(ts), 20)) and len(got) > 10
        assert all(f.pcm == bytes(640) for f in got[4:])
        await call.hangup("end_call")
        await asyncio.wait_for(reader, 1)                # the stream ends with the call
    run(body)


def test_8k_caller_audio_is_resampled_when_connect_keeps_its_rate():
    pytest.importorskip("numpy")

    async def body(clock):
        ep, connect, call, info = await started(clock, in_rate=8000, counter_propose=False)
        assert info.wire_format.sample_rate == 8000
        got = []

        async def collect():
            async for f in call.caller_frames():
                got.append(f)
        reader = asyncio.ensure_future(collect())
        connect.audio(bytes(320) * 5)                    # 100 ms at 8 kHz
        await clock.sleep_ms(10)
        assert len(got) == 5 and all(len(f.pcm) == 640 for f in got)
        await call.hangup("end_call")
        await reader
    run(body)


# --- assistant audio out ------------------------------------------------------------------------

def test_one_response_is_one_artifact_then_trace_then_terminal_status():
    async def body(clock):
        ep, connect, call, _ = await started(clock)
        await play_ms(call, clock, 400)
        await clock.sleep_ms(100)                        # a short gap inside the response is bridged
        await play_ms(call, clock, 200, start=500)
        await clock.sleep_ms(1000)                       # then the response ends
        frames = connect.sent[1:]
        kinds = [kind_of(f) for f in frames]
        assert kinds[-2:] == ["TRACING_SPAN", "STATUS"]
        audio = [f["result"]["artifactUpdate"] for f in frames if kind_of(f) == "AUDIO_RESPONSE_CHUNK"]
        assert len({a["artifact"]["artifactId"] for a in audio}) == 1 and len({a["taskId"] for a in audio}) == 1
        assert audio[0]["append"] is False and all(a["append"] for a in audio[1:])
        assert audio[-1]["lastChunk"] is True and not any(a["lastChunk"] for a in audio[:-1])
        assert len(audio) >= 31                          # 30 frames of tone, the bridge, the closing chunk
        check_protocol(frames)
        await play_ms(call, clock, 100, start=3000)      # the next response: a fresh task and artifact
        await clock.sleep_ms(1000)
        tasks = [f["result"]["statusUpdate"]["taskId"] for f in connect.sent if kind_of(f) == "STATUS"]
        assert len(tasks) == 2 and tasks[0] != tasks[1]
        check_protocol(connect.sent[1:])
    run(body)


def test_barge_in_sends_interruption_first_and_no_last_chunk():
    async def body(clock):
        ep, connect, call, _ = await started(clock)
        await play_ms(call, clock, 300)
        dropped = await call.clear()
        assert dropped > 0
        frames = connect.sent[1:]
        kinds = [kind_of(f) for f in frames]
        i = kinds.index("INTERRUPTION")
        assert kinds[i:] == ["INTERRUPTION", "TRACING_SPAN", "STATUS"]
        audio = [f["result"]["artifactUpdate"] for f in frames if kind_of(f) == "AUDIO_RESPONSE_CHUNK"]
        assert not any(a["lastChunk"] for a in audio)
        intr = frames[i]["result"]["message"]
        assert intr["parts"][0]["data"]["interruption"]["type"] == "USER_AUDIO_INPUT"
        assert await call.clear() == 0 and len(connect.sent) - 1 == len(frames)   # nothing playing: nothing sent
        await play_ms(call, clock, 100, start=1000)
        await clock.sleep_ms(1000)
        check_protocol(connect.sent[1:])
        ids = {f["result"]["artifactUpdate"]["artifact"]["artifactId"] for f in connect.sent[1:]
               if kind_of(f) == "AUDIO_RESPONSE_CHUNK"}
        assert len(ids) == 2
    run(body)


# --- finish -------------------------------------------------------------------------------------

def test_hangup_waits_for_the_goodbye_then_finish_complete_and_nothing_after():
    async def body(clock):
        ep, connect, call, _ = await started(clock)
        await play_ms(call, clock, 200)
        before = clock.now_ms()
        await call.hangup("end_call")
        assert clock.now_ms() >= before + 40             # the queued audio played first
        frames = connect.sent[1:]
        assert kind_of(frames[-1]) == "STATUS+FINISH"
        finish = frames[-1]["result"]["statusUpdate"]["status"]["message"]["parts"][0]["data"]["finish"]
        assert finish == {"type": "COMPLETE", "reason": "end_call"}
        check_protocol(frames)
        assert call.closed
        await call.play(Frame(tone(300, 20), 0))
        await call.hangup("again")
        assert len(connect.sent) - 1 == len(frames)
        await clock.sleep_ms(3000)
        assert connect.t.closed == 1000                  # we close after Connect had its chance
    run(body)


def test_finish_without_an_open_task_is_a_message_and_errors_escalate():
    async def body(clock):
        ep, connect, call, _ = await started(clock)
        await call.hangup("error")
        f = connect.sent[-1]
        assert kind_of(f) == "FINISH"
        assert f["result"]["message"]["parts"][0]["data"]["finish"]["type"] == "ESCALATE"
    run(body)


def test_transfer_is_finish_escalate_or_refused_when_connect_cannot_route_it():
    async def body(clock):
        ep, connect, call, _ = await started(clock)
        out = await call.transfer("+46812345678", 25)
        assert out.answered and out.reason == "escalated_to_flow"
        f = connect.sent[-1]["result"]["message"]["parts"][0]["data"]["finish"]
        assert f["type"] == "ESCALATE" and "+46" not in json.dumps(f)        # the number never travels in the reason
        ep2, connect2, call2, _ = await started(clock, finish=("COMPLETE",))
        out = await call2.transfer("+46812345678", 25)
        assert not out.answered and out.reason == "escalate_unsupported" and not call2.closed
        await call2.hangup("error")                      # ESCALATE not offered: COMPLETE
        assert connect2.sent[-1]["result"]["message"]["parts"][0]["data"]["finish"]["type"] == "COMPLETE"
    run(body)


# --- reconnect ----------------------------------------------------------------------------------

def test_one_reconnect_reattaches_the_running_call_and_a_second_drop_ends_it():
    async def body(clock):
        ep, connect, call, _ = await started(clock)
        await play_ms(call, clock, 100)
        connect.drop(1006)
        await clock.sleep_ms(500)
        assert not call.closed and not call.connected
        await call.play(Frame(tone(300, 20), 0))         # nothing goes out while the socket is down
        t2 = await connect.connect()                    # Connect's reconnect: new INIT_SESSION, same contextId
        assert kind_of(t2.sent[0]) == "INIT_SESSION_RESPONSE"
        assert call.reconnects == 1 and call.connected
        await call.play(Frame(tone(300, 20), 0))
        assert len(t2.sent) == 1                         # READY again before any output
        connect.ready()
        await clock.sleep_ms(1)
        await play_ms(call, clock, 100, start=2000)
        await clock.sleep_ms(1000)
        check_protocol(t2.sent[1:])
        assert any(kind_of(f) == "AUDIO_RESPONSE_CHUNK" for f in t2.sent)
        with pytest.raises(StopAsyncIteration):          # no second call was started for the reconnect
            await asyncio.wait_for(_next_or_stop(ep), 0.05)
        connect.drop(1006)
        await clock.sleep_ms(10)
        assert call.closed                               # one reconnect per call
    run(body)


async def _next_or_stop(ep):
    ep._new.put_nowait(None)
    return await ep.calls().__anext__()


def test_normal_close_ends_the_call_and_a_missing_reconnect_times_out():
    async def body(clock):
        ep, connect, call, _ = await started(clock)
        connect.drop(1000)                               # caller hung up: Connect ends the collaboration
        await clock.sleep_ms(10)
        assert call.closed
        ep2, connect2, call2, _ = await started(clock, reconnect_wait_ms=2000)
        connect2.drop(1012)
        await clock.sleep_ms(1000)
        assert not call2.closed
        await clock.sleep_ms(1500)
        assert call2.closed
    run(body)


# --- fragments, errors, other events -----------------------------------------------------------

def test_fragmented_init_session_is_reassembled_and_large_traces_are_fragmented():
    async def body(clock):
        ep = endpoint(clock)
        raw = base64.b64encode(json.dumps(init_frame("ctx-frag")).encode()).decode()
        parts = [raw[i:i + 100] for i in range(0, len(raw), 100)]
        t = FakeTransport()
        handler = asyncio.ensure_future(ep.handle(t, dict(HEADERS)))
        for n, p in enumerate(parts):
            t.inbox.put_nowait(json.dumps(request("ctx-frag", {"fragment": {
                "fragmentId": "f-1", "partNumber": n, "totalParts": len(parts), "payload": p, "encoding": "base64"}},
                "FRAGMENT", rid=f"fr-{n}")))
        await clock.sleep_ms(10)
        assert kind_of(t.sent[0]) == "INIT_SESSION_RESPONSE"
        frames = a2a.fragment_frames({"jsonrpc": "2.0", "id": "x", "result": {"message": {"blob": "a" * 5000}}}, 2048)
        assert len(frames) > 2
        decoded = [json.loads(f)["result"]["message"]["parts"][0]["data"]["fragment"] for f in frames]
        reasm = a2a.Reassembler(1 << 20, 1000)
        out = [reasm.add(d, 0) for d in decoded]
        assert out[-1]["result"]["message"]["blob"] == "a" * 5000 and out[0] is None
        handler.cancel()
    run(body)


def test_unknown_method_gets_a_json_rpc_error_and_dtmf_errors_are_ignored():
    async def body(clock):
        ep, connect, call, _ = await started(clock)
        connect.send({"jsonrpc": "2.0", "method": "GetTask", "id": "q1", "params": {}})
        connect.send(request("ctx-1", {"dtmfInputComplete": {"inputString": "1", "terminationReason": "TIMEOUT"}},
                             "DTMF_INPUT_COMPLETE"))
        connect.send(request("ctx-1", {"error": {"errorCode": 400, "errorCategory": "BAD_REQUEST"}}, "ERROR"))
        connect.t.inbox.put_nowait("not json")
        await clock.sleep_ms(10)
        errs = [f for f in connect.sent if "error" in f]
        assert errs[0]["id"] == "q1" and errs[0]["error"]["code"] == -32601
        assert errs[1]["error"]["code"] == -32700 and not call.closed
    run(body)


# --- traces ---------------------------------------------------------------------------------------

def test_trace_carries_input_output_tools_and_segments_under_connects_parent():
    from voice_gateway.events import (AgentSentence, EventBus, PlaybackFinished, PlaybackStarted, ToolFinished,
                                      ToolStarted, TurnEnd)

    async def body(clock):
        ep, connect, call, _ = await started(clock)
        bus = EventBus()
        call.attach_bus(bus)
        bus.publish(TurnEnd(clock.now_ms(), "Ett bord för fyra i morgon", "sv", 0, clock.now_ms(), "silence"))
        bus.publish(ToolStarted(clock.now_ms(), 1, "check_availability"))
        await clock.sleep_ms(300)
        bus.publish(ToolFinished(clock.now_ms(), 1, "check_availability", True, 300))
        bus.publish(AgentSentence(clock.now_ms(), 1, 0, "Det finns plats klockan sju.", "sv"))
        await call.play(Frame(tone(300, 20), clock.now_ms()))
        bus.publish(PlaybackStarted(clock.now_ms(), "t1-s0", "assistant", "sv", 400))
        call.note_text("t1-s901", "Vill du boka?")
        bus.publish(PlaybackStarted(clock.now_ms(), "t1-s901", "fixed", "sv", 300))
        bus.publish(PlaybackStarted(clock.now_ms(), "bc:sv:mm#1", "backchannel", "sv", 300))
        await play_ms(call, clock, 300, start=clock.now_ms())
        bus.publish(PlaybackFinished(clock.now_ms(), "t1-s0", "assistant", 300, False))
        await clock.sleep_ms(1000)
        trace = next(f for f in connect.sent if kind_of(f) == "TRACING_SPAN")
        update = trace["result"]["artifactUpdate"]
        status = next(f for f in connect.sent if kind_of(f) == "STATUS")["result"]["statusUpdate"]
        assert update["taskId"] == status["taskId"] and update["lastChunk"] is True
        spans = update["artifact"]["parts"][0]["data"]["tracingSpan"]["otlp"]["resourceSpans"][0]["scopeSpans"][0][
            "spans"]
        root = spans[0]
        assert root["name"] == "invoke_agent booqy-voice-gateway" and root["kind"] == 2
        assert root["traceId"] == "550e8400e29b41d4a716446655440000" and root["parentSpanId"] == "6ba7b8109dad11d1"
        assert all(s["traceId"] == root["traceId"] and int(s["endTimeUnixNano"]) >= int(s["startTimeUnixNano"]) > 0
                   for s in spans)
        assert all(s["parentSpanId"] == root["spanId"] for s in spans[1:])

        def content(ev):
            return {a["key"].rsplit(".", 1)[-1]: list(a["value"].values())[0] for a in ev["attributes"]}
        events = [content(e) for e in root["events"]]
        assert events[0] == {"role": "input", "type": "TEXT_MESSAGE", "participant": "user",
                             "content": "Ett bord för fyra i morgon"}
        assert events[1]["role"] == "output" and events[1]["content"] == "Det finns plats klockan sju. Vill du boka?"
        tool = next(s for s in spans if s["name"] == "execute_tool check_availability")
        assert [content(e)["role"] for e in tool["events"]] == ["input", "output"]
        assert sum(s["name"].startswith("chat sentence-") for s in spans) == 3
        attrs = {a["key"] for a in root["attributes"]}
        assert {"gen_ai.agent.name", "gen_ai.operation.name", "gen_ai.request.model"} <= attrs
        # the caller's text is used once: the next response has no input event
        await play_ms(call, clock, 100, start=clock.now_ms())
        await clock.sleep_ms(1000)
        trace2 = [f for f in connect.sent if kind_of(f) == "TRACING_SPAN"][1]
        root2 = trace2["result"]["artifactUpdate"]["artifact"]["parts"][0]["data"]["tracingSpan"]["otlp"][
            "resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert [content(e)["role"] for e in root2["events"]] == ["output"]
    run(body)


def test_no_trace_when_connect_did_not_subscribe():
    async def body(clock):
        ep, connect, call, _ = await started(clock, tracing=False)
        await play_ms(call, clock, 100)
        await clock.sleep_ms(1000)
        assert "TRACING_SPAN" not in [kind_of(f) for f in connect.sent]
        assert "STATUS" in [kind_of(f) for f in connect.sent]
    run(body)


# --- a whole call through the orchestrator ------------------------------------------------------

def test_whole_call_greeting_turn_reply_and_finish_over_the_fake_connect_line():
    from voice_gateway.brain.scripted import GREETING, Reply, factory
    from voice_gateway.config import GatewayConfig
    from voice_gateway.events import ProviderAction
    from voice_gateway.orchestrator import CallOrchestrator
    from voice_gateway.server import components

    async def body(clock):
        cfg = GatewayConfig.from_json(json.dumps({"vad": {"model": "energy"}, "backchannel": {"enabled": False}}))
        ep, connect, call, _ = await started(clock)
        comps = components(cfg, clock, fake=True, transcript="Jag vill boka ett bord.")
        comps.brain_factory = factory([Reply("Tack för samtalet, hej då.", actions=(ProviderAction("end_call"),))])
        orch = CallOrchestrator(cfg, call, a2a.components_for_call(comps, call, clock))
        running = asyncio.ensure_future(orch.run())

        async def caller():
            for _ in range(40):                          # 0.8 s of line noise floor while the greeting plays
                connect.audio(silence_pcm(20))
                await clock.sleep_ms(20)
            while not any(kind_of(f) == "STATUS" for f in connect.sent):
                connect.audio(silence_pcm(20))
                await clock.sleep_ms(20)
            speech = tone(220, 1200)
            for i in range(0, len(speech), 640):
                connect.audio(speech[i:i + 640])
                await clock.sleep_ms(20)
            while not call.closed:
                connect.audio(silence_pcm(20))
                await clock.sleep_ms(20)
        acting = asyncio.ensure_future(caller())
        summary = await asyncio.wait_for(running, 15)
        acting.cancel()
        assert summary.end_reason == "agent_end"
        frames = connect.sent[1:]
        check_protocol(frames)
        assert kind_of(frames[-1]) in ("STATUS+FINISH", "FINISH")
        traces = [f["result"]["artifactUpdate"]["artifact"]["parts"][0]["data"]["tracingSpan"]["otlp"]
                  ["resourceSpans"][0]["scopeSpans"][0]["spans"][0] for f in frames if kind_of(f) == "TRACING_SPAN"]
        texts = [[{a["key"].rsplit(".", 1)[-1]: list(a["value"].values())[0] for a in e["attributes"]}
                  for e in root["events"]] for root in traces]
        assert any(ev["role"] == "output" and "Du pratar med vår digitala assistent" in ev["content"]
                   for t in texts for ev in t)
        assert any(ev["role"] == "input" and ev["content"] == "Jag vill boka ett bord." for t in texts for ev in t)
        assert any(ev["role"] == "output" and "hej då" in ev["content"] for t in texts for ev in t)
        assert GREETING.startswith("Hej och välkommen till")
    run(body)


# --- the real WebSocket server ----------------------------------------------------------------------

def test_websocket_server_checks_the_key_echoes_the_extension_and_answers_health():
    pytest.importorskip("websockets")
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus
    from voice_gateway.clock import RealClock

    async def main():
        ep = a2a.A2AEndpoint(RealClock, KEY)
        ready, stop = asyncio.Event(), asyncio.Event()
        server = asyncio.ensure_future(a2a.serve(ep, "127.0.0.1", 0, health_body=lambda: {"phoneAdapter": "x"},
                                                 ready=ready, stop=stop))
        await asyncio.wait_for(ready.wait(), 5)
        url = f"ws://127.0.0.1:{ep.bound_port}/"
        with pytest.raises(InvalidStatus) as err:
            async with connect(url):
                pass
        assert err.value.response.status_code == 401
        with pytest.raises(InvalidStatus) as err:
            async with connect(url, additional_headers={"Authorization": "Bearer nope"}):
                pass
        assert err.value.response.status_code == 403
        async with connect(url, additional_headers={"Authorization": f"Bearer {KEY}", "A2A-Extensions": a2a.EXT,
                                                    "traceparent": TRACEPARENT}) as ws:
            assert ws.response.headers["A2A-Extensions"] == a2a.EXT
            await ws.send(json.dumps(init_frame("ctx-ws")))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert kind_of(reply) == "INIT_SESSION_RESPONSE"
            call = await asyncio.wait_for(ep.calls().__anext__(), 5)
            assert call.context_id == "ctx-ws"
        reader, writer = await asyncio.open_connection("127.0.0.1", ep.bound_port)
        writer.write(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        head = await asyncio.wait_for(reader.read(4096), 5)
        assert b" 200 " in head.split(b"\r\n")[0] and b'"status": "ok"' in head
        writer.close()
        stop.set()
        await asyncio.wait_for(server, 5)

    asyncio.run(main())


def test_server_connect_mode_serves_health_and_drains_on_stop():
    pytest.importorskip("websockets")
    import argparse

    from voice_gateway import server
    from voice_gateway.clock import RealClock
    from voice_gateway.config import GatewayConfig

    async def main():
        ep = a2a.A2AEndpoint(RealClock, KEY)
        stop, ready = asyncio.Event(), asyncio.Event()
        args = argparse.Namespace(host="127.0.0.1", port=0, fake=True, drain_s=0, health_port=0)
        task = asyncio.ensure_future(server.run_connect(args, GatewayConfig(), stop=stop, ready=ready, endpoint=ep))
        await asyncio.wait_for(ready.wait(), 5)
        reader, writer = await asyncio.open_connection("127.0.0.1", ep.bound_port)
        writer.write(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        head = await asyncio.wait_for(reader.read(4096), 5)
        assert b'"phoneAdapter": "connect-a2a"' in head
        writer.close()
        stop.set()
        assert await asyncio.wait_for(task, 5) == 0 and ep.draining

    asyncio.run(main())
