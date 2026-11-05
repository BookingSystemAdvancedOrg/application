# Personalliggare (feature H, `personalliggare`)

ADR: `infrastructure/docs/adr/ADR-H-personalliggare.md`. Contract: CONTRACTS.md 6.H, 4, 5, 9.2, 9.3.

**Needs legal review.** The feature ships off in every plan and must never be
described as compliant, approved or certified (`tests/test_personalliggare_copy.py`
checks the admin, console, site-kit and booqy-website sources when they sit
beside this repo). The admin page title is "Personalliggare (under granskning)".

## Pieces

| Piece | Where | What |
|---|---|---|
| API Lambda `personalliggare` | `functions/personalliggare/app.py` | routes H1-H8; hooks `platform` (H9 `personalliggare.export`, forwarded by Booqy Console) and `tenant.purge` |
| Logic | `shared/features/personalliggare/__init__.py` | keys, personnummer check, the append transaction, chain verification, corrected view, CSV |
| OpenAPI | `docs/openapi/personalliggare.yaml` | 8 JWT routes (H9 is documented in Booqy Console) |

## Rules that matter

- **Append only.** Check-in, check-out, correction and inspection view are each
  one new ledger record written in one transaction: REQ marker
  (`attribute_not_exists`) + HEAD (`seq = :prev`, or a first Put) + ledger Put
  (`attribute_not_exists(SK)`) + the presence pointer (Put on check-in, Delete
  on check-out). A lost race on the head retries up to 3 times, then 409
  `changed_retry`. The code only Puts, Gets and Queries the ledger table
  (tested); the table's resource policy denies Update/Delete/BatchWrite.
- **Hash chain.** `hash = shared.kassa.entry_hash(record)` (sha256 of the sorted
  JSON without `hash`, imported read-only), `prevHash` = the previous record's
  hash. The export verifies the whole chain of the location from seq 1 and
  against the HEAD: a changed record, a broken link, a gap or a cut tail sets
  `chainOk: false` and `firstBadSeq`.
- **Server time.** `recordedAt` is always the server clock; check-in/out take no
  time from the client. A late or offline registration is a correction
  without `correctsSeq` (it moves the presence pointer like the registration it
  stands in for); a wrong time is a correction with `correctsSeq`. Reason 5-300
  characters; `eventTime` not in the future and not before 1 January of the
  second calendar year back.
- **Corrected view.** H1 and H5 show each check-in/out with its latest
  correction applied and the original beside it (`original`), and late
  registrations as their own rows (`late: true`). Records are read for the day
  and its two neighbours, so a correction that moves a time across midnight is
  shown on the right day.
- **Who.** Staff check themselves in through their login's person
  (`TENANT#<t>/USER#<sub>` pointer; 409 `no_person_for_user` without one) and
  are held to their own location; the owner records for anyone (403
  `owner_only` for staff naming another person). A person with no
  `locationIds` works at every location of the tenant.
- **Identity numbers** (personnummer/samordningsnummer, Luhn and date checked,
  stored as `YYYYMMDD-XXXX`) appear only in H5, H6, H7/H8 and H9 bodies; never
  in H1, logs, events or error bodies. Events carry ids and times only.
- **Entitlement off** refuses every route with 403 except H6 for the owner while
  the location has records (the duty to keep the ledger outlives the
  subscription).
- **Purge** deletes the state table rows only; the ledger is never purged and
  expires by `ttl` (end of the 2nd calendar year after the record, legal to
  confirm). H9 works from the ledger rows alone, so it works for offboarded
  tenants.
- `Personalliggare.Corrected` carries `correctsSeq: 0` for a late registration
  (the contract field is a required int).

## Unverified

- That the `Deny` resource policy blocks Update/Delete for the Lambda role and
  does **not** block TTL deletes (moto enforces neither; needs a real AWS check).
- The legal points of ADR-H (scope, required fields, retention, offline
  behaviour, paper fallback wording, own-phone check-in): **needs legal review**.
- No official Skatteverket export format is assumed.
