# voice-gateway

Stage 2 of the Booqy phone agent: a streaming voice front around the unchanged phone agent. The caller is
heard by a local VAD and Amazon Transcribe streaming, turns end on meaning and silence (not on a fixed
timeout), short acknowledgements ("mm") fall in the caller's pauses, replies are spoken sentence by sentence
by Amazon Polly while the agent is still thinking, and the agent stops within 300 ms when the caller talks
over it. The booking itself is the existing agent in `shared/features/phone_agent` (same tools, read-back and
clear-yes guard, real availability, owner wording, language rules): the gateway imports it and edits nothing.

AWS-billed services only (Transcribe, Polly, Bedrock, Fargate later); the VAD (silero) runs on the CPU in the
container. Design, ownership and interfaces: [DESIGN.md](DESIGN.md). Languages: MULTILINGUAL.md.

## Layout

| Folder | What |
|---|---|
| `voice_gateway/audio/` | frames, resampler, ring, jitter and playout buffers, WAV, LocalAdapter (simulated phone line), WebSocket adapter |
| `voice_gateway/listen/` | VAD (silero, energy fallback), Transcribe streaming client (live, probe and shadow streams), turn detector, barge-in, language tracker, Swedish number read-back (`numbers.py`), Listener |
| `voice_gateway/speak/` | Polly TTS, sentence chunker, speech queue, fillers, openers (`openers.py`), output mixer, Speaker |
| `voice_gateway/backchannel/` | when an "mm" fits (controller), the tokens per language, the pre-rendered clip cache |
| `voice_gateway/brain/` | AgentBrain (the phone agent's turn, streamed), StreamingAgentModel (Bedrock ConverseStream tee), speculative availability and the one-round booking turn (`prefetch.py`) |
| `voice_gateway/orchestrator.py` | the call state machine (greeting, listening, thinking, speaking, transfer, ending) |
| `voice_gateway/server.py` | entry points: `local`, `ws`, `serve` |
| `tests/` | unit tests per part, the replay harness (`tests/vg_harness/`) and its 14 scenarios |

## Run it locally

From the application root, with Python 3.13 or later:

```
python -m venv .venv-vg && . .venv-vg/bin/activate
pip install -r services/voice-gateway/requirements-dev.txt     # the gateway's runtime plus the application's dev deps
cd services/voice-gateway
```

One whole call, no AWS (fake recogniser, tone voice, scripted agent), recorded as a stereo WAV (left the
caller, right the assistant) with a metrics JSON next to it:

```
python -m voice_gateway.server local --fake --caller tests/fixtures/listen/sv_booking.wav \
    --transcript "Hej, jag vill boka ett bord för fyra i morgon klockan sju." --out /tmp/vg/call.wav
```

Replay one scenario through the whole gateway and print its timeline (lanes, events, numbers, checks):

```
python -m voice_gateway.replay list
python -m voice_gateway.replay voice-B                  # fake mode: virtual clock, free, deterministic
python -m voice_gateway.replay all --summary            # every scenario, one line each
python -m voice_gateway.replay lang-B --mode live       # real Transcribe, Polly, Bedrock on booqy-dev (cents)
python -m voice_gateway.replay all --summary --sim-latency   # fake mode with the live costs on the virtual clock
python -m voice_gateway.replay all --summary --sim-latency --config-json '{"speak": {"opener": false}}'
```

`--sim-latency` puts what the live run measured on the virtual clock (`vg_harness.runner.SIM_LATENCY`: 1500 ms
per Bedrock round the turn sent, 120 ms per tool Lambda call that went out cold, Polly 100 ms, the Transcribe
final 550 ms after the last word), so a change to the turn's shape shows in fake mode. It is for the latency
numbers: the scripted callers are not reactive, so some booking checks fail under it as they did before.

Live mode uses the AWS profile `booqy-dev` (`VG_AWS_PROFILE` to change it); tables and tool Lambdas stay
faked, so it writes no dev data. The silero model: point `VG_SILERO_MODEL` at a `silero_vad.onnx` (the
container has it at `/opt/models/silero_vad.onnx`); without it the energy VAD is used and logged as
`vad_fallback`.

A WebSocket line for manual tests (127.0.0.1 only): `python -m voice_gateway.server ws --fake`.

## Tests

```
python -m pytest services/voice-gateway -q -n auto                    # from the application root
VG_SILERO_MODEL=/path/silero_vad.onnx python -m pytest services/voice-gateway -q -n auto
VG_AWS=1 AWS_PROFILE=booqy-dev python -m pytest services/voice-gateway -q -m aws   # live Transcribe tests
VG_HARNESS=off python -m pytest services/voice-gateway -q             # skip the scenarios
```

Root CI runs `python -m pytest -q -n auto` with only the application's `requirements-dev.txt` (no numpy,
onnxruntime or amazon-transcribe). The gateway tests are written for that: pure logic and the 14 scenarios
run there (the line is plain PCM without numpy), a test that needs a runtime library skips itself
(`pytest.importorskip`), and the live tests are opt-in (`VG_AWS=1`, marker `aws`). Nothing here needs a
change to `.github/`.

