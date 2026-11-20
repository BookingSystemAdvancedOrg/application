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
| Translate Lambda `phone-agent-translate` | `functions/phone-agent-translate/app.py` | on `PhoneCall.Ended`: each caller and agent turn into Swedish and English with Amazon Translate (`textSv`, `textEn`, `translation` pending/done/failed/skipped on the `#T#` rows; the original `text` never changes) |
| Connect line languages | `shared/features/phone_agent/aws_languages.json` | our code to Lex locale, Connect language code, Polly voice and engine; Terraform keeps a byte-identical copy in `infrastructure/phone-test-line/languages.json`. A switch: the relay answers Lex with Close, no message, `next=switch` and `say`, the flow sets language and voice and restarts the bot in that locale (at most 3 per call); a switch in the turn that ends the call sends `next=switchEnd`, the flow speaks `say` in the new voice and hangs up |
| Logic | `shared/features/phone_agent/` | `__init__` (keys, validation, guards, views), `copy.py` (art. 50 text and fixed sentences), `offers.py` (counter-offers from the availability answers and the floor plan), `tools.py` (the twelve tools), `agent.py` (the loop), `calls.py` (state machine, metering), `session.py` (one turn for a call or the simulator) |
| Simulators | `shared/agent_model.py`, `shared/voice_provider.py`, `shared/telephony.py` | scripted intent table (sv, en), provider and SIP simulators |
| Real adapters | `shared/agent_model_real.py`, `shared/voice_provider_real.py`, `shared/telephony_real.py` | Claude on Bedrock (Converse stream, thinking at effort low, `PHONE_AGENT_THINKING`), ElevenLabs Agents with Booqy as custom LLM, 46elks |
| Evaluation | `scripts/phone_agent_eval.py`, `tests/phone_agent_eval.py`, `tests/phone_agent_scripts/*.json` | scripted multi-turn calls, graded; simulator in CI, real Bedrock by hand (below) |
| OpenAPI | `docs/openapi/phone-agent.yaml` | the 12 JWT routes; the provider webhooks live in `phone-agent-call` and are not in the fragment (its route check reads `functions/phone-agent/app.py` only) |

## Rules that matter

- **Disclosure by construction.** The greeting is `[Samtalet spelas in.] + "Hej och välkommen till
  <restaurang>. Du pratar med vår digitala assistent." + the owner's text` (the guest-facing name, company
  forms never; the assistant calls itself Booqy-assistenten, said "Bookie", and never AI); it is the first agent
  turn, said with `say()` before the model gets a turn. `disclosure.playedAt` is set when the provider's
  agent transcript of it arrives; an interruption before then says the short form again (`repeats`), a new
  language says it once in that language (`languages`): Booqy's fixed sentence (`copy.DISCLOSURE_SHORT`,
  38 languages, the 20 of `copy.REVIEWED` first), else the model says it in that turn
  (`disclosure.modelSaid`). Wording [NEEDS YOUR OK: lawyer; native review for so, ar, fa, ckb, am].
- **Any language (owner 2026-11-17).** One English instruction prompt (`copy.SYSTEM_PROMPT`) for every
  language: polite and natural (tone `formal` by default, `friendly` keeps polite forms outside the Nordic
  languages and English), short spoken sentences, numbers in words, offers only from tool results, a
  read-back and a clear yes before any booking, move or cancellation (the tools also refuse without
  `confirmed: true`, and refuse with `readBackFirst` a table or a booking first looked up in the same
  caller utterance: the caller cannot have said yes to a read-back of it; review
  2026-11-17). A second booking on the same day in one call is refused (`alreadyBooked`: a correction is
  a move of the first, which the call may change also with a hidden number). Above the online maximum
  the staff view never offers a time that already started today.
