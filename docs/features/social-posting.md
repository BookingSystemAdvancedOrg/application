# Social posting (feature G, `socialPosting`)

ADR: `infrastructure/docs/adr/ADR-G-social-posting.md`. Contract: CONTRACTS.md 6.G, 4, 5, 11, 13.

## Pieces

| Piece | Where | What |
|---|---|---|
| Lambda `social-posting` | `functions/social-posting/app.py` | routes G1-G8; publish worker `{"socialPostJob": ...}` (one-time schedule or async self-invoke for "Publicera nu"); bus rule `MetaConnection.Disconnected`; hook `tenant.purge` |
| Logic | `shared/features/social_posting/__init__.py` | keys, validation, template caption, channel/overall state |
| OpenAPI | `docs/openapi/social-posting.yaml` | the 8 JWT routes |

## Rules that matter

- **No token of its own**: reads feature A's `CONNECTION` row and secret
  `booqy/<env>/meta-ads/<tenantId>` (GetSecretValue only); the secret id is
  built from the post's own `tenantId`. Meta calls go through A's port.
- **Per channel state** (`queued -> publishing (5-minute lease) -> published | needs_check | failed`).
  Instagram stores `containerId` before `media_publish`, so a retry publishes
  the same container. Facebook has no idempotency key: a retryable error
  (unknown outcome) goes to `needs_check`, and the next run reads the Page's
  recent posts for the same caption before posting again. Never blind-retry.
- **Retry** = "Publicera nu" / a new schedule on a `failed` or
  `partly_published` post; only failed channels are re-queued.
- **Alcohol**: no automatic draft for `alcohol: true` dishes; scheduling such
  a post needs `alcoholConfirmed` (400 `alcohol_confirmation_required`).
- Entitlement re-checked by the worker (channel failed `feature_not_in_plan`);
  Meta disconnected at publish -> `meta_not_connected`; the disconnect event
  moves scheduled posts back to draft and deletes their schedules.
- Images go to Meta as a 15-minute presigned GET URL of the menu photo.

## Not built / unverified

- "Föreslå text" (Bedrock caption suggestion against A's COPYUSE quota,
  ADR-G 7) has no route in CONTRACTS.md 6.G, so it is not built; drafts use
  the Swedish template only. Needs a contract row (Fable) before it can be added.
- Real Meta publishing (App Review for `pages_manage_posts` and
  `instagram_content_publish`; Instagram's per-account daily API post limit
  to verify). Simulator only.
