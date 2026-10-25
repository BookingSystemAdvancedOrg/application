# Privacy audit: staff notifications, unit isolation, cookies, DPA (2026-10-22)

Scope: IT-hjälparna as processor for the restaurants' guest data and as controller for restaurant staff accounts.
Dev account only; Terraform validated, not planned or applied. Consent, retention, the guest notice and
`docs/privacy.md` belong to the GDPR session and are not covered here except as dependencies.

Severity: **High** = personal data reaches people or places it should not today. **Medium** = a real gap
that needs a change. **Low** = hardening or documentation.

## Findings

| # | Finding | Where | Severity | Status |
|---|---|---|---|---|
| N1 | The staff email for a new online booking, a guest cancellation or a failed fee carried the guest's phone, e-mail and free-text message. The message often holds allergies, which can be health data. These emails go to a shared inbox (`location.email` / `replyToEmail`) that may be forwarded. | `shared/messages.py:261` | High | **Fixed.** Name, date, time, guests and table only; a free-text message is flagged ("Gästen har lämnat ett meddelande") but not quoted. Contact details stay in admin. |
| N2 | The same email put the guest's address in `Reply-To`. | `functions/notification/app.py:359` | Medium | **Fixed.** `Reply-To` is empty. **Behaviour change:** staff can no longer reply to the guest straight from the staff email; they contact the guest from the booking in admin. |
| N3 | The owner catering emails quoted the guest's own words (`changeMessage`, `rejectReason`). | `shared/catering_messages.py:122` (lines 134-139) | Medium | **Fixed.** The email says there is a message and links to admin. |
| N4 | The owner catering SMS contains the guest's full name or company plus contact name. SMS previews show on locked phones. | `shared/catering_messages.py:159` | Low | **Open, owner to decide.** Proposed: drop `who` from the SMS ("Ny cateringförfrågan (fre 24 okt). Öppna: ..."). Left unchanged on purpose; if adopted, `tests/test_catering.py:668` changes with it. |
| N5 | No webhooks to staff exist. Operator newsletters (`booqy-console/api/src/app/mail.py`) go to owners only and carry no guest data. Order messages go to the guest only. Reminders go to the guest only. | - | - | No action. |
| U1 | Staff location isolation is enforced on every staff-reachable JWT route (table below), but the check in `tenant.for_jwt` is skipped when a function has no `USER_TABLE_NAME`, so a new function that forgets it would let staff reach every location of their restaurant. Today every staff-reachable function has it. | `shared/tenant.py:282` | Medium (latent) | **Open (proposed).** Fail closed (deny staff when the user table is not configured). About 40 existing tests run staff calls without `USER_TABLE_NAME` and would need the variable added, so not done under "minimal fixes". |
| U2 | Booqy Console operators can read every guest message in the dev mail outbox (`GET /platform/outbox`). The outbox exists in dev only and expires after 30 days. | `functions/notification/app.py:205`, `infrastructure/storage/s3/mail-outbox/main.tf` | Low | Accepted for dev. Never enable `mail_outbox` in prod. |
| U3 | BankID evidence stores the signer's full personnummer in S3 (Object Lock, 7 years). | `functions/catering-customer/app.py:145` | Medium | **Open (legal).** Needed as signing evidence? Masked number is already used in the API view. See legal review. |
| C1 | booqy.se/integritet said "sparar ingenting i din webbläsare", but `assets/booqy.js:39` stores `booqy-theme`. | `booqy-website/site/integritet.html` | Low | **Fixed in the diff** (`integritet-diff.md`); not applied. New page `booqy-website/site/cookies.html` lists it. |
| C2 | The customer site disclosed storage only inside the privacy page. | `customer-front-end` | Low | **Fixed.** New page `/cookies` (`src/features/juridik/CookiesPage.tsx`) from the same registry, linked in the footer. |
| C3 | The Stripe cookie entry said "only on the card guarantee step", but Stripe.js also loads for order and catering payments (`PaymentTicket`). | `customer-front-end/src/shared/storageRegistry.ts:104` | Low | **Fixed.** |
| C4 | Cookieless visit counting (`POST /t`) is labelled `nödvändig` in the customer registry. It stores and reads nothing on the device, so LEK 9:28 consent does not apply, but it is analytics, not strictly necessary. | `customer-front-end/src/shared/storageRegistry.ts:132` | Low | **Open.** Label it as statistics without storage; it still needs no banner. Legal basis (legitimate interest) is in the GDPR session's privacy notice. |
| C5 | Booqy Console keeps Cognito tokens in `localStorage` (Amplify default) and has no cookie page. Users are IT-hjälparna's own operators. | `booqy-console/web/src/auth.ts:17` | Low | **Open.** Add a short storage note to the console, or move to cookie storage. |
| C6 | No analytics, advertising or tracking scripts on any of the four sites. Third-party scripts: Stripe.js and Cloudflare Turnstile on the customer site only (CSP in `customer-front-end/index.html`). Fonts are self-hosted everywhere. | all | - | No consent banner needed. None added. |
| P1 | booqy.se/integritet says "Vi skickar aldrig reklam eller nyhetsbrev", which is no longer true (restaurant marketing with double opt-in). It also barely covers IT-hjälparna's role as controller for staff accounts, has no third-country transfer statement and does not name the legal entities. | `booqy-website/site/integritet.html` | Medium | **Diff proposed** in `integritet-diff.md`, not applied. |
| P2 | The company name differs: "IT-hjälparna" (booqy.se) and "IT Hjälparna Nordic AB" (customer privacy page). | both sites | Low | **Open (legal).** Placeholder `[ATT FYLLA I]` for org.nr in the diff. |
| D1 | No DPA existed between IT-hjälparna and the restaurants. | - | High | **Built.** Draft in `dpa-draft.md`; acceptance step in admin; records stored. Needs lawyer review before production use. |