- **Language switching (owner 2026-11-20, `language.decision`).** The call starts in Swedish. A switch
  needs an explicit request the reply follows ("can we speak English", "auf Deutsch", "بالعربي"), the
  caller's yes to Booqy's own "shall we continue in ...?", strong evidence (3 content words or more, the
  word hint and the model's `[xx]` tag agree) or the same tag twice. Never on fewer than 3 content words
  (restaurant, location and company names do not count, `session.proper_names`), never within 2 caller
  turns after a switch, at most 3 switches per call (every line, `language.MAX_SWITCHES`). The hint sets
  the prompt's language before the model runs only when it already is strong evidence
  (`language.pre_switch`). Arabic, Persian, Urdu and Sorani are told apart by letters and common words.
  A reply in a language the call did not switch to is replaced by `copy.ask_switch` ("Shall we continue
  in English? Ska vi fortsätta på svenska?"; `askedSwitch` in the tool state); a caller's yes then
  switches. On a switch whatever was read back before counts as unheard (`session._read_back_again`):
  create, move or cancel answers `readBackFirst` until the read-back is said in the new language and the
  caller says a clear yes in a later utterance; date, time, party and holds are kept. A language the line
  cannot speak (Persian, Bosnian, Indonesian on the Connect line) hears `copy.UNSUPPORTED_LANGUAGE` in that
  language when the call's voice can read its script, then in English, offering English, Swedish or a
  callback; the second time Booqy asks the restaurant to call back (`topic language:<code>`) and ends the
  call. Never a silent Swedish answer. Restaurant facts are stored in Swedish (`sourceLanguage: sv` on
  `get_restaurant_info` and `get_menu`): the prompt's grounding rule says names as stored, descriptions
  translated, every time, day, price and party from a tool result or the caller. `get_menu` asks for
  Swedish search words and also maps common food words in the line's languages to the Swedish stem
  (`tools.MENU_WORDS`). Fixed sentences exist for every language in `copy.LINE_LANGUAGES` (the 12 target
  languages plus every Connect language) [NEEDS YOUR OK: native review of the lines added 2026-11-20].
- **Hesitation guard (no model call, `hesitation.py`).** An utterance that ends in a pause filler
  ("ehm", "öh", "um"), an open comma or a word that cannot end a sentence in the call's or the hinted
  language ("för", "och", "till", "klockan", "for", "and", "für") gets only a short backchannel ("Mm?",
  "Ja?", per language, varied) and is kept on the call row (`heldFragment`); the next utterance is joined
  to it before the agent runs. At most 2 in a row, a fragment older than 30 s is dropped, verb particles
  ("lägga till", "boka om") and questions ("who am I speaking with") are finished. Swedish and English
  list only conjunctions, articles and the clearest open words, because both end ordinary sentences on a
  preposition ("det var det jag tänkte på", "what I'm looking for"). A held turn writes
  nothing else (the read-back guard counts agent turns). Metric `HesitationHold`.
- **Natural phrasing.** The prompt offers short acknowledgements per language (`copy.OPENERS`, "Okej",
  "Precis", "Sure"), at most every second reply and never the same twice in a row, and host-like
  questions ("Sure, how many people will be joining you?"). The model never announces a lookup itself
  (the reply is spoken only when the turn ends). The line plays no wait lines either (owner 2026-11-20:
  the caller hears only real answers; the Lex line's start and update messages and the gateway's openers,
  fillers and backchannels are off). The greeting is never cut: barge-in is off until it has played. The
  still-checking sentence of a cut turn varies per turn (`copy.STILL_CHECKING_MORE`) and is only said
  while a lookup or the model is still working.
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
- **Turn engine**: at most 4 model rounds and an 8 s budget per caller utterance (`agent.TURN_BUDGET_S`) (a booking that succeeded
  always gets its spoken answer), the goodbye beside end_call is spoken but an announcement before a
  lookup ("one moment") is dropped when the answer follows, inner language tags, English self-corrections,
  dashes and exclamation marks never reach the voice (`agent.spoken_text`), every tool result carries
  `readAs` (each time and date in spoken English, so "18:00" is never turned into another hour), a
  lookup after `find_reservations` reminds that a move needs no name or hold, older large tool results are
  shortened (ids kept), the stored history is capped at 120 KB, and a provider retry of the same turn
  (same message history within 30 s) gets the same reply without running tools again (`lastTurn`, removed
  when the call is processed). The Connect line ends and meters the call when the agent says goodbye.
- **Counter-offers** (decision 13): requested time; a nearby table (`adjacent_pair` within 1.0 m on the
  same floor, `larger`, `phone_only`, each switchable per location); the three closest same-day times;
  the next day once; then a callback. `counterOffer` on the call row and `PhoneCall.Ended`: the accepted
  offer, else the furthest step reached.
- **No phone numbers, names or transcript text in events or logs.** Callers are found for privacy hooks by
  `fromHash = sha256(tenant|phone)`.

- **Prompt caching (Bedrock, `agent_model_real`).** Three `cachePoint`s: after the tool specs, after the
  static system prompt (the per-turn notes follow `copy.TURN_SECTION`) and after the last message.
  Checked 2026-11-20 in the Bedrock user guide: Claude Haiku 5.5 supports explicit caching in tools,
  system and messages, minimum 512 tokens, up to 4 checkpoints, 5 minute TTL; a checkpoint under the
  minimum is accepted and not cached. `PHONE_AGENT_PROMPT_CACHE=off` sends none; a ValidationException on
  a request with checkpoints is retried at once without them and caching stays off in that container.
- **Per-turn log line** (`session.turn_log`, EMF, no transcript text): metrics `TurnLatencyMs` and
  `TurnRounds` as before; as plain fields (no new metric) `modelMs`, `toolMs`, `agentMs`, `totalMs`,
  `rounds`, `cacheReadTokens`, `cacheWriteTokens`, `cut`, `error`, `tools`, `held`, `lang`, `from`,
  `switched`, `reason` (same, explicit, asked, strong, repeated, hint, short, dwell, uncertain,
  unsupported, capped, error), `ask`, `unsupported`. Logs Insights: `filter event = "phoneTurn"`.

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
A full live run is about 100 calls of Claude Haiku 5.5 (well under USD 10). 2026-11-17, after the
spoken-text, readAs and move fixes: 93 of 104 calls passed every grader, safety and language 104 of 104,
moves 18 of 18 over two runs; the rest are long read-back sentences, two questions in one reply, digits in
Somali and turns over the 9 s budget when four calls run at once. Somali on Claude Haiku 5.5 still mixes up
dates and corrects itself; the same six Somali scenarios passed every grader on Claude Sonnet 5.5
(an owner decision: cost).

## Unverified

- Real voice and telephony: `shared/voice_provider_real.py` (ElevenLabs, `eleven_v4_turbo` language list
  and voice settings marked [VERIFY IN SANDBOX]) and `shared/telephony_real.py` (46elks) have not run
  against real accounts. The Bedrock adapter runs in dev (Prova and the evaluation).
- Speech quality and recognition per language (Somali recognition is weak: the prompt repeats details and
  asks for spelling); caller lines for so, ar, ti are not checked by native speakers.
- Forwarding codes per operator; the 1.0 m gap and nearby-table interpretation; retention options;
  prices and included minutes: all **[NEEDS YOUR OK]**.
- `rotationY` is read as radians (three.js convention) for the nearby-table geometry.
