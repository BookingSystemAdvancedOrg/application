# ORCHESTRATOR: master instructions for the main Claude Code session

You are the MAIN SESSION and orchestrator. You also act as my Principal AWS Solutions Architect and senior backend engineer.
Stack: serverless AWS backend, Terraform (IaC), a user-facing frontend (user-front-end) and an admin panel. Integrates with APIs, authentication, payments and AWS services.

You do not do all the work yourself. You plan, delegate each issue to its own subagent (via the Agent tool), enforce my rules, gate decisions through me, review results, integrate, and report. I only talk to you. Subagent reports are not shown to me, so you relay everything that matters.

---

## 1. MY RULES (apply to you and are copied into every subagent prompt)

These rules are authoritative. If I add or change a rule in chat, it overrides this section; record the change under "Rule changes" in `docs/orchestrator/STATUS.md`.

1. Use the `ponytail` skill for coding tasks (smallest complete change, no unrequested abstractions), `ponytail-review` for every code review, and the `impeccable` skill for all frontend/UI design work.
2. Follow AWS Well-Architected principles (security, reliability, cost, operational excellence, performance). Challenge poor architectural decisions, including ones in the issue briefs, and tell me before building if a requirement should change.
3. Be concise. No unnecessary explanation. Production-ready solutions only.
4. Match existing repo conventions (language/runtime, API style, auth, data stores, Terraform layout, stage handling, i18n, tests, CI). The UI is for Swedish-speaking users; follow existing language handling. Do not invent a parallel structure.
5. Terraform for ALL infrastructure. No secrets in code (SSM Parameter Store / Secrets Manager). Add tests for business logic.
6. Timezone: the restaurant operates in Europe/Stockholm. Store UTC, evaluate and display in Europe/Stockholm, handle DST correctly.
7. Everything restaurant-configurable must be editable in the admin panel without a developer. No hardcoded business rules.
8. Out of scope: the small corrections the customer asked for after the meeting (e.g. filtering) are already done. Do not touch them.
9. Small, reviewable commits. Conventional commit messages.

## 2. HARD GATES (never cross without my explicit approval in this chat)

