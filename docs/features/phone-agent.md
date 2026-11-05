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
| Simulators | `shared/agent_model.py`, `shared/voice_provider.py`, `shared/telephony.py` | scripted intent table (sv, en), provider and SIP simulators; the `*_real.py` adapters are still skeletons |
| OpenAPI | `docs/openapi/phone-agent.yaml` | the 12 JWT routes; the provider webhooks live in `phone-agent-call` and are not in the fragment (its route check reads `functions/phone-agent/app.py` only) |

## Rules that matter

- **Disclosure by construction.** The greeting is `[Samtalet spelas in.] + "Hej, du pratar med <namn>, en
  AI-assistent för <restaurang>." + the owner's text`; it is the first agent turn, said with `say()`
  before the model gets a turn. `disclosure.playedAt` is set when the provider's agent transcript of it
  arrives; an interruption before then says the short form again (`repeats`), a new language says it once
  in that language (`languages`). Wording [NEEDS YOUR OK: lawyer].
- **Guards at call start**, in order: entitlement and location enabled (`entitlement`), tenant pause
  (`paused`), location or platform kill switch (`kill_switch`), agent hours unless mode `always`
  (`hours`), the period's minutes against the cap (`cap`). A refused call hears the refusal sentence,
  ends with 0 minutes and is listed.
- **State machine on the call row**, every transition conditional on `version` (and the from-status), so
  duplicate webhooks are no-ops. `processed` meters once: one transaction marks `metered` and adds the
  minutes to `USAGE#<periodEnd>` (`loc.<l>` too); a second end webhook or the sweep finds it processed.
  Minutes stop at an answered transfer.
- **Tools** invoke the existing functions on `:live` with a synthesized event; staff routes carry the
  agent principal (`role: staff_user`, `sub: agent:<l>`). `hold_table` is a bookability check (no write);
  `create_reservation` books through `POST .../reservations/manual` with `source: phone`; a table taken
  in between is re-offered from fresh availability. A party that would need a card guarantee online gets a
  callback (the manual route asks none; hotspot upgrade pending). `find_reservations` lists the next 14
  days and only the caller's number; move and cancel act only on a booking found that way.
- **Counter-offers** (decision 13): requested time; a nearby table (`adjacent_pair` within 1.0 m on the
  same floor, `larger`, `phone_only`, each switchable per location); the three closest same-day times;
  the next day once; then a callback. `counterOffer` on the call row and `PhoneCall.Ended`: the accepted
  offer, else the furthest step reached.
- **No phone numbers, names or transcript text in events or logs.** Callers are found for privacy hooks by
  `fromHash = sha256(tenant|phone)`.

## Deviations and open findings

- **Agent principal vs `staff_location()`**: CONTRACTS.md 4 writes `USER#agent:<l>` with `status: system`,
  but `shared/tenant.staff_location()` accepts only `status: active`, so in AWS every staff-route tool
  (booking, listing, moving, cancelling) would answer 404. Needs a hotspot decision (accept `system` for
  `agent:` subs, or write `active` and filter system users from staff lists). Tool tests use a fake
  invoker, so they do not hit this.
- **Cancel** uses the staff status route (`POST .../reservations/{id}/status`, `cancelled`) on
  `mark-arrived:live`, not `cancel-reservation` (a guest route that needs the manage-link token); the
  phone-agent role then needs no link-key secret.
- **Provider call ids**: after `call_started` the provider is expected to echo `tenantId` and `callId`
  (returned as dynamic variables by L14) in later events and turn requests; telephony `ended` events are
  matched by the called number and the call reference. To verify against ElevenLabs and 46elks.
- The turn endpoint answers one SSE chunk (buffered); true token streaming needs the Lambda Web Adapter
  image (OWNERSHIP L) and the real Bedrock adapter.
- `PhoneCall.Ended` is published after metering; `billing.usage` answers per-location minutes and the
  included minutes; the invoice lines are the platform-tenants hotspot (Run 2 hotspot window).

## Unverified

- Real providers: `shared/voice_provider_real.py` (ElevenLabs), `shared/telephony_real.py` (46elks),
  `shared/agent_model_real.py` (Bedrock Converse stream) are skeletons; nothing ran against them.
- Latency budget (no real model or voice); Swedish speech quality (Run 3 voice QA).
- Forwarding codes per operator; the 1.0 m gap and nearby-table interpretation; retention options;
  prices and included minutes: all **[NEEDS YOUR OK]**.
- `rotationY` is read as radians (three.js convention) for the nearby-table geometry.
