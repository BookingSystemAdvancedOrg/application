"""Streaming out of the unchanged agent loop (brain/streaming.py): the tee
returns exactly the Turn the parent model returns, the sink sees events in
order, prompt caching, retries, the deadline, stale rounds, the sentence
streamer (tags, held languages, the native voice guard, the disclosure on
a new language, preambles, max_tokens) and the reconciliation with the
reply session.call_turn returns. Pure stdlib + the application's deps."""

import asyncio
import threading
import time

import pytest
from botocore.exceptions import ClientError

from shared.agent_model import AgentModelError
from shared.agent_model_real import RealAgentModel, read_stream
from shared.features.phone_agent import copy
from shared.features.phone_agent.tools import TOOLS

from voice_gateway.brain.streaming import (NO_FILLER_TOOLS, StreamBridge, StreamingAgentModel, TextStreamer,
                                           fixed_language, reconcile, split_sentences, with_cache_points)
from voice_gateway.clock import FakeClock
from voice_gateway.config import BrainConfig, SpeakConfig
from voice_gateway.events import AgentSentence, EventBus, ToolFinished, ToolStarted

ALLOWED = ("sv", "en", "de", "ar")


def text_events(*chunks, index=0):
    return [{"contentBlockDelta": {"contentBlockIndex": index, "delta": {"text": c}}} for c in chunks]


def tool_events(name, args='{"date": "2026-11-03"}', index=1, use_id="u1"):
    return [{"contentBlockStart": {"contentBlockIndex": index, "start": {"toolUse": {"toolUseId": use_id,
                                                                                    "name": name}}}},
            {"contentBlockDelta": {"contentBlockIndex": index, "delta": {"toolUse": {"input": args}}}},
            {"contentBlockStop": {"contentBlockIndex": index}}]


def stop(reason="end_turn"):
    return [{"messageStop": {"stopReason": reason}}]


class FakeBedrock:
    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def converse_stream(self, **kwargs):
        self.requests.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"stream": iter(answer)}


class Sink:
    def __init__(self):
        self.calls = []

    def round_started(self, r):
        self.calls.append(("start", r))

    def round_restarted(self, r):
        self.calls.append(("restart", r))

    def block_started(self, r):
        self.calls.append(("block", r))

    def text_delta(self, r, t):
        self.calls.append(("text", r, t))

    def tool_started(self, r, n):
        self.calls.append(("tool", r, n))

    def round_stopped(self, r, why):
        self.calls.append(("stop", r, why))


