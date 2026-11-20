# Voice gateway: design (Stage 2, streaming)

Status: integrated on branch `voice-gateway` (2026-11-20): every part built, the 14 replay scenarios pass in
fake mode, live runs on booqy-dev work end to end. Missing: the AWS phone adapter and deployment (section 10).
Sources: `ASSESSMENT.md` (Stage 2, option c) and `MULTILINGUAL.md` (rules 1 to 10, scenarios A to H),
the phone agent on origin/dev (2026-11-20).

## 0. Goal and constraints

A caller should feel they talk to a person: short acknowledgements while they speak, no cut-offs on
"för, ehm...", first audio about 1 s after they stop, the agent stops within 300 ms when they talk over it.
Booking stays exactly as reliable as today.

Owner constraints, binding for every builder:

- AWS-billed services only: Transcribe streaming, Polly, Bedrock, Fargate later. No ElevenLabs, no
  third-party telephony, no paid external API. Open-source libraries on CPU in the container are fine
  (silero-vad via onnxruntime, numpy, websockets, amazon-transcribe SDK).
- The booking logic is never rewritten. The gateway imports and runs the existing agent loop, tools,
  copy, language rules and guards (`shared/features/phone_agent/*`, `shared/agent_model_real.py`,
  `functions/phone-agent-call/app.py`) and edits none of them (a parallel Stage 1 change edits those now).
- Owner wording: greeting "Hej och välkommen till <restaurant>. Du pratar med vår digitala assistent."
  (copy.DISCLOSURE), "Booqy" read "Bookie" (copy.ssml_names), never "AI-assistent", fee rule without
  amounts until asked (tools guaranteeTerms), read-back plus a clear yes before any write, real
  availability only. All of this already lives in the shared code; the gateway only voices it.
- Swedish by default; multilingual per MULTILINGUAL.md with Transcribe identify-language (no local LID
  model, no ElevenLabs): languages with no Polly voice (fa, bs, id, so) get the fixed unsupported
  sentences and a callback in `language:<code>`.
- No transcript text or phone numbers in logs or metrics. No secrets anywhere.
- No em dash or en dash characters in any added line.

## 1. Architecture

```
               caller audio (canonical PCM16 16 kHz, 20 ms frames)
TelephonyAdapter ──────────────────────────────────────────────┐
  (LocalAdapter now,                                           v
   AWS phone adapter later)                      ┌────────── Listener (listen/) ──────────┐
        ^                                        │ RingBuffer 12 s                        │
        │ play / clear / hangup / transfer       │ VadSegmenter (silero) ─> VadStart/Pause│
        │                                        │ Transcriber (Transcribe streaming)     │
        │                                        │    ─> SttPartial / SttFinal            │
 OutputMixer (speak/)  <── PlaybackView ──────── │ BargeInDetector ─> BargeIn             │
   one source at a time                          │ TurnDetector ─> TurnHold / TurnEnd     │
   assistant > filler > backchannel              │ LanguageTracker (+ LID probe)          │
        ^        ^           ^                   └────────────┬───────────────────────────┘
        │        │           │                                │ EventBus (per call, typed events)
 SpeechQueue  FillerPolicy  BackchannelController <───────────┤
   (Polly, synth-ahead)        (rules, no LLM)                v
        ^                                          CallOrchestrator (state machine, metrics)
        │ AgentSentence                                       │ TurnEnd -> brain.turn()
        └──────────────────── AgentBrain (brain/) <───────────┘
                               session.call_turn(...) in a worker thread, unchanged,
                               with a per-call StreamingAgentModel (tee on ConverseStream)
                               DynamoDB call row, tool Lambdas, Bedrock as today
```

Latency budget for one tool-free turn (target p50 end of speech to first audio under 1200 ms):

| Step | Budget (ms) | Lever |
|---|---|---|
| Turn detection after the last voiced frame | 350 to 550 | TurnConfig thresholds by transcript tail |
| Transcribe final after silence | overlaps the above, wait at most 350 | partial stabilisation "high" |
| Bedrock first sentence (ConverseStream, cached prompt) | 450 to 700 | prompt caching, thinking "low" |
| Polly first audio bytes for the first sentence (Frankfurt) | 120 to 250 | stream the first item, short first chunk |
| Line lead | 60 | AudioConfig.playout_lead_ms |

