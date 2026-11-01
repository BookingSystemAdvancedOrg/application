# OpenAPI fragments (one per feature)

Each feature documents its own JWT and public routes in
`docs/openapi/<feature-id>.yaml` (feature ids: CONTRACTS.md section 0).
`docs/openapi.yaml` stays the base document; existing-route changes are
made there.

A fragment holds only two top-level keys:

```yaml
paths:
  /tenant/gift-cards/settings:
    get: { ... }          # same conventions as docs/openapi.yaml
components:
  schemas:
    GiftCardsSettings: { ... }   # prefixed with the feature in PascalCase
```

`tests/test_openapi.py` merges the base document with every fragment and
fails on:

- a key other than `paths` and `components.schemas`;
- a path or schema name that is already in the base or another fragment;
- a merged document that is not valid OpenAPI 3.0.3;
- a fragment whose operations differ from the module-level `ROUTES` dict
  in `functions/<feature-id>/app.py` (keys `"METHOD /path"`, path
  templates as in API Gateway, `{proxy+}` written `{proxy}` here).

Operator routes (`/platform/...`) are documented in Booqy Console, not here.
