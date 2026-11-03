# Meta ads (feature A, `metaAds`)

ADR: `infrastructure/docs/adr/ADR-A-meta-ads.md`. Contract: CONTRACTS.md 6.A, 4, 5, 9.3, 11.

## Pieces

| Piece | Where | What |
|---|---|---|
| API Lambda `meta-ads` | `functions/meta-ads/app.py` | routes A1-A13; `platform` hook for operator routes A14-A17 (forwarded by Booqy Console); `tenant.purge` |
| Jobs Lambda `meta-ads-jobs` | `functions/meta-ads-jobs/app.py` | Step Functions tasks of `<prefix>meta-ads-publish`, nightly insights, kill-switch sweep |
| Logic | `shared/features/meta_ads/__init__.py` | keys, caps, kill switch, token secret, UTM links, pause helpers |
| OpenAPI | `docs/openapi/meta-ads.yaml` | A1-A13 |

## Rules that matter

- **Simulator by default**: every Meta call goes through `shared/meta_graph`
  (`META_CLIENT`, simulator refused in prod). `shared/meta_graph_real.py`
  is still the skeleton: the real Marketing API client is not built in Run 1
  and needs Meta App Review first.
- **Always PAUSED**: the workflow creates campaign, ad set, creative and ad
  paused; "Starta annonsen" (A11) activates them.
- **Spend bounded three ways**: lifetime budget + end date on every ad set;
  campaign budget 100-20 000 SEK and the owner's monthly cap (under the
  platform ceiling) checked on create and activate against month spend plus
  the unspent budget of active campaigns; the kill switch (platform row
  `PLATFORM#META_ADS/SETTINGS` or the operator's per-tenant `paused`) refuses
  create/activate with 409 and the sweep job pauses every active campaign.
  Nightly insights mail the owner once at 80 % and pause everything at 100 %.
- **Idempotent workflow**: each task skips a step whose Meta id is stored and
  looks an object up by name (`booqy-<campaignId>-<step>`) before creating
  it. CheckGuards only passes a campaign that is still `publishing`.
- **No guest data to Meta**, no Pixel, no Conversions API. Attribution is the
  existing cookieless counters via `utm_source=facebook`,
  `utm_medium=paid_social`, `utm_campaign=booqy-<first 10 of campaignId>`.
- **Token**: Secrets Manager `booqy/<env>/meta-ads/<tenantId>`, created at
  callback, deleted on disconnect and purge, never logged or returned.
- **Alcohol**: menu items with `alcohol: true` are refused (alkohollagen).
- **Location position**: the ad radius is drawn around the location's stored
  geocode (`cateringOrigin.position`, saved by the catering settings). A
  location without one answers 409 `assets_missing`.

## Environment

`META_ADS_TABLE_NAME`, `TENANT_TABLE_NAME`, `LOCATION_TABLE_NAME`,
`LOCATION_ID_INDEX_NAME`, `MENU_TABLE_NAME`, `MARKETING_TABLE_NAME`,
`MENU_IMAGES_BUCKET_NAME` (jobs), `USER_TABLE_NAME` +
`USER_TENANT_INDEX_NAME` (jobs: owner email for the 80 % mail),
`PUBLISH_STATE_MACHINE_ARN`, `META_REDIRECT_URI`, `SITE_BASE_DOMAIN`,
`JOBS_FUNCTION_NAME`, `NOTIFICATION_FUNCTION_NAME`, `EVENT_BUS_NAME`,
`META_CLIENT`, `BEDROCK_CLIENT`, `BEDROCK_MODEL_ID`.

## Contract notes (for the orchestrator)

- Notification kind `meta-ads.cap-warning` (email to the owner at 80 %) is
  used; CONTRACTS.md 9.4 does not list it yet (notification accepts any
  `<feature-id>.<message>` kind).
- 409 `assets_missing` also covers "location has no stored geocode".
- A9 never returns a cursor (a location has few campaigns); any cursor is 400.

## Human actions

Meta Business Verification, App Review for the scopes in ADR-A, a Facebook
Login for Business configuration, the `${prefix}meta-ads/app` secret value,
Bedrock model access in eu-north-1 (or an EU inference profile).
