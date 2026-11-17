# AI phone agent (feature L, `phoneAgent`)

ADR: `infrastructure/docs/adr/ADR-L-phone-agent.md` (decision 13: counter-offers). Contract: CONTRACTS.md
6.L, 4, 5, 9, 13, 17. Off by default in every plan; nothing answers a phone until the owner enables a
location and forwards the restaurant's number.

## Pieces

| Piece | Where | What |
|---|---|---|
| API Lambda `phone-agent` | `functions/phone-agent/app.py` | routes L1-L12; hooks `platform` (L15-L18), `privacy.export`, `privacy.erase`, `tenant.purge`, `billing.usage`; number provisioning on the first enable |
| Real-time Lambda `phone-agent-call` | `functions/phone-agent-call/app.py` | provider webhooks L13/L14, the agent turn endpoint (Function URL, `POST /v1/chat/completions`, header `x-booqy-agent-key`), guards, disclosure, metering |
| Jobs Lambda `phone-agent-jobs` | `functions/phone-agent-jobs/app.py` | 5-minute sweep (max length, unprocessed ends), nightly retention and number release |
| Logic | `shared/features/phone_agent/` | `__init__` (keys, validation, guards, views), `copy.py` (art. 50 text and fixed sentences), `offers.py` (counter-offers from the availability answers and the floor plan), `tools.py` (the twelve tools), `agent.py` (the loop), `calls.py` (state machine, metering), `session.py` (one turn for a call or the simulator) |
| Simulators | `shared/agent_model.py`, `shared/voice_provider.py`, `shared/telephony.py` | scripted intent table (sv, en), provider and SIP simulators |
| Real adapters | `shared/agent_model_real.py`, `shared/voice_provider_real.py`, `shared/telephony_real.py` | Claude on Bedrock (Converse stream, thinking at effort low, `PHONE_AGENT_THINKING`), ElevenLabs Agents with Booqy as custom LLM, 46elks |
| Evaluation | `scripts/phone_agent_eval.py`, `tests/phone_agent_eval.py`, `tests/phone_agent_scripts/*.json` | scripted multi-turn calls, graded; simulator in CI, real Bedrock by hand (below) |
| OpenAPI | `docs/openapi/phone-agent.yaml` | the 12 JWT routes; the provider webhooks live in `phone-agent-call` and are not in the fragment (its route check reads `functions/phone-agent/app.py` only) |

## Rules that matter

- **Disclosure by construction.** The greeting is `[Samtalet spelas in.] + "Hej och välkommen till
  <restaurang>. Du talar med <namn>, restaurangens AI-assistent." + the owner's text`; it is the first agent
  turn, said with `say()` before the model gets a turn. `disclosure.playedAt` is set when the provider's
  agent transcript of it arrives; an interruption before then says the short form again (`repeats`), a new
  language says it once in that language (`languages`): Booqy's fixed sentence (`copy.DISCLOSURE_SHORT`,
  38 languages, the 20 of `copy.REVIEWED` first), else the model says it in that turn
  (`disclosure.modelSaid`). Wording [NEEDS YOUR OK: lawyer; native review for so, ar, fa, ckb, am].
- **Any language (owner 2026-11-17).** One English instruction prompt (`copy.SYSTEM_PROMPT`) for every
  language: polite and natural (tone `formal` by default, `friendly` keeps polite forms outside the Nordic
  languages and English), short spoken sentences, numbers in words, offers only from tool results, a
  read-back and a clear yes before any booking, move or cancellation (the tools also refuse without
  `confirmed: true`). The call starts in Swedish; `language.hint()` sets the prompt's language before the
  model runs, the model's `[xx]` tag decides after it (`language.decide()`: one word, a name or a street
  never switches; a caller asking for a language does). The call may switch to every language the voice
  model speaks (`language.voice_languages(PHONE_AGENT_TTS_MODEL)`: 32 with flash v2.5, Somali, Persian,
  Amharic, Sorani and more with `eleven_v4_turbo`). A language the voice cannot speak hears
  `copy.UNSUPPORTED_LANGUAGE` in Swedish, English and Arabic; the second time Booqy asks the restaurant to
  call back (`topic language:<code>`) and ends the call. The Connect test line stays Swedish (Lex and Polly
  cannot switch).
- **Guards at call start**, in order: entitlement and location enabled (`entitlement`), tenant pause
  (`paused`), location or platform kill switch (`kill_switch`), agent hours unless mode `always`
  (`hours`), the period's minutes against the cap (`cap`). A refused call hears the refusal sentence,
  ends with 0 minutes and is listed.
- **State machine on the call row**, every transition conditional on `version` (and the from-status), so
  duplicate webhooks are no-ops. `processed` meters once: one transaction marks `metered` and adds the
  minutes to `USAGE#<periodEnd>` (`loc.<l>` too); a second end webhook or the sweep finds it processed.
  Minutes stop at an answered transfer.
