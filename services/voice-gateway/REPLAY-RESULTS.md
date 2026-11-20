# Live replay results (dev)

Run 2026-11-20, about 04:30 to 05:05 local, `VG_WORLD=dev python -m voice_gateway.replay` in live mode: real
Transcribe streaming (eu-central-1), Polly (eu-central-1), Bedrock `eu.anthropic.claude-haiku-5-5` and the real dev
tables and tool Lambdas of Restaurant 1 (dialed +46100000001, caller +46700000000). SMS notices and events recorded,
never sent. Silero VAD (pinned model). Caller audio: the cached Polly caller voices over the simulated 8 kHz mu-law
line. lang-C (Persian, no AWS voice) ran with `VG_STT=fake`: its recogniser is scripted, Polly, Bedrock and the tools
are real.

**Verdict: 0 of 17 scenarios pass every check live.** Every scenario fails the latency check (end of speech to first
assistant audio, target p50 1500 ms). Apart from latency, 8 pass every behaviour check, 9 fail one or more for the
reasons in the table. No booking was written in any run, and no write happened without a clear yes.

Run 1 was every scenario on the code before this pass. The fixed scenarios were run again on the fixed code
(run 2: voice-A, lang-B, lang-C, lang-E, lang-K; run 3: voice-A, lang-C, lang-E, lang-I; run 4: voice-A). The table
shows the last run of each scenario.

## Scenarios

| Scenario | Run | Behaviour checks | End of speech to first audio per turn (ms) | What happened |
|---|---|---|---|---|
| voice-A hesitation | 4 | pass | 9134 | "för, ehm... fyra" held across the 1.5 s pause after the fix (run 1: cut off at the "ehm"). The first Bedrock round took 3.9 s before the tool call; the filler came 5.9 s after the caller stopped |
| voice-B backchannel | 1 | FAIL backchannels 0 (want 1 to 2) | 6157 | The caller's sentence pauses were 450 ms; the shortest trimmed "mm" is 320 ms and fires 250 ms into a pause, so the fit rule refused every one (0 overlaps, as designed) |
| voice-C correction | 1 | pass | 4377, 5933 | Correction to five heard; barge-in detected after 523 ms, line quiet 0 ms after |
| voice-D interruption | 1 | pass | 6481, 2483, 1954 | Stop word barge-in detected after 126 ms, line quiet 0 ms after; read-back repeated |
| voice-E booking with fee rule | 1 | FAIL booked, fee_rule | 2708, 2654, 2635, 2470 | Transcribe dropped "för åtta personer" from the first sentence, the agent kept asking for the party size and the scripted answers no longer fitted |
| voice-F unavailable time | 1 | pass | 4552, 2649 | No invented times, no booking, call ended |
| lang-A Swedish | 1 | FAIL booked | 4269, 3398, 4739 | Transcribe wrote "för tre" as "för 15:00 15:00"; the agent asked which time was meant and the scripted caller fell out of step |
| lang-B English after greeting | 2 | FAIL booked | 2756, 2254, 3556 | English from the first reply; hold made after "Yes, that is right"; the script ends at the final read-back, so no yes for the write |
| lang-C Persian (fake STT) | 3 | pass | 7745, 6560 | Unsupported sentences in English, callback `language:fa` after the fix (run 2: the booking ran on Persian text) |
| lang-D Arabic | 1 | FAIL booked | 5143, 4616, 3083 | Arabic from the first reply; hold made; script ends before the final yes |
| lang-E English to Swedish | 3 | FAIL final_language, switches | 3694, 2460 | Swedish on the English stream came back as English nonsense; no switch back (see Gaps) |
| lang-F uncertain | 1 | pass | 2725, 5461 | Stayed Swedish; "Okay." handled as poor audio (repeat) |
| lang-G context across switch | 1 | pass | 5092, 3459, 7985, 5345 | Explicit "continue in English" switched at once; hold kept |
| lang-H interruption keeps language | 1 | pass | 4978, 5486, 2268 | Barge-in detected after 185 ms, line quiet 0 ms after; stayed English |
| lang-I German | 3 | FAIL booked | 4549, 3823, 4452 | German from the first reply; caller heard in German after the fix (run 1: a German "Ja, das stimmt" over the reply was transcribed on the Swedish stream); script ends before the final yes |
| lang-J Turkish | 1 | FAIL booked, final_language | 3150, 4859, 2188 | Turkish is not among the five identify candidates for a +46 caller (sv, en, ar, de, pl): heard as English nonsense, call went English |
| lang-K Polish | 2 | FAIL booked | 1800, 3428, 3847 | Polish found by the identify stream on the first utterance (pl 1.00); hold made; script ends before the final yes |

## Metrics (last run of each scenario, 44 answered turns)