Tool turns add one Bedrock round and the tool Lambda; a filler covers the gap only when nothing else
plays within 700 ms of the tool starting, and the model's own preamble ("Då kollar jag") is now spoken
as it streams, which usually makes the filler unnecessary.

## 2. Reuse of the existing agent (no edits in shared/ or functions/)

| Need | How, unchanged code |
|---|---|
| Call start, guards, refusal, greeting, shared-number question | `app.start_call` via `brain/lambda_app.load()` (importlib by path) |
| One caller turn | `session.call_turn(...)`, the body of `app.run_call_turn` re-stated in AgentBrain with a per-call model |
| Agent loop, tools, read-back and clear-yes guard, availability, fee rule | `agent.run_turn`, `tools.Executor` inside call_turn, untouched |
| Streaming | `StreamingAgentModel(RealAgentModel)` overrides only `_once`: tees ConverseStream events to the gateway, then returns `read_stream(...)`, so run_turn sees the same Turn as today |
| Spoken form of model text | `agent.spoken_text`, `language.tag`, `agent._META` per sentence |
| Fixed copy | `copy.*` (greeting, DISCLOSURE_SHORT, ERROR, STILL_CHECKING, STILL_THERE, TRANSFERRING, TRANSFER_UNANSWERED, MAX_LENGTH, unsupported_language) |
| Languages allowed, switch cap 3 | `language.call_languages(callRef)`: the gateway's callRef starts with `connect-gw-`, so `is_connect()` is true and the AWS voice list and MAX_CONNECT_SWITCHES apply |
| Transfer | `GatewayVoice.deferred_tools`: Executor returns `transfer_to_number`, the orchestrator runs it on the adapter |
| Barge-in after a read-back | `tools.Executor.not_heard` semantics applied to the stored toolState (AgentBrain.mark_not_heard) |
| End of call, metering, cap warning | `app.finish` |

Minimal future changes (Stage 2 hardening, NOT now, listed so Stage 1 can plan them):

1. `language.py`: a `GATEWAY_PREFIX` and `uses_aws_voices(call_ref)` instead of riding on `connect-`.
2. `shared/features/phone_agent/line.py`: `start_call`, `finish`, `run_call_turn(model=, voice=)` moved
   out of the Lambda module (then `lambda_app.py` is a plain import).
3. `agent.run_turn(on_delta=...)`: an official streaming hook instead of the `_once` tee.
4. `copy.INTERRUPTED`: a prompt note for "the caller interrupted your last reply" (today the gateway
   reuses not_heard and drops `resume`, and the history keeps only what the caller heard plus " ...").
5. Prompt caching in `agent_model_real` (the gateway adds cachePoints in its own `_once` meanwhile).

## 3. Package layout and file ownership

```
services/voice-gateway/
  DESIGN.md README.md Dockerfile requirements.txt requirements-dev.txt conftest.py .gitignore
  voice_gateway/
    __init__.py clock.py events.py config.py languages.py log.py      FROZEN (design owner)
    audio/      frames.py adapter.py (FROZEN shapes), resample.py buffers.py wav.py local.py
    listen/     vad.py transcribe.py language_id.py turn.py barge_in.py pipeline.py
    backchannel/ tokens.py clips.py controller.py
    speak/      tts.py chunker.py queue.py mixer.py fillers.py speaker.py
    brain/      lambda_app.py voice_port.py streaming.py agent_brain.py
    orchestrator.py metrics.py server.py
  tests/
    test_vg_skeleton.py                                                FROZEN (design owner)
    test_vg_<builder>_*.py                                             each builder its own
    vg_harness/ scenarios.py caller_audio.py runner.py asserts.py
    test_vg_scenarios.py   fixtures/
```