## Container

```
docker build -f services/voice-gateway/Dockerfile -t voice-gateway:dev .      # from the application root
docker run --rm -p 8080:8080 voice-gateway:dev
curl -s localhost:8080/health
```

Python 3.13 slim, two stages: the silero model is taken from the pinned `silero-vad` wheel (no torch) and
checked by sha256, the build fails on a mismatch. Runs as uid 10001, no secrets in the image (AWS access
from the task role), `HEALTHCHECK` on `GET /health`. `serve` answers no calls yet (see What is missing):
`/health` returns 200 with `"phoneAdapter": "missing"`.

## Configuration

Every tunable is a field of `voice_gateway/config.py` (`GatewayConfig`, sections `audio`, `vad`, `stt`,
`language`, `turn`, `barge_in`, `backchannel`, `speak`, `brain`, `call`; all durations in ms). Override with
one JSON document; unknown keys fail loudly:

```
VG_CONFIG_JSON='{"turn": {"filler_silence_ms": 1800}, "backchannel": {"enabled": false}}' python -m voice_gateway.server ...
```

The latency levers can each be switched off there: `speak.opener` (the opener at the turn end),
`speak.first_clause` (Polly starts on the first clause), `brain.prefetch` (availability looked up from the
partial transcript), `brain.one_round` (the lookup before the first model round), `brain.warm_connections`
(one Bedrock client per call, Bedrock and Polly connections opened while the greeting plays),
`language.second_identify_candidates` (empty: no second identify stream) and
`language.shadow_start_language` (no Swedish stream beside the live one away from Swedish).

Transcribe cost of the language streams: the second identify stream runs from call start to the first turn
end (about 10 s, sv, en, tr, fi, es: Turkish, Finnish and Spanish callers on +46 numbers are found, the first
stream keeps sv, en, ar, de, pl); the Swedish stream beside the live one runs only while the call is in
another language, so such a call bills its Transcribe seconds twice for that stretch. Both are billed like the
main stream (Transcribe streaming, per second, eu-central-1); a Swedish call pays only the 10 s.

In Fargate the same JSON comes from SSM (`/booqy/<env>/voice-gateway/config`). Environment knobs:
`VG_CONFIG_JSON`, `VG_SILERO_MODEL`, `VG_APP_ROOT` (where `functions/phone-agent-call/app.py` lives),
`PHONE_AGENT_MODEL_ID` (the Bedrock model, as the phone-agent-call Lambda), `VG_HARNESS` (`fake`, `live`,
`off`), `VG_AWS=1` (live tests), `VG_AWS_PROFILE`.

Regions: Transcribe streaming `eu-central-1` (it answers 401 in eu-north-1, checked live), Polly
`eu-central-1` (Elin neural is only there), Bedrock as the phone agent.

## Metrics and logs

One CloudWatch Embedded Metric Format line per value, namespace `Booqy/VoiceGateway`, dimensions of low
cardinality only (language, outcome, reason; never a call id, a phone number or any transcript text).

| Metric | Per | Meaning |
|---|---|---|
| `EosToFirstAudio` | turn | end of the caller's speech to the first audio the caller hears, the opener included (the number that matters) |
| `EosToReplyAudio`, `OpenerPlayed` | turn | end of speech to the reply itself; whether an opener played |
| `TurnDetectMs` | turn | end of speech to the turn decision |
| `FirstSentenceMs` | turn | turn decision to the agent's first sentence |
| `TtsFirstAudioMs` | turn | sentence to its first Polly audio |
| `FirstBackchannelMs`, `BackchannelOverlaps` | turn | when the first "mm" came, and how many met the caller's voice (target 0) |
| `BargeInDetectMs`, `BargeInStopMs` | turn | caller onset to detection, detection to silence on the line (target under 300) |
| `Rounds`, `FillerPlayed` | turn | agent rounds (tools), whether a filler covered a tool call |
| `CallMs`, `BargeIns`, `Outcome`, `FirstLanguage`, `LanguageDetected`, `LanguageSwitch` | call | the call's shape |

