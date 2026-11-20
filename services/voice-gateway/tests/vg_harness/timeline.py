"""The timeline a replay prints. Owner: builder 'harness'.

`render(result, scenario)` turns a recorded call into text a person can read in one screen:

  1. a header (scenario, mode, length, how it ended, where the caller audio came from);
  2. two lanes on one time axis, caller above assistant, one character per 250 ms:
       caller    : # speaking     (the audio the harness sent)
       assistant : A reply  b backchannel  f filler  F fixed copy  x barge-in
  3. the events in order: caller speech, transcripts, turn holds and ends, language decisions, backchannels,
     assistant sentences and audio, barge-ins and how fast the line went quiet, tool calls, state changes;
  4. the numbers: end of speech to first audio per turn, backchannels, overlaps, barge-in stop time, language path,
     tools, bookings made;
  5. the verdict of every Expect field.

It is a development tool: it prints transcripts and sentences (the logs of the gateway never do, log.py drops them);
`show_text=False` hides them, for a shared screen.
"""

from __future__ import annotations

from vg_harness import analysis as an
from vg_harness.scenarios import Scenario

BIN_MS = 250
LANE_WIDTH = 96


def _fmt_t(ms: int) -> str:
    return f"{ms / 1000:6.2f}s"


def _quote(text: str, show_text: bool, limit: int = 78) -> str:
    if not show_text:
        return f"<{len(text.split())} words>"
    text = text if len(text) <= limit else text[: limit - 1] + "..."
    return f'"{text}"'