| Builder | Owns (writes only these) | Test files |
|---|---|---|
| listen | `audio/*` (implementations; frames.py and adapter.py shapes frozen), `listen/*` | `tests/test_vg_listen_*.py`, `tests/test_vg_audio_*.py` |
| converse | `backchannel/*`, `speak/*` | `tests/test_vg_converse_*.py` |
| brain | `brain/*`, `orchestrator.py`, `metrics.py`, `server.py` | `tests/test_vg_brain_*.py`, `tests/test_vg_orchestrator_*.py` |
| harness | `tests/vg_harness/*`, `tests/test_vg_scenarios.py`, `tests/fixtures/*` | the scenario suite |
| design owner | the FROZEN files above, DESIGN.md, README.md, Dockerfile, requirements*.txt, conftest.py | `test_vg_skeleton.py` |

Rules:

- Never edit another builder's file. Need a change in a frozen file or another builder's interface?
  Write it in your summary as a request (file, change, reason); the design owner applies it.
- A builder may change DEFAULT values in its own config section (config.py is frozen in shape only)
  and must list them in its summary. Field names and types do not change.
- Keep the public names in `test_vg_skeleton.py` INTERFACES. Private helpers are free.
- Every builder ships fakes for what others need from it: listen ships `FakeSttFactory` and
  `LocalAdapter`; converse ships `FakeTts`; brain ships a scripted brain for `server.py --fake`.
- Runtime-only libraries (numpy, onnxruntime, amazon_transcribe, websockets) are imported lazily inside
  functions or classes; tests that need them start with `pytest.importorskip`.
- No dashes (U+2013, U+2014) in any line, no transcript text or numbers in logs, Swedish for every word a
  caller hears, English in code and docs.

## 4. Concurrency model

- One asyncio task group per call (`CallOrchestrator.run`): Listener.run, Speaker.run (mixer, tick for
  backchannel and fillers), the orchestrator loop. A failing task ends the call with the error sentence.
- Pure cores, async shells: VadSegmenter, TurnDetector, BargeInDetector, LanguageTracker,
  BackchannelController, FillerPolicy, SentenceChunker and TextStreamer take `now_ms` or events and
  return events; no I/O, no clock reads. They are unit tested without asyncio.
- Audio frames never travel on the bus: adapter -> Listener by async iterator; SpeechQueue -> mixer ->
  adapter by direct calls. The bus carries only events (events.py), never blocks (a slow subscriber loses
  its oldest item).
- Blocking work runs in threads (`asyncio.to_thread`): `session.call_turn` (DynamoDB, tool Lambdas,
  Bedrock), Polly SynthesizeSpeech, boto3 clients. StreamBridge hops back to the loop with
  `call_soon_threadsafe` and drops callbacks from stale rounds (a worker that `agent._within` stopped
  waiting for keeps running; its text must never be spoken).
- Time: one `Clock` per call. RealClock in production and live harness; FakeClock (virtual, auto
  advanced) for unit tests and the fake harness. With FakeClock the brain's thread work is treated as
  instantaneous (fakes are synchronous); measure latency only with RealClock.

## 5. Interfaces (summary; the code is the spec)

- `audio.frames`: `Frame(pcm, ts_ms, sample_rate=16000)`, `AudioFormat`, `TELEPHONY_MULAW_8K`,
  `PCM16_16K`, `mulaw_encode/decode`, `to_canonical/from_canonical`, `frames_from_pcm`, `rms_dbfs`, `gain`.
- `audio.adapter.TelephonyAdapter`: `accept() -> CallInfo`, `caller_frames()`, `play(frame)`,
  `clear() -> ms dropped`, `hangup(reason)`, `transfer(to, timeout_s) -> TransferOutcome`, `closed`.
- `audio.local`: `LocalAdapter(clock, wire_format, paced, echo_db, noise_dbfs, ...)` with `.caller`
  (`CallerSide.say/silence_ms/wait_assistant_audio/wait_assistant_quiet/hang_up`), `timeline`, `save()`;
  `WebSocketAdapter`.
- `listen.vad`: `VadModel.prob(frame)`, `make_vad(cfg)`, `VadSegmenter.on_frame(frame, playing=) ->
  [VadStart|VadPause|VadResume]`.
- `listen.transcribe`: `SttMode`, `SttStream`, `SttFactory`, `TranscribeFactory`, `FakeSttFactory`,
  `Transcriber.open/open_identify/feed/reopen/probe/close`.
