"""Small constructors for recorded calls in tests (no gateway needed). Owner: builder 'harness'."""

from __future__ import annotations

from vg_harness.runner import Result, Segment


def seg(step: int, piece: int, start: int, end: int, text: str = "x", language: str = "sv") -> Segment:
    return Segment(step, piece, f"t/s{step}p{piece}", text, language, start, end, "synth")


def api_call(fn: str, at_ms: int, *, status: int = 201, party=None, date="2026-11-06", time="19:00") -> dict:
    return {"fn": fn, "route": fn, "at_ms": at_ms, "status": status, "party": party, "date": date, "time": time}


def booking(at_ms: int, party: int = 4) -> dict:
    return api_call("create-pending-reservation", at_ms, party=party)


def result_of(*events, segments=(), tool_calls=(), callbacks=(), offered=("19:00",), tts_calls=(), mode="fake",
              error=None) -> Result:
    return Result("t", events=sorted(events, key=lambda e: e.at_ms), caller_segments=list(segments),
                  tool_calls=list(tool_calls), callbacks=list(callbacks), offered_times=set(offered),
                  tts_calls=list(tts_calls), mode=mode, error=error)