| Metric | Measured | Target |
|---|---|---|
| End of speech to first assistant audio | p50 3835 ms, p95 7567 ms (min 1800, max 9134) | p50 under 1500 |
| of which turn detection (end of speech to turn end) | p50 900 ms, p95 1240 ms | 350 to 1600 by tail |
| of which turn end to first audio (Bedrock rounds, tools, Polly) | p50 2910 ms, p95 7007 ms | |
| Bedrock time to first token, same prompt cached (checked apart) | 1.3 to 1.8 s; the cache works (cacheReadInputTokens) | |
| Time to first backchannel | 5825 ms after the caller began (1 backchannel in all runs, lang-G) | 1 or 2 per 8 s monologue |
| Backchannels over the caller | 0 | 0 |
| Barge-in detection, caller onset to BargeIn | 126, 185, 523 ms | |
| Barge-in stop, BargeIn to quiet line | 0, 0, 0 ms | under 300 |
| Language, correct final language | 9 of 11 (lang-A..K; lang-C as unsupported fa) | 11 of 11 |
| Language, end of the first utterance to the first reply audio in the caller's language | pl 1.8 s (identify stream, the short disclosure in Polish first), en 2.8 s, de 4.5 s, ar 5.1 s, en by explicit request 3.5 s | |
| Language, identify-stream result before the turn ended | 1 of 6 first utterances in another language (pl); the other 5 switched on the model's tag | |
| Bookings written | 0 in all runs | |
| Booked only after a clear yes | held: no write in any run, holds only after a yes to the offered time | |
| Stored time equals the read-back | not measurable: nothing was stored | |

## Fixed in the gateway (commit 46b2f0d)

- Turn detection: Transcribe leaves "ehm" out, so the turn ended 1.4 s after "för" inside the caller's pause. A short
  voiced burst after a pause whose words all end before its middle now counts as a hesitation (filler wait, the
  longest after a dangling word). STT events carry the last word's end time for this.
- Reconcile: a clause streamed before a held done claim ("A text message ... to that number,") was spoken again inside
  the reply's whole sentence (lang-B, lang-K). Only the rest is spoken now.
- Language: the listener and speaker followed the brain's switch only after the whole reply had played, so a caller
  talking over a long reply was heard in the old language (lang-I). They follow at turn done now.
- Filler: an English caller after the Swedish greeting heard a Swedish filler; no filler now when the caller's words
  point to another language than the call's.
- Unsupported language: a weak identify guess (sv 0.40) vetoed the Persian script hint and the booking ran on text
  nobody could read (lang-C). A guess under 0.5 no longer vetoes it.
- Probe: Transcribe answers replayed audio in real time (a 5.0 s utterance fed at 3x or 6x gave its final 5.1 to 5.6 s
  after the feed began, plus 0.8 s to open), so the 2.5 s probe never finished on a normal utterance and only added
  2.5 s to the turn. It runs only on utterances up to 1 s now; away from the start language it is asked for at an ASR
  confidence under 0.7 (wrong-language text measured 0.66, right language 0.75 to 1.00).

Tests: 552 passed, 10 skipped (full gateway suite); fake replay 17 of 17 pass; root CI environment 526 passed.

## Not fixed (findings for the owner)

1. **Latency** is the agent's Bedrock rounds: about 1.3 to 1.8 s to the first token even with the prompt cached, and a
   tool turn needs two rounds. With turn detection (p50 900 ms) the 1.5 s target is out of reach on this model. Levers:
   a faster model, a filler for long tool-free first rounds (voice-A waited 5.9 s in silence), shorter first sentences.
2. **Transcribe sv-SE number formatting**: "tre" became "15:00", "fyra" "16:00", "sju" "17" or "17:00". Party sizes
   turn into clock times and "klockan sju" into 17. The read-back catches it, but calls get longer. Worth a look before
   real calls (custom vocabulary or a gateway rewrite of a clock time right after "för" or "bord för").
3. **Turkish and other languages outside the five identify candidates** (sv, en, ar, de, pl for a +46 caller) are not
   recognised (lang-J). Which five to use for Swedish numbers is a product choice; a second probe set needs a
   probe that does not wait in real time.
4. **Going back to the start language** (lang-E) needs an identify stream that runs alongside the utterance (opened at
   speech start while the call is away from Swedish), since a probe after the turn cannot finish in time. More
   Transcribe minutes; owner decision.
5. **Backchannels** do not fit 450 ms sentence pauses with Polly clips (320 ms shortest after the trim). Recorded
   human clips (owner decision 7) or a shorter wait inside the pause.
6. **The scripted callers are not reactive**: in 4 scenarios (lang-B, D, I, K) the agent's flow (fee question, final read-back) is one
   step longer than the script, so the run ends before the final yes. A reactive caller step ("answer yes to a
   read-back") would let the live replay measure the stored time against the read-back.
7. awscrt prints an `InvalidStateError` traceback when a Transcribe stream is cancelled while a chunk arrives; harmless,
   noisy.

## Dev data

Before this run, 18 calls of the earlier interrupted runs (2026-11-20 02:16 to 02:35 local, `connect-gw-harness-*`,
caller +46700000000) were deleted with their 137 transcript rows, and the usage row of Restaurant 1 was lowered by their
23 minutes and 18 calls. This run's 26 calls, 201 transcript rows and 2 callback rows were deleted after it, and the
usage row lowered by 36 minutes and 26 calls. Rows of other sessions on the same tenant and number were left alone.
No reservation, PENDING, RID or LINK row was written by either. WAV recordings stay in `tests/out/` (git-ignored).