- `listen.language_id`: `candidates_for(caller, cfg)`, `strip_names`, `LanguageTracker.observe(UtteranceInfo)
  -> LanguageDecided`, `.wants_probe`, `.follow`.
- `listen.turn`: `classify_tail(text, lang, asked)`, `TurnDetector.on_vad/on_partial/on_final/on_tick/
  set_context/set_language/threshold_ms/reset`.
- `listen.barge_in`: `is_backchannel(text, lang)`, `BargeInDetector.on_frame/on_partial/reset`.
- `listen.pipeline.Listener(cfg, adapter, stt, bus, clock, playback, caller_e164=, names=)`:
  `run()`, `set_context()`, `follow_language()`, `turn_taken()`; `PlaybackView` protocol.
- `backchannel`: `tokens.TOKENS`, `pick_token`, `clips.Clip`, `ClipCache.warm/backchannel/filler`,
  `BackchannelController.on_event/on_tick -> [BackchannelRequest]`.
- `speak`: `Tts.synthesize/stream`, `PollyTts`, `FakeTts`, `SentenceChunker.feed/flush/reset`,
  `SpeechItem`, `SpeechQueue.enqueue/ready/cancel`, `OutputMixer` (PlaybackView + `flush`, `drain`,
  `request_backchannel`, `request_filler`), `FillerPolicy`, `Speaker.say/flush/drain/set_language/busy`.
- `brain`: `StreamingAgentModel(sink, cfg)`, `StreamSink`, `StreamBridge`, `TextStreamer`,
  `AgentBrain.start/turn/set_language/mark_not_heard/greeting_interrupted/greeting_played/
  transfer_result/finish/fixed/language`, `CallStart`, `GatewayVoice`, `lambda_app.load()`.
- `orchestrator`: `Components`, `CallOrchestrator(cfg, adapter, components).run() -> CallSummary`.
- `metrics`: `TurnMetrics`, `CallMetrics`, `MetricsRecorder.on_event/summary`.

## 6. Behaviour

### 6.1 Call state machine

See `orchestrator.py` docstring for every transition. Key decisions:

- Barge-in during the greeting before the disclosure sentence ended: flush, `greeting_interrupted()`
  says the short disclosure before the next reply (art. 50, as the voice webhook does today).
- Caller resumes within `continuation_grace_ms` of a TurnEnd and before any reply audio: the reply is
  not played; when its turn finishes, `mark_not_heard([])`; the new utterance is the next turn.
  Writes are never cancelled mid-way (agent.run_turn guarantees that); a read-back the caller did not
  hear never counts.
- STILL_THERE after 8 s of silence in LISTENING, twice, then the goodbye and hang up (`still_there`).
- Max call length comes from `run_call_turn` logic (`loc.maxCallSeconds`): MAX_LENGTH + end_call.

### 6.2 Brain, streaming and the call row

- One StreamingAgentModel per call; `session.call_turn` per caller turn in a worker thread; the call row,
  agentMessages, toolState, transcript turns, disclosure evidence and outcome exactly as today.
- callRef = `connect-gw-<line ref>`: allowed languages = aws_languages.json (every one has a Polly
  voice), switch cap 3, unsupported sentences in sv + en (connect=True: Elin cannot read Arabic script).
- Reconciliation: the reply `call_turn` returns is authoritative; sentences of it not yet streamed are
  spoken after the streamed ones (disclosure on a switch first, then fixed copy). Text tagged with a
  language outside the voice table is held until the turn ends.
- `AgentTurnDone.awaiting_confirmation` = a hold/proposal exists in toolState and outcome is not
  booked; `asked_question` from the last sentence ("?" plus yes/no or number or open by simple rules).

### 6.3 Turn detection