- **Tools** invoke the existing functions on `:live` with a synthesized event; staff routes carry the
  agent principal (`role: staff_user`, `sub: agent:<l>`, status `system`, accepted by
  `tenant.staff_location()` for `agent:` subs only; `staff_access` gives it `bokningar` and `layout`
  whatever the staff defaults say). Before any read the tools answer a past date (`pastDate`), a date past
  `availability.last_bookable_date()` (`beyondHorizon`, 21 days), a party above `phoneMaxPartySize`
  (`largeParty`, default the online maximum; between the two the staff view books it) and a card
  guarantee (`guaranteeRequired`, before any read-back; the manual route asks none, hotspot upgrade
  pending) with a `say` hint. A full day lists `nextDays` (four days read in parallel). `hold_table` is a
  bookability check (no write, no table ids in the answer); `create_reservation` books through
  `POST .../reservations/manual` with `source: phone`, under the caller ID (a spoken number only when it is
  hidden); 409 re-offers from fresh availability, a 400 is never called a taken time.
  `find_reservations` reads the whole horizon (parallel) and only the caller's number; move (checked
  against availability first) and cancel act only on a booking found that way.
- **Turn engine**: at most 4 model rounds and a 9 s budget per caller utterance (a booking that succeeded
  always gets its spoken answer), text beside a tool call is spoken, older large tool results are
  shortened (ids kept), the stored history is capped at 120 KB, and a provider retry of the same turn
  (same message history within 30 s) gets the same reply without running tools again (`lastTurn`, removed
  when the call is processed). The Connect line ends and meters the call when the agent says goodbye.
- **Counter-offers** (decision 13): requested time; a nearby table (`adjacent_pair` within 1.0 m on the
  same floor, `larger`, `phone_only`, each switchable per location); the three closest same-day times;
  the next day once; then a callback. `counterOffer` on the call row and `PhoneCall.Ended`: the accepted
  offer, else the furthest step reached.
- **No phone numbers, names or transcript text in events or logs.** Callers are found for privacy hooks by
  `fromHash = sha256(tenant|phone)`.

## Deviations and open findings

- **Cancel** uses the staff status route (`POST .../reservations/{id}/status`, `cancelled`) on
  `mark-arrived:live`, not `cancel-reservation` (a guest route that needs the manage-link token); the
  phone-agent role then needs no link-key secret.
- **Provider call ids**: after `call_started` the provider is expected to echo `tenantId` and `callId`
  (returned as dynamic variables by L14) in later events and turn requests; telephony `ended` events are
  matched by the called number and the call reference. To verify against ElevenLabs and 46elks.
- The turn endpoint answers one SSE chunk (buffered); true token streaming (and a spoken "one moment"
  before slow tools) needs the Lambda Web Adapter image (OWNERSHIP L).
- CONFIG `languages` is not a filter: the admin has no language picker and every saved config lists the
  languages of its day, so honouring it would shut out languages added since.
- `PhoneCall.Ended` is published after metering; `billing.usage` answers per-location minutes and the
  included minutes; the invoice lines are the platform-tenants hotspot (Run 2 hotspot window).

## Evaluation

    python scripts/phone_agent_eval.py                     # simulator, sv and en (CI runs the same)
    AWS_PROFILE=booqy-dev python scripts/phone_agent_eval.py --live eu.anthropic.claude-haiku-5-5
    AWS_PROFILE=booqy-dev python scripts/phone_agent_eval.py --live eu.anthropic.claude-haiku-5-5 \
        --langs so,sv>so --scenarios 01,10 --verbose      # replies to this terminal only

Thirteen scenarios (booking, other time, full day, horizon and past date, large party, guarantee,
cancel with and without caller ID, move to a taken time, mishearing, switch mid-call, false switch,
unsupported language, unclear yes) in sv, en, de, ru, so, ar, fi, pl and tr; graders safety, outcome,
language, and live also style, register and latency. The report holds codes and timings, no transcripts.
A full live run is about 100 calls of Claude Haiku 5.5 (well under USD 10). 2026-11-17: 92 of 104 calls
passed every grader, safety and language 104 of 104; the rest are digits in Somali replies, two questions
in one reply and run-to-run variance in the move scenario.

## Unverified

- Real voice and telephony: `shared/voice_provider_real.py` (ElevenLabs, `eleven_v4_turbo` language list
  and voice settings marked [VERIFY IN SANDBOX]) and `shared/telephony_real.py` (46elks) have not run
  against real accounts. The Bedrock adapter runs in dev (Prova and the evaluation).
- Speech quality and recognition per language (Somali recognition is weak: the prompt repeats details and
  asks for spelling); caller lines for so, ar, ti are not checked by native speakers.
- Forwarding codes per operator; the 1.0 m gap and nearby-table interpretation; retention options;
  prices and included minutes: all **[NEEDS YOUR OK]**.
- `rotationY` is read as radians (three.js convention) for the nearby-table geometry.