### Unit isolation per staff-reachable route

API Gateway's JWT authorizer only proves a valid token; each Lambda enforces tenant and location (`shared/tenant.py`).

| Routes | Lambda | Check |
|---|---|---|
| `/locations/{id}/reservations*` (list, get, manual, status, patch, payment) | get-reservation, create-pending-reservation, mark-arrived | `for_jwt(location_id=...)`, user table set |
| `/locations/{id}/tables/{t}/block` | block-table | own profile check (`_authorize_location`) |
| `/locations/{id}/orders*` | manage-order | `for_jwt(location_id=...)` |
| `/locations/{id}/catering/requests*`, `/catering/invoices*` | catering-requests, catering-offer | `for_jwt(location_id=...)` |
| `/locations/{id}/menu*`, layout routes | get-menu, manage-menu, manage-layout-element, list-layout-version | `for_jwt(location_id=...)` |
| `GET /locations/{id}` | get-location | `for_jwt(location_id=...)`; `GET /locations` (all) is owner only |
| `GET /insights` | insights | staff without `locationId` get their assigned location only |
| `GET /tenant` | tenant-account | staff get only their assigned location in `locations` |
| `/users/*` | manage-user | staff: only `/users/me/guide`, own row |
| owner-only | catering-settings, discount-tiers, create-location, publish/activate layout, pre-signed-url, marketing, tenant-account writes, DPA accept | `owner_only=True` |

No route was found where staff can read another location's guest data. The one gap is U1 (fails open if misconfigured).

## Cookie and storage inventory

| Site | Name | Kind | Set by | Necessary? | Disclosed |
|---|---|---|---|---|---|
| booqy.se | `booqy-theme` | localStorage | booqy.se, only on toggle | user's own choice | `/cookies` (new) |
| Admin | refresh cookie | HttpOnly cookie | Booqy (login) | yes | `/cookies` (existing) |
| Admin | location, logout-pending, guide, motion, theme, sample data | localStorage | admin | yes / own choice | `/cookies` |
| Customer sites | location, theme, cart, table code, order and catering tokens, preview slug, offer card | local/sessionStorage | site | yes / own choice | `/cookies` (new) and `/integritetspolicy#cookies` |
| Customer sites | `__stripe_mid`, `__stripe_sid` | cookie | Stripe, payment steps only | yes (fraud prevention for a payment the guest asked for) | same |
| Customer sites | Cloudflare Turnstile | none recorded | Cloudflare | yes (bot protection) | same |
| Console | Cognito tokens (Amplify) | localStorage | console | yes | not disclosed (C5) |