Thresholds by transcript tail (TurnConfig): complete 550, a complete clause of two words or more that
answers the assistant's question 450, short answer after a yes/no or number question 350, after an open
question 900 minimum (one word), filler 1600 (2200 right after a dangling word: "för ehm", "för. N."),
dangling word 1400, number being spoken 1100 (a number after "klockan", "kl", "halv" completes a time and
gets the complete wait; so does a single number word the recogniser ended with a full stop; digits in a
row stay a phone number), never above 2200; max utterance 20 s. TurnEnd waits at most 350 ms for the
Transcribe final, except that a partial whose last word ends within 150 ms of the end of speech covers a
complete clause of two words or more (never a short answer: "Nej ... tack, det går bra"), and never while
the live stream identifies the language (its final carries the scores). Swedish numbers Transcribe writes
as clock times ("för 15:00 15:00", "16:00 personer") are read back by `listen/numbers.py` before the brain
and the prefetch see the text.
Scenario voice A ("för, ehm... fyra personer"): "för" (dangling, 1400) then "ehm" (filler, 1600) hold
the turn across the 1.5 s pause; at most one "mm" (backchannel inside the pause window).

### 6.4 Language

- First utterance: identify-language stream with `candidates_for(caller)`: sv, en, then the caller's
  country candidates, then ar, de, pl; max 5 (Transcribe limit). Strong result (p >= 0.85, >= 1.5 s,
  >= 3 content words) or the explicit request switches before the brain runs: `brain.set_language`
  (the short disclosure in the new language is said first), the live stream reopens in that language
  with replay of the utterance from the ring buffer, and the TurnEnd text is the new stream's.
- Beside it, a second identify stream for the first utterance only (`second_identify_candidates`: sv, en,
  tr, fi, es, minus what the first has): Transcribe takes five options per stream, so Turkish (lang-J live)
  had no place among sv, en, ar, de, pl. Its result is used when its top language is not in the first
  stream, at p >= strong_p and above the first stream's top; it is waited for (at most 400 ms) only when
  the first stream's top is under strong_p. It runs from call start to the first turn end, about 10 s of
  extra Transcribe streaming per call (billed per second like the main stream).
- Away from Swedish a fixed sv-SE stream runs beside the live one (`shadow_start_language`): when the
  live stream's confidence is under 0.7 and the Swedish one's is 0.75 or more (and 0.1 higher), with three
  content words and no other language hint, its text is the utterance (the explicit "på svenska" and the
  Swedish words reach the tracker and the agent). It replaces the probe there, which could not finish in
  time. Cost: the call's Transcribe minutes double while it is away from Swedish, nothing in Swedish.
- Later utterances: a fixed-language stream (cheaper, custom vocabulary possible). A probe
  identify-language stream on the buffered utterance runs only when `wants_probe`: the text hint names
  another language (Arabic script with Persian letters gives fa), the caller asks for a language, or the
  ASR confidence is under 0.4.
- Rules 1 to 9 of MULTILINGUAL.md in LanguageTracker with LanguageConfig numbers; rule 6 (no flip-flop,
  stay 2 turns, max 3 switches) shared with the call row's languageSwitches.
- The brain may still switch on the model tag (language.decide): the orchestrator calls
  `listener.follow_language` and `speaker.set_language`.
- Unsupported (fa, bs, id, so...): no switch; the brain's turn yields the shared unsupported sentences
  (sv + en) and, the second time, the callback with `language:<code>` and end_call.
- Native voice guard: every AgentSentence carries its language; PollyTts speaks only with that
  language's voice (NoVoice otherwise, never a fallback voice).
- A read-back heard in another language before a switch is not a read-back now (MULTILINGUAL gap 9):
  `set_language` calls `mark_not_heard([])` semantics on toolState too.

### 6.5 Backchannels

Rules in `backchannel/controller.py` (all thresholds in BackchannelConfig): caller has held the turn
>= 2.5 s, pause between 250 and 600 ms, a clause of >= 4 new words, not a number tail, cooldown 4 s, at
most 2 per turn, never in the first second, never over caller speech, never over or right after
assistant audio, never while a read-back awaits the yes. Tokens per language pre-rendered at startup by
Polly into ClipCache, -4 dB, read at 150 to 165% and trimmed at 20 dB; one-syllable tokens ("mm", "ja")
cut to 220 ms with a fade, so they fit a 450 ms sentence pause (fire window from 200 ms). Target: 1 or 2
per 8 s monologue, 0 overlaps (scenario voice B).

### 6.6 Barge-in, echo, not heard