def lanes(result, scenario: Scenario | None = None) -> list[str]:
    """The two-lane strip as text lines (a ruler, caller, assistant per row of LANE_WIDTH bins)."""
    end = max([e.at_ms for e in result.events] + [s.end_ms for s in result.caller_segments] + [BIN_MS])
    bins = end // BIN_MS + 2
    caller = [" "] * bins
    assistant = [" "] * bins
    for s in result.caller_segments:
        for b in range(s.start_ms // BIN_MS, max(s.start_ms // BIN_MS, (s.end_ms - 1) // BIN_MS) + 1):
            caller[b] = "#"
    finished = {e.item_id: e for e in an.of(result.events, an.PlaybackFinished)}
    glyph = {"assistant": "A", "fixed": "F", "filler": "f", "backchannel": "b"}
    for p in an.playbacks(result.events):
        fin = finished.get(p.item_id)
        stop = fin.at_ms if fin else p.at_ms + p.duration_ms
        for b in range(p.at_ms // BIN_MS, max(p.at_ms // BIN_MS, (stop - 1) // BIN_MS) + 1):
            assistant[b] = glyph.get(p.kind, "A")
    for b, _ in an.barge_ins(result.events):
        assistant[b.at_ms // BIN_MS] = "x"
    out = []
    for row in range(0, bins, LANE_WIDTH):
        stop = min(bins, row + LANE_WIDTH)
        ruler = [" "] * (stop - row)
        for i in range(row, stop):
            if i % 20 == 0:                              # a mark every 5 s
                for k, ch in enumerate(f"{i * BIN_MS // 1000}s"):
                    if i - row + k < len(ruler):
                        ruler[i - row + k] = ch
        out.append(f"  t         {''.join(ruler)}")
        out.append(f"  caller    {''.join(caller[row:stop])}")
        out.append(f"  assistant {''.join(assistant[row:stop])}")
        out.append("")
    return out


def describe(e, show_text: bool, verbose: bool) -> tuple[str, str] | None:
    """(lane tag, text) of one event, None when it is not shown at this verbosity."""
    if isinstance(e, an.VadStart):
        return ("CALLER", f"starts speaking{' over the assistant' if e.during_playback else ''} (onset {_fmt_t(e.speech_start_ms).strip()})")
    if isinstance(e, an.VadPause):
        return ("caller", f"pause after {e.speech_ms} ms of speech") if verbose else None
    if isinstance(e, an.VadResume):
        return ("caller", f"speaks again after {e.pause_ms} ms") if verbose else None
    if type(e).__name__ == "SttPartial":
        return ("stt", f"partial {_quote(e.text, show_text, 60)}") if verbose else None
    if isinstance(e, an.SttFinal):
        return ("STT", f"final {_quote(e.text, show_text)} [{e.language}]")
    if isinstance(e, an.LanguageEvidence):
        return ("LANG", f"evidence {e.source} {e.code} {'' if e.score is None else f'{e.score:.2f}'}") if verbose else None
    if isinstance(e, an.LanguageDecided):
        what = f"{e.action.value} {e.candidate or ''} ({e.reason})".replace("  ", " ")
        return ("LANG", f"{what}; call language {e.current}, automatic switches {e.switches}")
    if isinstance(e, an.TurnHold):
        return ("TURN", f"holds the turn open: {e.reason} ({e.threshold_ms} ms)")
    if isinstance(e, an.TurnEnd):
        return ("TURN", f"ends: {_quote(e.text, show_text)} [{e.language}] (end of speech {_fmt_t(e.eos_ms).strip()}, "
                        f"waited {e.at_ms - e.eos_ms} ms, {e.reason})")
    if isinstance(e, an.BargeIn):
        return ("BARGE", f"caller talks over the assistant ({e.reason}), speech began {_fmt_t(e.speech_start_ms).strip()}")
    if isinstance(e, an.PlaybackFlushed):
        return ("BARGE", f"line quiet {e.stop_ms} ms after the barge-in ({e.dropped_ms} ms of audio dropped)")
    if type(e).__name__ == "BackchannelRequest":
        return ("BACK", f"requests '{e.token}' [{e.language}] ({e.reason})")
    if isinstance(e, an.PlaybackStarted):
        tag = {"backchannel": "BACK", "filler": "FILL"}.get(e.kind, "ASSIST")
        return (tag, f"plays {e.kind} [{e.language}] {e.duration_ms} ms")
    if isinstance(e, an.PlaybackFinished):
        return ("assist", f"{e.kind} ended after {e.played_ms} ms{' (interrupted)' if e.interrupted else ''}") if verbose else None
    if isinstance(e, an.AgentSentence):
        flag = " (before a tool)" if e.preamble else ""
        return ("AGENT", f"{e.kind} [{e.language}]{flag} {_quote(e.text, show_text)}")
    if isinstance(e, an.ToolStarted):
        return ("TOOL", f"{e.name} started")
    if isinstance(e, an.ToolFinished):
        return ("TOOL", f"{e.name} finished {'ok' if e.ok else 'not ok' if e.ok is False else ''} in {e.ms} ms".replace("  ", " "))
    if isinstance(e, an.AgentTurnDone):
        extra = ", ".join(x for x in (f"outcome {e.outcome}" if e.outcome else "",
                                      "awaiting yes" if e.awaiting_confirmation else "",
                                      f"asks {e.asked_question}" if e.asked_question else "",
                                      "cut" if e.cut else "", "error" if e.error else "",
                                      ", ".join(a.name for a in e.actions)) if x)
        return ("AGENT", f"turn done in {e.latency_ms} ms, {e.rounds} rounds, language {e.language}{'; ' + extra if extra else ''}")
    if type(e).__name__ == "StateChanged":
        return ("state", f"{e.old.value} -> {e.new.value} {e.reason}".rstrip()) if verbose else None
    if isinstance(e, an.CallEnded):
        return ("CALL", f"ended: {e.reason}")
    return None


def event_lines(result, *, show_text: bool = True, verbose: bool = False) -> list[str]:
    lines = []
    for e in result.events:
        d = describe(e, show_text, verbose)
        if d:
            lines.append(f" {_fmt_t(e.at_ms)}  {d[0]:<7} {d[1]}")
    return lines


def numbers(result) -> list[str]:
    ev = result.events
    replies = an.replies(ev)
    lines = []
    for i, r in enumerate(replies, 1):
        eos = r.eos_to_first_audio
        lines.append(f"  turn {i}: end of speech to first audio "
                     f"{'none (no reply played)' if eos is None else f'{eos} ms'}, turn detection {r.turn_detect} ms")
    p50 = an.percentile(an.eos_to_first_audio(ev), 0.5)
    if p50 is not None:
        lines.append(f"  p50 end of speech to first audio: {p50:.0f} ms")
    spans = an.backchannel_spans(ev)
    lines.append(f"  backchannels: {len(spans)}, overlapping the caller: {an.backchannel_overlaps(ev)}")
    for b, f in an.barge_ins(ev):
        lines.append(f"  barge-in at {_fmt_t(b.at_ms).strip()}: detected {b.at_ms - b.speech_start_ms} ms after speech began, "
                     f"line quiet after {'?' if f is None else f'{f.stop_ms} ms'}")
    lines.append(f"  language path: {' -> '.join(an.language_path(ev))} ({an.switches(ev)} switches)")
    names = [t.name for t in an.tools(ev)]
    lines.append(f"  tools: {', '.join(names) if names else 'none'}")
    made = [c for c in result.tool_calls if c["fn"] == "create-pending-reservation" and c.get("status") == 201]
    lines.append(f"  reservations made: {len(made)}"
                 + (f" (party {made[-1]['party']}, {made[-1]['date']} {made[-1]['time']})" if made else ""))
    if result.callbacks:
        lines.append(f"  callbacks requested: {', '.join(str(c.get('topic')) for c in result.callbacks)}")
    if result.sms:
        lines.append(f"  SMS notices sent: {len(result.sms)}")
    return lines


def render(result, scenario: Scenario | None = None, failures: list[str] | None = None, *, show_text: bool = True,
           verbose: bool = False) -> str:
    from vg_harness.asserts import checked_fields
    title = f"{scenario.key}: {scenario.title}" if scenario else result.scenario
    out = [f"== {title}  [{result.mode}]",
           f"   {result.elapsed_s:.1f} s wall time, ended: {result.end_reason or 'unknown'}"
           + (f", wav: {result.wav_path}" if result.wav_path else "")]
    if result.audio_sources:
        kinds = sorted(set(result.audio_sources.values()))
        out.append(f"   caller audio: {', '.join(kinds)}")
    if result.error:
        out.append(f"   ERROR: {result.error}")
    out += ["", "LANES (one character is 250 ms; # caller, A reply, b backchannel, f filler, F fixed copy, x barge-in)"]
    out += lanes(result, scenario)
    out += ["EVENTS"] + event_lines(result, show_text=show_text, verbose=verbose) + ["", "NUMBERS"] + numbers(result)
    if scenario is not None and failures is not None:
        fields = checked_fields(scenario.expect)
        out += ["", "EXPECTATIONS"]
        failed = {f.split(":", 1)[0] for f in failures}
        for name in fields:
            out.append(f"  {'FAIL' if name in failed else 'pass'}  {name}")
        for f in failures:
            out.append(f"        {f}")
        out.append(f"  {'PASS' if not failures else 'FAIL'}: {len(fields) - len(failed & set(fields))} of {len(fields)} checks")
    return "\n".join(out)
