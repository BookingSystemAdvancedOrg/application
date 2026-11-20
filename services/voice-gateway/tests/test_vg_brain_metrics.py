"""MetricsRecorder (metrics.py) on a hand-written event sequence: latency
from end of speech to first audio, backchannels and their overlaps,
fillers, barge-in detect and stop, language decisions, the outcome, the
EMF lines (dimension language only, no text, no ids); and server.py's
command line. Pure stdlib + the application's deps."""

from voice_gateway import log, server
from voice_gateway.events import (AgentSentence, AgentTurnDone, BargeIn, CallEnded, EventBus, LanguageAction,
                                  LanguageDecided, PlaybackFinished, PlaybackFlushed, PlaybackStarted, TurnEnd,
                                  TurnHold, VadStart)
from voice_gateway.metrics import MetricsRecorder, percentile


def call_events():
    return [
        VadStart(1100, 1000, 0.9),
        PlaybackStarted(3600, "b1", "backchannel", "sv", 300),            # "mm" in a pause
        PlaybackFinished(3900, "b1", "backchannel", 300, False),
        TurnHold(4000, "filler", 1600),
        PlaybackStarted(4200, "b2", "backchannel", "sv", 300),
        VadStart(4250, 4250, 0.9),                                       # caller spoke over it: an overlap
        PlaybackFinished(4500, "b2", "backchannel", 300, False),
        TurnEnd(6000, "jag vill boka", "sv", 1000, 5450, "silence"),
        PlaybackStarted(6800, "f1", "filler", "sv", 600),
        AgentSentence(7100, 1, 0, "x", "sv"),
        PlaybackStarted(7300, "a1", "assistant", "sv", 1500),
        BargeIn(7900, 7600, "duration"),
        PlaybackFlushed(8100, 400, 200),
        AgentTurnDone(8200, 1, "sv", 2, False, False, outcome="booked", tools=("get_availability",)),
        VadStart(7700, 7600, 0.9, during_playback=True),
        TurnEnd(9000, "nej", "en", 7600, 8600, "silence",
                language_decision=LanguageDecided(9000, LanguageAction.SWITCH, "en", "en", "strong", 1)),
        LanguageDecided(9000, LanguageAction.SWITCH, "en", "en", "strong", 1),
        AgentSentence(9500, 2, 0, "y", "en", "fixed"),
        PlaybackStarted(9700, "a2", "fixed", "en", 900),
        AgentTurnDone(9800, 2, "en", 1, False, False),
        CallEnded(12000, "hangup"),
    ]


def test_turn_and_call_metrics():
    bus = EventBus()
    rec = MetricsRecorder(bus, emit=False)
    bus.listen(rec.on_event)
    for event in call_events():
        bus.publish(event)
    s = rec.summary()
    first, second = s["turns"]
    assert first["eos_to_first_audio"] == 7300 - 5450 and first["turn_end_to_first_audio"] == 1300
    assert first["turn_detect"] == 550 and first["first_sentence"] == 1100 and first["tts_first_audio"] == 200
    assert (first["backchannels"], first["backchannel_overlaps"], first["first_backchannel"]) == (2, 1, 2600)
    assert first["filler_played"] and first["holds"] == 1
    assert (first["barge_in_detect"], first["barge_in_stop"]) == (300, 200)   # on the interrupted reply's turn
    assert first["rounds"] == 2 and first["tools"] == ["get_availability"]
    assert second["language"] == "en" and second["eos_to_first_audio"] == 1100 and second["barge_in_stop"] is None
    assert s["outcome"] == "booked" and s["barge_ins"] == 1 and s["language_switches"] == 1
    assert s["language_detections"] == [["switch", "en", "strong"]] and s["first_language"] == "en"
    assert (s["end_reason"], s["call_ms"]) == ("hangup", 12000)
    assert s["eos_to_first_audio_p50"] in (1100, 1850) and s["barge_in_stop_max"] == 200


def test_emf_lines_carry_only_low_cardinality_dimensions():
    bus = EventBus()
    rec = MetricsRecorder(bus)
    bus.listen(rec.on_event)
    log.sink = []
    try:
        for event in call_events():
            bus.publish(event)
        lines = list(log.sink)
    finally:
        log.sink = None
    names = {k for line in lines for k in line if k not in ("_aws", "language", "action", "reason", "outcome")}
    assert {"EosToFirstAudio", "TurnDetectMs", "BargeInStopMs", "BackchannelOverlaps", "FillerPlayed",
            "LanguageSwitch", "CallMs", "BargeIns", "Outcome"} <= names
    for line in lines:
        dims = line["_aws"]["CloudWatchMetrics"][0]["Dimensions"][0]
        assert set(dims) <= {"language", "action", "reason", "outcome"}
        assert not any(isinstance(v, str) and ("jag" in v or "nej" in v) for v in line.values())
    eos = [line["EosToFirstAudio"] for line in lines if "EosToFirstAudio" in line]
    assert eos == [1850, 1100]                                            # one per turn, emitted once


def test_percentile():
    assert percentile([], 50) is None and percentile([5], 90) == 5
    assert percentile([100, 300, 200], 50) == 200


def test_server_command_line():
    p = server.parser()
    args = p.parse_args(["local", "--caller", "c.wav", "--out", "o.wav", "--fake"])
    assert args.mode == "local" and args.fake and args.dialed.startswith("+46")
    assert p.parse_args(["ws", "--port", "9000"]).port == 9000
    assert p.parse_args(["serve"]).port == 8080


def test_health_endpoint_answers_without_call_content():
    import asyncio
    import json as _json

    async def body():
        health = server.Health()
        srv = await server.health_server(health, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        out = []
        for path in ("/health", "/nope"):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            await writer.drain()
            out.append(await reader.read())
            writer.close()
        srv.close()
        await srv.wait_closed()
        return out

    ok, missing = asyncio.run(body())
    assert ok.startswith(b"HTTP/1.1 200") and missing.startswith(b"HTTP/1.1 404")
    data = _json.loads(ok.split(b"\r\n\r\n", 1)[1])
    assert data == {"status": "ok", "phoneAdapter": "missing", "clipsWarm": "pending", "clips": 0, "activeCalls": 0}