ROUND = text_events("[sv] Ett ögon", "blick, jag kollar.") + \
    [{"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"reasoningContent": {"text": "secret plan"}}}}] + \
    tool_events("get_availability") + stop("tool_use")


# --- the model --------------------------------------------------------------------------------------

def test_the_streaming_model_returns_exactly_the_parents_turn_and_tees_in_order():
    sink = Sink()
    model = StreamingAgentModel(sink, BrainConfig(prompt_cache=False), client=FakeBedrock(list(ROUND)),
                                model_id="m")
    turn = model.converse("system", [{"role": "user", "content": "Hej"}], TOOLS, max_tokens=400)
    assert turn == read_stream(iter(ROUND))                              # byte for byte what run_turn sees today
    assert turn.text == "[sv] Ett ögonblick, jag kollar." and turn.tool_uses[0].name == "get_availability"
    assert sink.calls == [("start", 1), ("block", 1), ("text", 1, "[sv] Ett ögon"),
                          ("text", 1, "blick, jag kollar."), ("tool", 1, "get_availability"),
                          ("stop", 1, "tool_use")]                       # reasoning never reaches the sink
    assert StreamingAgentModel.converse is RealAgentModel.converse


def test_prompt_cache_points_are_added_once_and_the_parent_args_stay_untouched():
    args = {"system": [{"text": "s"}], "toolConfig": {"tools": [{"toolSpec": {"name": "a"}}]}, "messages": []}
    cached = with_cache_points(args)
    assert cached["system"][-1] == {"cachePoint": {"type": "default"}}
    assert cached["toolConfig"]["tools"][-1] == {"cachePoint": {"type": "default"}}
    assert "cachePoint" not in str(args) and with_cache_points(cached) == cached
    client = FakeBedrock(text_events("[sv] Hej.") + stop())
    StreamingAgentModel(Sink(), BrainConfig(), client=client, model_id="m").converse("s", [], TOOLS, max_tokens=10)
    assert client.requests[0]["system"][-1] == {"cachePoint": {"type": "default"}}


def test_a_retried_round_is_reported_as_restarted():
    throttled = ClientError({"Error": {"Code": "ThrottlingException"}}, "ConverseStream")
    sink = Sink()
    client = FakeBedrock(throttled, text_events("[sv] Hej.") + stop())
    turn = StreamingAgentModel(sink, BrainConfig(), client=client, model_id="m").converse(
        "s", [], TOOLS, max_tokens=10)
    assert turn.text == "[sv] Hej."
    assert [c[0] for c in sink.calls] == ["start", "stop", "restart", "block", "text", "stop"]
    assert sink.calls[1] == ("stop", 1, "error")


def test_nothing_is_reported_after_the_deadline():
    class Slow:
        def __iter__(self):
            yield text_events("[sv] Först.")[0]
            time.sleep(0.15)
            yield text_events(" Sent.")[0]
            yield stop()[0]

    sink = Sink()

    class Client:
        def converse_stream(self, **kwargs):
            return {"stream": Slow()}

    model = StreamingAgentModel(sink, BrainConfig(), client=Client(), model_id="m")
    with pytest.raises(AgentModelError):
        model._once({"system": [{"text": "s"}]}, 0.1, time.monotonic() + 0.1)
    texts = [c[2] for c in sink.calls if c[0] == "text"]
    assert texts == ["[sv] Först."]                                      # the late text never reaches the caller


# --- the streamer -----------------------------------------------------------------------------------

def stream(chunks, *, lang="sv", disclosed=("sv",), stop_reason="end_turn", allowed=ALLOWED, **kw):
    s = TextStreamer(SpeakConfig(), lang, allowed, 1, disclosed, **kw)
    out = s.start_block(0)
    for i, chunk in enumerate(chunks):
        out += s.feed(chunk, i)
    out += s.end_round(stop_reason, 99)
    return s, out


def test_sentences_stream_as_they_complete_with_the_tag_stripped():
    s = TextStreamer(SpeakConfig(), "sv", ALLOWED, 1, ("sv",))
    s.start_block(0)
    assert s.feed("[s", 1) == [] and s.feed("v] Jag har ett bord klockan 19.", 2) == []   # no border yet
    first = s.feed("30 i morgon. Stämmer", 3)
    assert [x.text for x in first] == ["Jag har ett bord klockan 19.30 i morgon."]     # 19.30 is not a border
    assert first[0].language == "sv" and first[0].kind == "assistant"
    rest = s.feed(" det?", 4) + s.end_round("end_turn", 5)
    assert [x.text for x in rest] == ["Stämmer det?"] and [x.index for x in first + rest] == [0, 1]


def test_a_short_first_chunk_waits_for_the_next_sentence_and_a_long_clause_is_cut():
    _, out = stream(["[sv] Ja. ", "Jag kollar det åt dig nu. Ett ögonblick."])
    assert [x.text for x in out] == ["Ja. Jag kollar det åt dig nu.", "Ett ögonblick."]
    long = "[sv] Vi har bord klockan sju, klockan halv åtta, klockan åtta och klockan halv nio, " \
           "och även senare på kvällen om det passar bättre för ditt sällskap"
    s = TextStreamer(SpeakConfig(), "sv", ALLOWED, 1, ("sv",))
    s.start_block(0)
    cut = s.feed(long, 1)
    assert cut and cut[0].text.endswith(",") and len(cut[0].text) <= SpeakConfig().clause_split_chars + 1


def test_cleaning_drops_notes_dashes_and_repeats():
    _, out = stream(["[sv] Let me correct that: fel.\n\nKlockan sju går bra \u2014 välkommen! ",
                     "Klockan sju går bra, välkommen."])
    texts = [x.text for x in out]
    assert all("\u2014" not in t and "!" not in t for t in texts)
    assert texts.count("Klockan sju går bra, välkommen.") == 1


def test_max_tokens_drops_the_unfinished_fragment():
    _, out = stream(["[sv] Vi har bord klockan sju i morgon. Och klockan åt"], stop_reason="max_tokens")
    assert [x.text for x in out] == ["Vi har bord klockan sju i morgon."]


def test_a_new_language_gets_the_short_disclosure_first_in_its_own_voice():
    _, out = stream(["[en] Of course, for how many people would that be?"])
    assert [(x.kind, x.language, x.text) for x in out] == [
        ("fixed", "en", "I am the restaurant's digital assistant."),
        ("assistant", "en", "Of course, for how many people would that be?")]
    _, again = stream(["[en] Of course, for how many people would that be?"], disclosed=("sv", "en"))
    assert [x.kind for x in again] == ["assistant"]


def test_an_unspeakable_or_not_allowed_language_is_held_never_spoken_in_another_voice():
    s, out = stream(["[fa] من یک میز می خواهم."])
    assert out == [] and s.held and "میز" in s.held_text
    s, out = stream(["[ar] نعم، لدينا طاولة."], allowed=("sv", "en"))
    assert out == [] and s.held


def test_untagged_text_takes_the_prompt_language():
    _, out = stream(["Gerne, für wie viele Personen?"], lang="de", disclosed=("sv", "de"))
    assert [(x.language, x.text) for x in out] == [("de", "Gerne, für wie viele Personen?")]


def test_text_before_a_lookup_is_a_preamble():
    s = TextStreamer(SpeakConfig(), "sv", ALLOWED, 1, ("sv",))
    s.start_block(0)
    s.feed("[sv] Ett ögonblick, jag kollar", 1)
    out = s.tool_started(2)
    assert [(x.text, x.preamble) for x in out] == [("Ett ögonblick, jag kollar", True)]


# --- reconciliation ---------------------------------------------------------------------------------

def test_reconcile_adds_only_what_was_not_streamed_in_its_own_language():
    streamed = [AgentSentence(0, 1, 0, "I am the restaurant's digital assistant.", "en", "fixed"),
                AgentSentence(0, 1, 1, "Of course.", "en")]
    final = "I am the restaurant's digital assistant. Of course. What time would you like?"
    out = reconcile(final, streamed, turn=1, language="en", at_ms=5, start_index=2)
    assert [(x.index, x.kind, x.language, x.text) for x in out] == [(2, "assistant", "en",
                                                                     "What time would you like?")]
    unsupported = copy.unsupported_language(connect=True)
    out = reconcile(unsupported, [], turn=2, language="sv", at_ms=0)
    assert [(x.kind, x.language) for x in out] == [("fixed", "sv"), ("fixed", "sv"), ("fixed", "en"),
                                                   ("fixed", "en")]                 # Elin, then the English voice
    out = reconcile(copy.ERROR["de"], [], turn=3, language="de", at_ms=0)
    assert {(x.kind, x.language) for x in out} == {("fixed", "de")}


def test_reconcile_knows_a_short_sentence_streamed_together_with_the_next():
    streamed = [AgentSentence(0, 1, 0, "Ja. Jag kollar det åt dig nu.", "sv")]
    assert reconcile("Ja. Jag kollar det åt dig nu. Klockan sju är ledigt.", streamed, turn=1, language="sv",
                     at_ms=0)[0].text == "Klockan sju är ledigt."
    assert len(reconcile("Ja. Jag kollar det åt dig nu.", streamed, turn=1, language="sv", at_ms=0)) == 0
    assert [x.text for x in reconcile("Nej.", [AgentSentence(0, 1, 0, "Nejtack, inte nu.", "sv")], turn=1,
                                      language="sv", at_ms=0)] == ["Nej."]          # whole words only


def test_fixed_language_prefers_the_call_language_for_shared_sentences():
    assert fixed_language(copy.DISCLOSURE_SHORT["no"], "da") == "da"
    assert fixed_language(copy.DISCLOSURE_SHORT["no"], "no") == "no"
    assert fixed_language("Hej på dig.") is None
    assert split_sentences("Klockan 19.30. Går det bra?") == ["Klockan 19.30.", "Går det bra?"]


# --- the bridge -------------------------------------------------------------------------------------

def bridge_run(fn):
    async def main():
        bus = EventBus()
        bus.history = []
        bridge = StreamBridge(asyncio.get_running_loop(), bus, FakeClock(), BrainConfig(), SpeakConfig())
        result = await fn(bridge)
        return bus.history, result
    return asyncio.run(main())


def test_the_bridge_hops_threads_drops_stale_rounds_and_announces_only_real_tools():
    async def fn(bridge):
        bridge.open_turn(1, "sv", ALLOWED)

        def worker():
            bridge.round_started(1)
            bridge.text_delta(1, "[sv] Jag kollar bord åt dig, ett ögonblick.")
            bridge.tool_started(1, "get_availability")
            bridge.round_stopped(1, "tool_use")
            bridge.round_started(2)
            bridge.text_delta(1, "[sv] Sen text från en gammal runda.")     # stale: never spoken
            bridge.text_delta(2, "[sv] Klockan sju är ledigt. Tack för samtalet.")
            bridge.tool_started(2, "end_call")
            bridge.round_stopped(2, "tool_use")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        await asyncio.sleep(0)
        emitted = bridge.close_turn()
        bridge.text_delta(2, " Efter turen.")                                # after close: dropped
        await asyncio.sleep(0)
        return emitted

    history, emitted = bridge_run(fn)
    sentences = [e.text for e in history if isinstance(e, AgentSentence)]
    assert sentences == ["Jag kollar bord åt dig, ett ögonblick.", "Klockan sju är ledigt.", "Tack för samtalet."]
    assert [e.text for e in emitted] == sentences
    assert [e.name for e in history if isinstance(e, ToolStarted)] == ["get_availability"]   # no filler for end_call
    finished = [e for e in history if isinstance(e, ToolFinished)]
    assert [e.name for e in finished] == ["get_availability"]
    assert "end_call" in NO_FILLER_TOOLS and history.index(finished[0]) < len(history)


def test_a_held_reply_is_left_to_the_reconciliation():
    async def fn(bridge):
        bridge.open_turn(1, "sv", ("sv", "en"))
        bridge.round_started(1)
        bridge.text_delta(1, "[fa] سلام، یک میز برای چهار نفر.")
        bridge.round_stopped(1, "end_turn")
        await asyncio.sleep(0)
        return bridge.close_turn()

    history, emitted = bridge_run(fn)
    assert emitted == [] and not [e for e in history if isinstance(e, AgentSentence)]