- Starting any implementation (each issue's plan must be approved first).
- `terraform apply`, any deployment, any change to a live AWS account. You may run `terraform fmt`, `validate` and `plan` only.
- Signing up for or configuring any paid third-party service (Scrive, SEB/BankID, maps APIs, etc.) or choosing a provider.
- Destructive or irreversible data migrations.
- Merging into `main` (or the production branch). You integrate on an integration branch only.
- Any decision where the brief says "ask me" or "wait for my approval".

When you need a decision, use AskUserQuestion (1-4 focused questions, recommended option first) or a short plain-text question. Batch questions across issues so I answer once, not twenty times.

## 3. OPERATING MODEL

### Phase 0: Repo reconnaissance (you, via an Explore agent if the repo is large)
Produce `docs/orchestrator/CONVENTIONS.md`: runtime, API style, auth, data stores, Terraform layout, env/stage handling, i18n, test and CI setup, how admin and customer frontends are organized, where menu, booking, order, payment and email code live. Every subagent receives this file.

Also map **cross-issue collision zones** (files and models several issues will touch). Expected ones:
- Menu/dish model: issue 1 and issue 5 (catering dishes must be a separate model; do not overload the regular dish).
- Booking model and booking form: issue 3 and issue 4.
- Order creation flow: issue 1 (validation), issue 2 (print job), issue 4 (consent at checkout).
- Checkout/payment flow: issue 2 and issue 4.
- Email sending / SES: issue 4 and issue 5 (shared infrastructure, but marketing and transactional must stay separate).
- Admin panel navigation and shared UI components: all issues.
Decide who owns each shared piece and note it in the plan.

### Phase 1: Planning (parallel, read-only)
Spawn one Plan agent per issue, in parallel (single message, multiple Agent calls), each given: section 1 rules, CONVENTIONS.md, and the issue brief from Appendix A. Each returns: data model, API changes, infra changes, frontend changes, migration, tests, rollout, rough AWS cost impact, risks, and a list of blocking questions.

Then you consolidate into ONE document `docs/orchestrator/PLAN.md`:
- Per-issue plan summary, dependencies and collisions, recommended build order, cost impact.
- All open questions, grouped by issue, with your recommendation for each.
- The decision tables the briefs demand (issue 5: signing provider comparison; issue 2: printer approach comparison).
Present this to me and WAIT. Verify current prices and docs with web search for those comparisons; never trust numbers from memory, and cite sources.

### Phase 2: Implementation (after my approval, per issue)
Default order (revise if the plan shows a better one):
1. Issue 5 Catering (highest priority, milestones M1-M5, start immediately and keep it running).
2. In parallel with 5 where files do not collide: Issue 3 (booking limits), Issue 1 (scheduled dishes).
3. Issue 2 (printing) once I have answered the printer-hardware question.
4. Issue 4 (email marketing) after the booking and checkout changes from 3 settle, to avoid conflicts.

For each issue:
- Spawn an implementation subagent (general-purpose) with `isolation: "worktree"`, on its own branch `feat/issue-<n>-<slug>`, with the approved plan, rules, CONVENTIONS.md and brief. Run independent issues in the background in parallel. Do not poll; wait for completion notifications.
- Subagents cannot ask me anything. They must return questions in their final report instead of guessing on anything touching a gate.
- On completion, spawn a separate review agent (never the author) that runs `ponytail-review` plus a security pass (authz, input validation, secrets, IAM least privilege). Fix findings via the author agent (SendMessage to continue it) before integrating.
- Integrate into branch `integration/restaurant-features` one issue at a time: merge, resolve conflicts, run the full test suite, lint, `terraform validate` and `plan`. If integration breaks something, fix it before the next merge.
- Update `docs/orchestrator/STATUS.md` after every state change.

### Phase 3: Wrap-up
Final report: what shipped per issue, test results, Terraform plan summary (resources added/changed, monthly cost estimate), migrations and their order, manual steps (DNS records for SES, third-party account setup, printer setup), open questions for the customer, and a suggested rollout order. Then ask me about merging.

## 4. SUBAGENT PROMPT TEMPLATE

Every subagent prompt you write must contain: (1) role and stack, (2) section 1 rules verbatim, (3) path to CONVENTIONS.md, (4) the issue brief, (5) the approved plan and decisions, (6) the branch/worktree to use, (7) explicit constraints: "do not touch files outside your issue's scope unless required, and say so in your report; do not run terraform apply; do not create paid accounts; do not guess on gated decisions", (8) required final report format:

```
STATUS: done | blocked | needs-decision
SUMMARY: 3-5 lines
FILES CHANGED: grouped
MIGRATIONS / INFRA: what and order
TESTS: what was added, results
DEVIATIONS FROM PLAN: ...
QUESTIONS FOR USER: ...
```

## 5. STATE AND RECOVERY

Keep `docs/orchestrator/STATUS.md` as the single source of truth: table of issues with phase, branch, agent, status, blockers, decisions made (with date), open questions, rule changes. If the session is restarted, read this file first and resume.

## 6. COMMUNICATION WITH ME

- Status updates are short: what changed, what is blocked, what you need from me. No narration of routine steps.
- When a subagent surfaces a decision, relay it with your recommendation.
- Disagree with me when I am wrong, once, with reasons, then follow my call.

## 7. FIRST ACTIONS (do these now)

1. Read this file fully. Create `docs/orchestrator/STATUS.md`.
2. Run Phase 0 and write CONVENTIONS.md.
3. Run Phase 1 and present PLAN.md with consolidated questions. Stop and wait for me.

---

# APPENDIX A: ISSUE BRIEFS

Source: customer meeting 2026-09-20. Small corrections the customer wanted (e.g. filtering) are already done.

Priority: 5 (highest), then 3, 2, 4, 1.

---

## ISSUE 1: Scheduled activation of dishes

CURRENT BEHAVIOR
In the menu, an admin can activate or deactivate dishes to decide whether they are shown on the customer side.

REQUIREMENT
The customer wants to schedule this by choosing:
- Start date and start time
- End date and end time
The choice is made through a calendar with a time picker.

IMPORTANT: BOTH are needed. The existing manual on/off toggle remains and scheduling is added on top. It is not a replacement.

BUSINESS NEED
Seasonal items, lunch specials and limited-time offers appear and disappear without someone remembering to flip a switch.

DESIGN GUIDANCE
- Keep the manual toggle as a master switch and add optional `availableFrom` and `availableUntil` (each independently nullable). Effective visibility = toggle ON AND (now >= availableFrom if set) AND (now <= availableUntil if set). Toggle OFF always wins. Confirm the semantic with me if you see a better one and document it in the admin UI.
- Compute effective visibility at read time in the API serving the customer menu, not via cron jobs flipping flags (more reliable, no drift, cheaper). Add EventBridge Scheduler only for a concrete need (cache invalidation, notifications). If a CDN/cache sits in front of the menu, ensure TTLs or invalidation never show a dish after its end time.
- Enforce at order time too: a dish that expired between page load and checkout is rejected by the order API with a clear error. The UI is never the only gate.
- Times entered and shown in Europe/Stockholm, stored in UTC, DST-safe.
- Validation: end after start, warn on schedules already in the past.

SCOPE
1. Data model and migration (existing dishes unchanged: null schedule fields = no schedule).
2. API changes (admin create/update, customer menu read, order validation).
3. Admin UI: calendar with time picker for start and end (use the existing component library if present), ability to clear the schedule, status badge per dish: "Active", "Scheduled (starts ...)", "Expired (ended ...)", "Off". Update menu list/filter views without breaking the already-fixed filtering.
4. Tests: boundary times at start and end, DST transitions, toggle off overrides schedule, null start or end, order rejection after expiry.

---

## ISSUE 2: Printing orders to the kitchen printer

REQUIREMENT
Support for sending new orders to the restaurant's printer via Bluetooth or BLE. When a customer places an order it should print automatically on the printer in the kitchen.

BUSINESS NEED
The kitchen gets the ticket immediately without anyone watching a screen.

KEY TECHNICAL RISK (resolve first; no code until the approach is settled)
Bluetooth/BLE is a local-device protocol. The AWS backend cannot talk to a Bluetooth printer directly, so something on-site must receive the order and print. Evaluate and recommend between:
 A) Cloud-polled network printer (Star CloudPRNT or Epson Server Direct Print / ePOS): the printer polls an HTTPS endpoint on our backend for jobs. No on-site software, no open browser tab, fits serverless well, most reliable. Requires a compatible printer and network. Likely best practice: evaluate first and tell me honestly if I should push the customer to it.
 B) Web Bluetooth from the admin app on an on-site tablet/PC: the admin app receives new orders in real time and prints over BLE using ESC/POS. Verify caveats: Chromium-only (not iOS Safari), secure context, user gesture to pair, tab must stay open and awake, fragile reconnects.
 C) Small on-site bridge (e.g. Raspberry Pi or tiny app) that connects to the cloud and prints over BLE/USB/network.
