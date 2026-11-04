# Google Business Profile (feature D, `googleBusiness`)

ADR: `infrastructure/docs/adr/ADR-D-google-business-profile.md`. Contract: CONTRACTS.md 6.D, 4, 5, 9.2, 11.

## Pieces

| Piece | Where | What |
|---|---|---|
| API Lambda `google-business` | `functions/google-business/app.py` | routes D1-D10; hooks `platform` (D11 `google-business.tenant.get`), `tenant.purge`, `privacy.export`, `privacy.erase` |
| Jobs Lambda `google-business-jobs` | `functions/google-business-jobs/app.py` | hourly `{"job": "sync"}`; bus rule `GuestVisit.Recorded` -> REVJOB + one-time schedule; `{"reviewJob": ...}` sender |
| Logic | `shared/features/google_business/__init__.py` | keys, hours mapping + hash, token secret, schedules, stats |
| Port | `shared/google_business.py` (Run 0), real client `shared/google_business_real.py` (skeleton) | simulator unless `GOOGLE_BUSINESS_CLIENT=real`; simulator refused in prod |
| OpenAPI | `docs/openapi/google-business.yaml` | the 10 JWT routes |

## Rules that matter

- **One way, Booqy is the source.** `businessHours` -> `regularHours`;
  special day `closed` -> closed, `hours` -> its periods, `no_online` and
  notes are never sent; only today .. +12 months. Google is called only when
  the sha256 of the mapped payload differs from `lastSyncHash`. Edits made
  in Google are overwritten (the admin card says so).
- **Owner only, every route** (staff 403 `owner_only`); foreign locations 404.
- **Review request = direct marketing** (ADR-D 5): sent only with a current
  consent grant for that channel (`consent.may_send`), with the one-click
  withdraw link (List-Unsubscribe), at most once per guest per
  `minDaysBetween` (conditional write on `REVSENT#<guestKey>`), never to an
  anonymised/erased source row or a no-show, same text for everyone (no
  rating question, no incentive).
- **Exactly once**: schedule names are deterministic
  (`gbp-review-<kind>-<sourceId>`, ConflictException = success); the sender
  claims `REVJOB` with `status = scheduled` before sending.
- The refresh token lives only in `booqy/<env>/google-business/<tenantId>`;
  never logged, returned or evented. No guest data goes to Google.

## Not done / unverified

- **Click counting** (`stats.clicks30d`) is always 0: the review link is
  sent directly because the signed marketing redirect (`/m/c/{token}` in
  `marketing-public`, a frozen hotspot) only knows campaign links. Needs a
  hotspot change (a `k: "r"` token kind) in a later batch.
- The real Google client (`shared/google_business_real.py`) is still the
  Run 0 skeleton; everything runs against the simulator. Google Business
  Profile API access and OAuth verification of `business.manage` are human
  actions.
- Consent rule and soft opt-in: lawyer review (ADR-D legal notes).