## DPA acceptance

- Text: `docs/dpa-draft.md` (version `2026-10-v1`), shown in admin at `/avtal` (`admin-front-end/src/features/dpa/dpaText.ts`). `CURRENT_VERSION` in `shared/dpa.py` must match.
- Accept: `POST /tenant/dpa/accept {version}` (owner only, tenant-account). One transaction writes an append-only row `PK DPA#<tenantId>, SK <version>#<acceptedAt>` with `acceptedBy {sub, email, name}` and sets `dpa {version, acceptedAt, acceptedBySub}` on the tenant PROFILE. `GET /tenant` returns `dpa` status (optional field: old frontends ignore it, the new admin shows nothing on an old backend).
- Go-live gate: Booqy Console sets `dpaRequired: true` on tenants it creates from now on. For those, every public route (`tenant.for_public`, `shared/tenant.py:303`) answers 404 until any DPA version is accepted. Tenants without the flag (all existing ones) are never gated. A newer DPA version never takes a site offline; it only shows the reminder again.
- Admin: a banner in the shell (blocking text for new restaurants, a "Senare" reminder for live ones; staff only see the blocking case), a "Kom igång" step, and the `/avtal` page with checkbox and accept button.
- IAM: new inline policy `dpa-acceptance` on the tenant-account role (`infrastructure/security/iam/tenant-account/dpa-iam.tf`): `PutItem` only where the partition key starts with `DPA#`, `UpdateItem` only of `dpa`. New route `POST /tenant/dpa/accept` in `network/api-gateway/dpa-routes.tf`. No change to OIDC/deploy roles.

### Questions for tenant-purge (not changed here)

- `DPA#<tenantId>` rows are in their own partition, so `tenant-purge` (which deletes the `TENANT#<id>` partition) leaves them. That is probably right: proof of the DPA for the limitation period after offboarding. Decide how long to keep them and whether purge should delete them after that.
- The `dpa` summary on PROFILE goes with the PROFILE tombstone handling, as before.

## Dependencies on the GDPR session

- `tenant-site-config` still serves a gated new tenant's site config; menu, availability and booking all 404, so nothing can be booked. A clearer "not published yet" page would need a one-line `dpa.blocks_public` check in `functions/tenant-site-config/app.py`, which that session is editing.
- The withdrawal link line in `shared/messages.py` (the GDPR session adds it).
- C4 (visit counting label and legal basis) belongs with the guest privacy notice.

## Assumptions

- "Unit" means location: staff are assigned to one location, owners see all of their restaurant's locations.
- Guest names in staff emails are kept: staff need to know who is coming, and the shared inbox already is the restaurant's.
- `Reply-To` to the guest is removed deliberately (N2); restaurants contact guests from admin.
- "Going live" means the public site works for guests. Tenants become `active` in onboarding before the owner logs in, so the gate is on public routes, not on the tenant status.
- Existing tenants are identified by not having `dpaRequired`; only console code from this change sets it.
- The SMS provider is AWS (Amazon SNS); there is no separate SMS vendor.
- Cloudflare Turnstile sets no cookies on the customer domain (as stated in the existing registry); not re-verified in a browser here.

## Legal review needed

- The whole DPA draft (`dpa-draft.md`), including the open questions at its end: legal entity and org.nr, Stripe Connect role, BankID role, incident deadline (48 h), deletion deadline (90 days), audit rights, liability cap, whether a click by the logged-in owner is a valid signature and the owner's authority.
- Storing the full personnummer as BankID evidence (U3), with regard to 3 kap. 10 § dataskyddslagen.
- Free-text health data (allergies) in bookings: information and safeguards.
- The integritet diff (`integritet-diff.md`), especially the controller section for staff and customers and the transfer statement.
- How long DPA acceptance records are kept after offboarding.
- Whether the cookieless visit counting needs anything beyond the privacy notice (C4).
