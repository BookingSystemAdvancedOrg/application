# Site builder B1 (feature B, `siteBuilder`)

ADR: `infrastructure/docs/adr/ADR-B-site-builder.md`. Contract: CONTRACTS.md 6.B, 4, 5, 7, 8.4.
Run 1 builds B1 (schema, editor, preview, publish, rollback). AI edits and
the custom-code tier (B9-B13) are B2, Run 2.

## Pieces

| Piece | Where | What |
|---|---|---|
| API + workflow Lambda `site-builder` | `functions/site-builder/app.py` | B1-B8; Step Functions tasks Validate, Promote, MarkFailed; `tenant.purge`; `platform` answers 404 until B2 |
| Render Lambda `site-builder-render` | customer-front-end `render-lambda/` | Node 22: site-kit components with `react-dom/server` -> `sites/<t>/v/<n>/...` |
| Logic | `shared/features/site_builder/__init__.py` | validation, contrast (port of site-kit `accentPair`), keys |
| Schema | `shared/site_schema/v1.json` | the single source: Python validates with it (jsonschema), `scripts/gen_site_schema.py` writes site-kit's TS types |
| Contrast vectors | `shared/site_schema/contrast_vectors.json` | Python answers; site-kit's test checks its `accentPair` gives the same |

## Publish workflow (`<prefix>site-builder-publish`)

`Validate` (re-validates the snapshot stored on `PUBLISH#<id>`, writes the
immutable `VERSION#<n>`; a rollback only checks the version exists) ->
`Render` (skipped for rollback) -> `Promote` (CopyObject each page to the
root with `Cache-Control: no-cache`, delete root pages no longer in the
version, conditional `PUBLISHED` swap, `PROFILE.site = {mode, version}`,
`SiteVersion.Published`) -> done. Any failure -> `MarkFailed`
(`SitePublish.Failed`, lock released). One publish at a time per tenant
(`SITE.running`). Every task is safe to rerun.

## Rules that matter

- Owner only; staff 403 `owner_only`; feature off 403 (public preview 404).
- Draft writes are conditional on `version` (0 = first save); `publishId`
  is the execution name; rollback to the live version is a 200 no-op.
- Links only to own pages (`page:<pageId>`) or `https:`; images only under
  `menu-images/<tenantId>/`; block `locationId` must be the tenant's.
- Preview tokens: 32 random bytes, stored as SHA-256, 7 days, carried in the
  URL fragment and posted to B8 (rate-limited per IP), answer `noindex`.
- `VERSION#<n>` content (doc, pages, prefix, contrast) is written once;
  `publishedAt`/`publishedBy` are stamped once, conditionally, when it
  first goes live (B6 lists only versions that went live).

## Contract and ADR notes (for the orchestrator)

- ADR-B test plan "contrast 3:1 -> 400": with the ADR's own rule (theme
  colours are nudged like `accentPair`), every 6-digit colour reaches 4.5:1,
  so `contrast_too_low` is reachable only if the nudge cannot fix a pair.
  The check is implemented and unit-tested with a forced pair.
- B4 with `source: custom` answers 409 `wrong_mode` until B2 (no tenant can
  be in custom mode before the operator routes exist).
- `jsonschema` is in `functions/site-builder/requirements.txt`; the test
  suite gets it through `openapi-spec-validator` (requirements-dev.txt is
  shared; listing it there explicitly is an orchestrator change).

## Custom-code tier snippet (B2, owner's `.github`)

Not used in Run 1. In Run 2 the template repo workflow deploys PR builds to
`sites/<t>/preview/<sha>/` and `main` builds to `sites/<t>/releases/<sha>/`
and no longer writes the root; the snippet is added here with B2.