BargeIn when caller speech over assistant audio lasts 350 ms (stricter VAD threshold while playing), or
a partial has a non-backchannel word after 150 ms, or a stop word. Echo gate: caller level more than
10 dB under the playback level of the last 300 ms and no words. Flush target: under 300 ms from BargeIn
to silence on the line (playout lead 60 ms keeps little audio queued). Unheard sentences ->
`mark_not_heard(heard)`: proposals of this turn need a new read-back and a later clear yes.

### 6.7 Output rules

One source at a time; assistant > filler > backchannel; a backchannel never delays the agent by more
than 150 ms; a filler only when a real tool call started and nothing else is audible after 700 ms; fixed
copy always Booqy's wording; TTS failure of a sentence -> copy.ERROR in the same language, never silence;
a filler only while the tool still runs (once it finished the answer is a model round away and a filler,
which plays to its end, only delays it).

Opener (`speak/openers.py`): 120 ms after a TurnEnd, unless the caller goes on or the reply is already
there, a pre-rendered clip in the call language: "Jag kollar." only when the brain will run the
availability lookup first (6.8), "Då ska vi se." after a question, "Mm." or "Okej." otherwise, at
opener_chance and never the same line twice; none after a bare yes or no (unless it answers a read-back),
none on a language switch (the short disclosure comes first) or when the caller's words point to another
language. Clips over 650 ms are not kept. The reply's first chunk ends at its first comma (three words,
18 characters), so Polly starts on a clause.

### 6.8 Speculative availability and one model round

`brain/prefetch.py`. While the caller speaks, the partial text (numbers read back first) is parsed with the
phone agent's own readers (readback.clock_times, dates, weekdays, party_sizes; read only). Day, time and
party complete in a booking request (or after booking words earlier in the call), nothing else in toolState
(no live hold, no move, no language question, no pending disclosure): a throwaway executor on a copy of the
toolState runs get_availability through a PrefetchInvoker, which keeps only GET answers, each served once
within 20 s; newer slots drop older entries. At the turn, the same conditions on the final text make the
first model call answer with that get_availability tool use (the prompt's step 2); agent.run_turn runs it
through the real executor and the model's first Bedrock round has the result. Holds, writes, read-backs and
the clear yes stay the model's own and the guards are unchanged.

## 7. Metrics and logs

Per turn (EMF, dimension language): EosToFirstAudio (target p50 under 1500; the opener counts, it is what
the caller hears), EosToReplyAudio (the reply itself), OpenerPlayed, TurnDetectMs,
FirstSentenceMs, TtsFirstAudioMs, FirstBackchannelMs, BackchannelOverlaps (target 0), BargeInStopMs
(target under 300), BargeInDetectMs, Rounds, FillerPlayed. Per call: LanguageDetected and
LanguageSwitch (by reason), FirstLanguage, Outcome, BargeIns, CallMs. Logs: `log.event` with the hashed
call tag; never text, never numbers (log.py masks and drops).

## 8. Tests and CI

- Root CI (`python -m pytest -q -n auto` at the application root) collects `services/voice-gateway/tests`
  through `conftest.py`. It has no numpy, onnxruntime or amazon_transcribe: pure-logic tests run there,
  the rest skip with `importorskip`. `test_vg_skeleton.py` pins the contracts.
- Full gateway suite: `pip install -r services/voice-gateway/requirements-dev.txt` then
  `python -m pytest services/voice-gateway -q`. A CI job for it is a `.github/` change: snippet for the
  owner, never an edit by a builder.
- Run locally from the application root with the main checkout's venv:
  `/Users/akoura/projects/it-hjalparna/booqy/application/.venv/bin/python -m pytest services/voice-gateway -q`.

## 9. Replay harness (builder 'harness')

- `vg_harness.scenarios`: voice A to F (hesitation, backchannel, correction, interruption, booking with
  the fee rule, unavailable time) and language A to H (Swedish, English after the greeting, Persian
  unsupported, Arabic, English to Swedish, uncertain, context across a switch, interruption keeps the
  language), as data with expectations.
- Caller audio: Polly voices different from the assistant's (Astrid standard for Swedish), cached under
  `tests/fixtures/caller/`, run through an 8 kHz mu-law line simulation, fed at real-time pace through
  LocalAdapter; the reactive steps (talk over the read-back) use `CallerSide.wait_assistant_audio`.
- Modes: `fake` (FakeClock, FakeStt, FakeTts, real AgentBrain over the application's moto fixtures and
  the agent model simulator; free, deterministic, can run in CI) and `live` (RealClock, real Transcribe,
  Polly, Bedrock on booqy-dev; tool Lambdas and tables still faked, so no dev data to clean up).
- Records the stereo WAV (caller left, assistant right), events, metrics, fake tool calls; asserts every
  Expect field.

## 10. Later: deployment (not in this step)

- ECS Fargate ARM64 (1 vCPU, 2 GB; silero runs in about 1 ms per 32 ms window), one call per asyncio
  task, about 20 to 40 concurrent calls per task (verify with the harness), ECR, CloudWatch logs 30 days,
  config JSON in SSM.
- IAM call-out (new task role, no OIDC or deploy-role change in this step): transcribe:StartStreamTranscription
  (eu-central-1: streaming is not served in eu-north-1), polly:SynthesizeSpeech (eu-central-1), bedrock:InvokeModelWithResponseStream (same model
  ARNs as phone-agent-call), lambda:InvokeFunction (same tool functions), DynamoDB on the PhoneAgent and
  tenant tables (same as phone-agent-call), ssm:GetParameter on its path. The CI deploy role would need
  ECS/ECR permissions: that is a deploy-role change and the owner's decision.
- The AWS phone adapter: only AWS telephony (owner). Candidate to verify: Amazon Chime SDK Voice Connector
  with origination to the gateway's own SIP/RTP endpoint (G.711 mu-law 8 kHz, which is
  TELEPHONY_MULAW_8K) behind an NLB with UDP; also Swedish number availability in Chime SDK. Amazon
  Connect cannot stream audio both ways (ASSESSMENT). Decision for the owner when Stage 2 goes ahead.

