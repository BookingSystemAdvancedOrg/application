# Guest CRM (feature C, `guestCrm`)

ADR: `infrastructure/docs/adr/ADR-C-guest-crm.md`. Contract: CONTRACTS.md 6.C, 4, 5, 9.2.

## Pieces

| Piece | Where | What |
|---|---|---|
| API Lambda `guest-crm` | `functions/guest-crm/app.py` | routes C1-C11; hooks `privacy.export`, `privacy.erase`, `tenant.purge`, `marketing.segment`; the rebuild job (`{"guestCrmJob": ...}`, re-invokes itself) |
| Stream consumer `guest-crm-sync` | `functions/guest-crm-sync/app.py` | reservation, order and catering-requests streams -> profiles; emits `GuestVisit.Recorded` |
| Logic | `shared/features/guest_crm/__init__.py` | keys, identity resolution and merge, ingest, segments, exports, erase, purge |
| OpenAPI | `docs/openapi/guest-crm.yaml` | the 11 JWT routes |

## Rules that matter

- **No plaintext contact** in the table: identities are consent subjects
  (`consent.subject`), the profile keeps `contactRef` (logical table +
  key of the newest source row) and C3 reads email/phone live. Once
  retention or erasure anonymises the source row, the CRM shows no contact.
- **Counted once**: a source row writes one `HIST#` row
  (`attribute_not_exists`); counters move only when that write is new, so
  stream replays, arrived -> undo -> arrived and rebuild + stream overlap
  never double-count. A no-show is its own history row (`<id>.noshow`).
- **Merge**: a contact pair that points at two profiles merges into the
  older (ULID order); every step is safe to repeat.
- **Staff**: own location's guests only, history of their location only,
  no amounts; segments, exports, PATCH and rebuild are owner only (403
  `owner_only`).
- **Exports** hold only subscriber ids of guests that are `subscribed` in
  marketing AND have a current email consent grant, never a guest with
  `segmentsExcluded`. Marketing-send still calls `may_send` per recipient.
- **Erase** (`privacy.erase`) deletes IDENT, PROFILE, HIST rows and the
  guest's subscriber id inside export chunks; answers counts only;
  publishes `GuestProfile.Erased`.
- **Retention**: profile `ttl` = last activity + 24 months; history rows
  395 days (bookings) or `DATA_RETENTION_DAYS` (orders, catering; default 7
  years); exports 90 days.
- **Entitlement**: every JWT route needs `guestCrm` (403
  `feature_not_in_plan`). The sync writes only for tenants with it on and
  emits `GuestVisit.Recorded` for tenants with `guestCrm` or
  `googleBusiness` on. Hooks answer whenever called (the caller gates on
  "on or ever on").

## Environment

`GUEST_CRM_TABLE_NAME`, `TENANT_TABLE_NAME`, `LOCATION_TABLE_NAME`,
`LOCATION_ID_INDEX_NAME`, `USER_TABLE_NAME`, `CONSENT_TABLE_NAME`,
`MARKETING_TABLE_NAME`, `RESERVATION_TABLE_NAME`, `ORDER_TABLE_NAME`,
`CATERING_TABLE_NAME`, `EVENT_BUS_NAME`, `DATA_RETENTION_DAYS` (optional).

## Open (human)

- Legitimate-interest assessment template for restaurants (legal).