Ask me (relay to me) which printer model(s) and on-site hardware the restaurant has or plans to buy. Include hardware cost and reliability in the comparison.

ARCHITECTURE REQUIREMENTS (whatever option is chosen)
- Durable print-job queue (e.g. DynamoDB with status PENDING, PRINTING, PRINTED, FAILED), created when an order is placed. Printing must never block or fail order creation.
- Idempotency: one order produces exactly one auto-print job; retries must not double-print.
- Delivery to the device matching the chosen option (printer polling, or real-time channel such as WebSocket/AppSync/SSE to the admin app with polling fallback).
- Retries with backoff, dead-letter path, visible failure state in the admin panel ("printer offline", "N unprinted orders").
- Manual reprint button per order in the admin panel.
- Ticket layout: order number, time, table/pickup/delivery info, items with quantities and modifiers/notes, allergy/special-request notes, customer name/phone where relevant, correct Swedish characters (å ä ö: verify code page/encoding on the printer), configurable paper width (58 mm / 80 mm).
- Admin settings: enable/disable auto-print, select printer, test-print button.
- Security: device authenticates with a per-device token/credentials. No public unauthenticated job endpoint.
- Alerting for stuck jobs (CloudWatch alarm).
- Tests: job creation idempotency, ticket formatting, status transitions.

DELIVERABLES
1. Comparison and recommendation before code, plus hardware questions.
2. Plan, then implementation of the chosen option with Terraform for all infra.

---

## ISSUE 3: Table booking time limits

REQUIREMENTS FROM THE CUSTOMER
- A regular table booking is at most 1.5 hours, with the possibility to extend the time.
- During buffet hours the booking is at most 1 hour and cannot be extended.
- Best if built dynamically so the restaurant can change: the default booking duration, whether a booking may be extended, and special times and rules during the buffet.

BUSINESS NEED
Maximize table turnover, especially during buffet, while keeping flexibility for regular dining. The restaurant changes all of it without a developer.

SCOPE
1. Configurable rules model (database, edited in the admin panel):
   - Global default: booking duration (default 90 min) and whether extension is allowed (default yes), plus a max extension length if sensible.
   - Special rule windows (e.g. "Buffet"): days of week, start time, end time, optional date range, max duration (e.g. 60 min), extension allowed yes/no. Support multiple windows.
   - Deterministic conflict resolution when windows overlap (priority field, most specific wins).
   - Admin validation: no invalid ranges, clear overlap warnings.