## 11. Risks and open points

- Verified with live streams on booqy-dev (builder 'listen', 2026-11-20): Transcribe streaming answers
  401 in eu-north-1, so the gateway streams to eu-central-1 (about 25 ms away, like Polly). sv-SE and en-GB
  stream with partial stabilization (partials about 100 ms behind the audio, every 480 ms; the final
  about 0.55 s after the last word), identify-language scores Swedish and English correctly, replay at 3x
  is accepted, and a stream opens for every code in languages.TRANSCRIBE (fa-IR, id-ID, bs-BA and so-SO
  included). Open: recognition and identification quality for Arabic and Persian on real callers.
- Transcribe bills every streamed second, silence included (the stream carries the whole call: gating by
  VAD would break its timeline). About the price of the call length, not of the speech.
- webrtcvad has no arm64 Python 3.13 wheel (checked): silero via onnxruntime is the default VAD, energy
  VAD the fallback; the silero model file is pinned by sha256 in the Dockerfile.
- Elin neural lives only in eu-central-1 (checked): every Polly call crosses to Frankfurt (about 25 ms).
- Echo on speakerphones: the echo gate is heuristic; scenario D/H with `echo_db` in LocalAdapter.
- Bedrock prompt caching for Haiku 5.5: verify; harmless if unsupported (the gateway then sends none).
- Backchannel voice: Elin saying "mm" may sound synthetic; owner decision 7 (recorded human clips).
- Persian callers on a +46 number: detection depends on the Arabic-script hint and the probe (scenario
  lang-C proves it or shows the gap).

## 12. Parallel build plan

| Milestone | listen | converse | brain | harness |
|---|---|---|---|---|
| M1 (pure cores) | frames tests, resample, buffers, VadSegmenter, TurnDetector, BargeInDetector, LanguageTracker | tokens, controller, chunker, fillers | TextStreamer, StreamingAgentModel._once tee, metrics | scenarios, caller_audio render + cache |
| M2 (shells with fakes) | LocalAdapter, FakeSttFactory, Listener | FakeTts, ClipCache, SpeechQueue, OutputMixer, Speaker | AgentBrain over moto fixtures, orchestrator | runner fake mode |
| M3 (AWS) | TranscribeFactory, live verification | PollyTts, clip tuning | real Bedrock streaming, prompt cache | live mode, metrics report |

Integration rule: at each milestone every builder's own tests pass and `test_vg_skeleton.py` still passes.
