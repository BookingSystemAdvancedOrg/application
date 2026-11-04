# Waitlist (feature F, `waitlist`)

ADR: `infrastructure/docs/adr/ADR-F-waitlist.md`. Contract: CONTRACTS.md 6.F, 4, 5, 7, 9.4, 10.

## Pieces

| Piece | Where | What |
|---|---|---|
| API Lambda `waitlist` | `functions/waitlist/app.py` | routes F1-F10; the one-time expiry timer `{"waitlistJob": ...}`; hooks `privacy.export`, `privacy.erase`, `tenant.purge` |
| Logic | `shared/features/waitlist/__init__.py` | keys, settings, entry views, positions, the HMAC queue token |
| OpenAPI | `docs/openapi/waitlist.yaml` | 6 JWT + 4 public routes |

## Rules that matter

- **An offer is a table hold.** "Erbjud bord" writes a `held` reservation
  with `hold_items` (`holdExpiresAt = now + holdMinutes`), the lock version
  bump and the entry update in one transaction, so the table is blocked by
  the existing occupancy rows; once the hold time passes the existing
  takeover rule (`holdExpiresAt <= :now`) lets anyone book it.
- **Conversion is one transaction**: `keep_hold_items` + reservation `held`
  -> `reserved` (guest accepts) or `arrived` (staff seat) + the entry
  update, conditional on `status` and `version`. A lost race is 409
  `hold_expired`; a retry after success returns the same `reservationId`.
- **Same day only** (location time zone). Staff are held to their own
  location; settings are owner only. `publicJoin` (with `enabled`) is
  mirrored onto the location row as `waitlist = {publicJoin}` for
  `/site-config`.
- **SMS** (`waitlist.joined`, `waitlist.offered`, `waitlist.expired`) go
  through `notification:live` (InvocationType Event). `notice{kind, at}` is
  written first, `notice.sentAt` after a successful invoke; a failed invoke
  leaves no `sentAt` ("Sms ej skickat" in admin).
- **Expiry**: schedule `waitlist-offer-<entryId>` (ActionAfterCompletion
  DELETE) releases the hold and marks the entry `expired` (idempotent);
  every read already treats a passed `offer.expiresAt` as expired.
- No name or phone in events; the guest token is never stored.

## Unverified

- SMS delivery itself (notification is mocked in unit tests).
- Card guarantee for waitlist conversions: none in v1 (ADR-F open question 3, owner to confirm).