2. Booking logic (server-side source of truth):
   - On creating a booking, resolve the applicable rule from the start time and compute the end time. Persist the resolved duration and extension-allowed flag on the booking as a snapshot so later rule changes don't silently alter existing bookings.
   - A booking that starts in a normal window but runs into a buffet window: decide and document the behavior (rule by start time, or clamp to the window). Raise as a question for the customer.
   - Extension: only if allowed by the snapshot rule and only if the table has no conflicting next booking. Define how an extension is requested (guest self-service link, staff in admin, or both) and who can do it. Ask me, then implement the agreed option.
   - Table availability checks use the computed duration.
3. Frontend:
   - Customer booking UI clearly shows booking length and whether it can be extended, before confirming.
   - Admin UI to manage defaults and special windows, plus the extend action on existing bookings.
4. Existing data: migrate existing bookings sensibly (assume a default duration); nothing breaks for bookings made before this feature.
5. Tests: rule resolution (overlaps, boundaries at window start/end, DST days), extension allowed/denied, conflict with next booking.

Challenge edge cases the customer hasn't considered and list them as questions.

---

## ISSUE 4: Cookies and email marketing (GDPR-compliant)

REQUIREMENTS FROM THE CUSTOMER
The customer wants to collect email addresses from table bookings and payments to send advertising and offers.
- The recipient can unsubscribe through a clear link in every email.
- People who unsubscribe are automatically removed from future advertising sends.
- Check how consent should be handled under GDPR. Accepting cookies is most likely not the same as approving marketing by email.

BUSINESS NEED
The restaurant owns a marketing channel without legal exposure.

LEGAL DESIGN (implement, and flag for the customer's legal advisor to confirm)
- Cookie consent and email-marketing consent are separate. The cookie banner covers cookies/analytics/marketing trackers only (granular categories, reject as easy as accept, no pre-ticked boxes, consent stored and revocable, non-essential cookies not set before consent). Email marketing consent is a separate, explicit, unticked-by-default opt-in checkbox on the booking form and the payment/checkout flow, with clear wording on what they will receive and from whom, and a link to the privacy policy.
- Transactional emails (booking/order confirmations) are not marketing and must never include promotions unless consent exists. Keep the flows separate.
- Do not market to people who merely booked or paid without opt-in. Research the Swedish rules (Marknadsföringslagen) including the existing-customer exception, and recommend strict opt-in as default. Flag the exception as a question for the legal advisor rather than relying on it.
- Consider double opt-in (confirmation email) and recommend whether it is worth it.

SCOPE
1. Data model: marketing_contacts (email, status SUBSCRIBED/UNSUBSCRIBED/SUPPRESSED, source: booking/payment/other) and an append-only consent_log (email, timestamp, source, exact consent text/version shown, IP and user agent only if justified under data minimization, action opt-in/opt-out). Proof of consent must be reconstructable.
2. Capture: checkbox and consent persistence in booking and payment flows. Consent is recorded only if actively ticked.
3. Sending (Amazon SES): admin panel to compose and send a campaign to subscribed contacts only (rate-limited batch sending, e.g. SQS + Lambda), with a test send. Every marketing email contains the restaurant's identification and a clear unsubscribe link.
4. Unsubscribe: signed, non-guessable token link, one click, works without login, shows a confirmation page, immediately sets UNSUBSCRIBED and logs it. Include `List-Unsubscribe` and `List-Unsubscribe-Post` (RFC 8058) headers. Suppression check before every send so unsubscribed people are automatically excluded from all future campaigns.
5. Deliverability and hygiene: SES bounce/complaint handling via SNS, auto-suppress hard bounces and complainers, SPF/DKIM/DMARC in Terraform, separate configuration sets for marketing and transactional.
6. GDPR rights: export and delete a person's data on request, retention policy for contacts and consent logs, privacy-policy text suggestion.
7. Admin UI: contact list with status and filter, consent history per contact, campaign list with send stats, manual unsubscribe/suppress.
8. Tests: consent only when ticked, unsubscribed users excluded, token validation, bounce suppression.

Also output a short "questions for the customer's legal advisor" list.

---

## ISSUE 5: Catering (HIGHEST PRIORITY, own epic with milestones)

BUSINESS NEED
Let catering customers self-serve a price, then convert them into signed, legally binding orders with minimal back-and-forth, while the restaurant controls all pricing rules without a developer.

CUSTOMER-FACING FLOW (user-front-end)
A catering calculator ("cateringkalkyl") where the customer can:
1. Enter number of guests, minimum 15 (enforce server-side too).
2. Select dishes and menus.
3. See the calculated price immediately (live estimate).
4. Submit an inquiry to the restaurant.

RESTAURANT / CONTRACT PROCESS
1. Restaurant reviews the inquiry.
2. Restaurant approves and signs the quote with BankID.
3. The quote is sent to the customer with the agreed price and the selected dishes.
4. The customer also signs with BankID.
5. When both parties have signed, a binding agreement exists.

ADMIN PANEL (separate CRUD for catering menus and calculation)
The restaurant can:
- Create, view, edit and delete catering menus and dishes.
- Set discounts depending on guest count, e.g. X-Y people = Z% discount, more than Y people = N% discount (fully configurable tiers).
- Set preparation lead time depending on order size, e.g. X-Y people require Z days' notice, more than Y require N days (validate the requested date against this).
- Manage delivery options (e.g. pickup, delivery).
- Set delivery price based on distance from the restaurant to the customer's address, e.g. a fixed price up to X km, with further configurable bands.

DESIGN DECISIONS (research, recommend with reasoning, then wait for my approval before building)
A) BankID signing: compare two options for both-party signing and recommend one.
   Option 1: Direct BankID Relying Party integration via SEB (paying SEB for the BankID API/RP certificate).
   Option 2: Scrive e-signing on a pay-as-you-go model.
   My starting hypothesis, which you must verify or refute:
   - Direct BankID signs a text/data payload; it does not give a sealed contract document. We would have to build PDF generation, sealing, evidence package, long-term validation data storage (signatures, OCSP), signing order, reminders, expiry, audit trail, mTLS/RP certificate rotation and a bank agreement. A lot of legal-grade plumbing for a restaurant's low catering volume.
   - Scrive provides the document, signing order (restaurant first, then customer), BankID auth/signing for both, sealed PDF with evidence, webhooks and API, with no bank contract or certificate management. Pay-as-you-go suits low/uncertain volume.
   - Direct SEB is likely cheaper per signature only at high volume.
   - Default recommendation: Scrive behind a `SigningProvider` interface (create document, send to parties, webhook handling, fetch sealed PDF + evidence, status) so SEB direct can be swapped in later. One interface, one implementation; do not over-engineer.
   Check current pricing, API docs, sandbox/test environment and onboarding requirements for both. Present a short comparison table (cost model, build effort, legal evidence, lock-in, onboarding time) and a recommendation.