Logs are JSON events (`state`, `brain_turn`, `barge_in`, `still_there`, `call_ended`, ...) tagged with a
hashed call tag only. `python -m voice_gateway.server local` writes the same per-turn numbers to
`<out>.metrics.json`; the replay harness recomputes them from the raw events and checks them.

## Guards on top of the agent's

The agent's read-back and clear-yes guard decides every write; streaming adds a few ways to hear things out of
order, so the gateway closes them:

* a sentence that says a write is done ("Jag har bokat ...") is never streamed before its round ends: it could
  come right before the create_reservation call the guard refuses; the final reply says it when it is true;
* a caller turn that began while a read-back was still playing (more than `call.overlap_tolerance_ms` before
  its end) is not the answer to it: the read-back counts as not heard, so the agent reads back again;
* a barge-in, a reply dropped because the caller kept talking, and a language switch all mark the read-back
  as not heard (the agent's own not-heard rule);
* a Transcribe send that hangs (2 s) or fails reopens the stream once with replay of the buffered audio; a
  second failure ends the call with the error sentence (never silence);
* a brain turn still running 25 s after it started ends the call with the error sentence; no call outlives
  960 s (the largest `maxCallSeconds` plus a minute), so a line with endless noise cannot keep Transcribe open.

## Add a language

1. Hearing: add the Transcribe streaming code to `TRANSCRIBE` in `voice_gateway/languages.py` and open one
   live stream in that code (`VG_AWS=1` test in `tests/test_vg_listen_transcribe_sdk.py`).
2. Speaking: the voice comes from the phone agent's own table (`pa_language.AWS_LANGUAGES` in
   `shared/features/phone_agent/language.py`, owned by the phone agent, not by the gateway). A code with a
   Transcribe code but no voice is heard only: it gets the fixed unsupported sentences and a callback.
3. Conversation feel (optional, each falls back to silence or the generic rule when missing):
   backchannel tokens in `backchannel/tokens.py` (`TOKENS`, at least one continuer), fillers in
   `speak/fillers.py` (`FILLERS`), turn-end word lists in `listen/turn.py` (`TAILS`: filler, dangling,
   number, short answer), backchannel words for barge-in in `listen/barge_in.py`, explicit language requests
   in `listen/language_id.py`.
4. A replay scenario in `tests/vg_harness/scenarios.py` (render its caller audio once with
   `python -m voice_gateway.replay --warm`) and run it fake and live.

## What is missing

- **The AWS phone adapter.** Calls reach the gateway only through LocalAdapter and the WebSocket test line.
  The real line needs an AWS-only telephony path that streams audio both ways (Amazon Connect cannot);
  the candidate is the Amazon Chime SDK Voice Connector with origination to the gateway's SIP/RTP endpoint
  (G.711 mu-law 8 kHz, which `TELEPHONY_MULAW_8K` already handles) behind an NLB, plus Swedish number
  availability. It implements `audio/adapter.py: TelephonyAdapter` (accept, caller_frames, play, clear,
  hangup, transfer). Owner decision when Stage 2 goes ahead.
- **Deployment.** ECS Fargate ARM64, ECR, the SSM config, CloudWatch, and a new task role (Transcribe,
  Polly, Bedrock, the tool Lambdas, the PhoneAgent and tenant tables). That is an IAM change, and pushing
  images from CI touches the deploy role: both are the owner's call (DESIGN.md 10).
- **Live latency.** End of speech to first audio p50 1231 ms on booqy-dev (target 1500; REPLAY-RESULTS.md), the
  first sound being the opener; the reply itself p50 2.6 s, one Bedrock round (1.3 to 1.8 s to the first token)
  plus detection. Language-switch turns get no opener (the short disclosure must come first) and stay 1.4 to 4.7 s.
- **Tuning on real calls.** VAD, turn and backchannel thresholds are tuned on Polly voices over a
  simulated line; Arabic and Persian recognition are unverified on real callers; nobody has listened to
  the backchannel clips yet (owner decision 7: recorded human clips).