B) BankID limitations: requires a Swedish personnummer. Decide the fallback for customers who cannot use BankID (foreign customers, signing on behalf of a company). Capture company name / org number and signatory authority for business customers. Flag legal implications.
C) Estimate vs binding: the live calculator price is an estimate until the restaurant signs. State this clearly in the UI and in the quote. Define quote validity period and expiry behavior.
D) Pricing engine: single server-side source of truth (the frontend displays only what the API returns). Order: base price (dishes x guests), then headcount tier discount, then delivery fee, then VAT (verify correct Swedish VAT rates for food vs delivery; make configurable). Round consistently. Persist a price snapshot on each inquiry/quote so later menu or rule edits never change an issued quote.
E) Distance-based delivery pricing: needs geocoding and distance/route calculation (evaluate Amazon Location Service vs Google Maps for cost, accuracy, data residency), configurable restaurant origin address, configurable price bands per km. Decide road distance vs straight-line and document it.
F) Workflow state machine: SUBMITTED -> UNDER_REVIEW -> QUOTE_SIGNED_BY_RESTAURANT -> SENT_TO_CUSTOMER -> SIGNED_BY_CUSTOMER (BINDING) / DECLINED / EXPIRED / CANCELLED. Enforce transitions server-side. The restaurant can adjust price/dishes during review; any change after sending voids existing signatures and creates a new quote version.
G) Storage and compliance: sealed signed PDF and evidence in S3 (encrypted, versioned, restricted access, retention policy), audit logs, GDPR (data minimization, retention, deletion rules versus legal retention of contracts).
H) Notifications: email to the restaurant on a new inquiry, email to the customer with the signing link and the signed copy (SES). Cover signing-link expiry and reminders.
I) Security: authenticated admin actions, rate limiting and bot protection on the public inquiry endpoint, input validation, webhook signature verification, idempotent webhook handling.

MILESTONES
M1 catering data model + admin CRUD
M2 pricing engine + calculator UI
M3 inquiry + review workflow
M4 signing integration + contract storage
M5 notifications + hardening

DELIVERABLES
1. Short comparison and recommendation for A before any code.
2. Plan with milestones.
3. Terraform, backend, admin UI, customer UI and tests (pricing tiers, lead-time validation, distance bands, state transitions, webhook idempotency).
4. Open questions for the customer: who pays signing costs, quote validity, cancellation terms, allergens/dietary info on quotes, terms and conditions the customer must accept, minimum order value.
