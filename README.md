# application

Python Lambda backend for the reservation platform. Each function in
`functions/` is built and deployed as its own container image, on its own
ECR repository, defined in the separate `infrastructure` repo.

## What to read first

**`docs/LAMBDA_REFERENCE.md`** is the single source of truth for what every
function should do, which AWS resources it can access, what environment
variables it has, and how it's invoked (API Gateway route, DynamoDB Stream,
or EventBridge Scheduler). You should be able to implement any
function from that document alone, without needing to read the Terraform in
the `infrastructure` repo.

It's copied here (not just linked) so this repo is self-contained. It's
maintained in `infrastructure`'s repo root - if it changes there, re-copy it
here rather than editing this copy directly.

## Layout

```
shared/                        Common helper package, imported by every function's app.py
  auth.py                      JWT claims / Cognito group helpers for JWT-protected routes
  responses.py                 API Gateway v2 proxy response builders
  dynamo.py                    Thin boto3 DynamoDB resource helper

functions/<name>/
  app.py                       Lambda handler - its module docstring summarizes the
                                trigger, purpose, environment variables, and resources
  requirements.txt             Third-party deps beyond boto3 (which the base image
                                already includes). Pre-filled with `stripe` for the
                                3 functions that need it.
  Dockerfile                   Builds this function's container image

docs/LAMBDA_REFERENCE.md       Full per-function reference (see above)
docs/openapi.yaml              OpenAPI 3.0 contract for implemented HTTP endpoints
```

Implementation is incremental. Completed handlers have unit tests under `tests/`;
functions scheduled for later tasks may still return `501 not implemented` or raise
`NotImplementedError`. The OpenAPI document includes only implemented HTTP routes.

## Building a function's image

Build context is the **repo root**, not the function's own directory - each
Dockerfile `COPY`s both `shared/` and its own `functions/<name>/`:

```bash
docker build -f functions/get-location/Dockerfile -t get-location:latest .
```

## Local dev

```bash
python3 -m venv .venv
source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -r functions/<name>/requirements.txt boto3
```

For the complete test toolchain on Windows PowerShell, activate the virtual
environment and install the repository's development requirements:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

`shared/` is a plain Python package (`shared/__init__.py` exists) - as long
as you run things from the repo root, `from shared.auth import get_claims`
etc. resolves the same way it does inside the container.

## OpenAPI and Swagger UI

Validate the OpenAPI 3.0 document and its implemented-route guard locally:

```powershell
python -m openapi_spec_validator docs\openapi.yaml
python -m pytest -q tests\test_openapi.py
```

Run Swagger UI from the repo root, then open `http://localhost:8081`:

```powershell
docker compose up -d swagger-ui
docker compose logs swagger-ui
```

In Swagger's **Servers** selector, replace the `apiId` value (`replace-me`) with
the deployed dev HTTP API ID and change `region` if needed. For a protected
operation, select **Authorize** and paste the Cognito access token itself (do
not include the `Bearer ` prefix). Swagger UI displays and calls the API; it
does not start or proxy the Lambda backend.

The OpenAPI file is the client-facing documentation and testing contract. It
does not provision API Gateway: Lambda integrations, routes, CORS, and the JWT
authorizer remain owned by the separate `infrastructure` repository.

### Public customer reads

`GET /locations/{locationId}/public-info` and
`GET /locations/{locationId}/layout/active` are public (`NONE` auth) customer
routes. The first returns only `locationId`, `name`, `address`, `timezone`,
`businessHours`, and—when present as a valid pair—`email` and `phoneNumber`.
It never exposes booking policy, audit fields, DynamoDB keys, or unexpected
stored attributes.

The active-layout route returns `{"floors": [...], "elements": [...]}`. Each
floor contains only `floorId`, `name`, and `level`; renderable non-floor
elements contain their validated geometry, optional `floorId`, and applicable
table, cash-register, door, or window fields. A table or `cashRegister` may
include its optional persisted `label`. A `floorArea` is a renderable,
non-bookable element distinct from the `floor` storey record; it has the common
geometry and optional `floorId`, with no additional type-specific fields. A
`cashRegister` has the same common geometry and optional `floorId`. A door may
include the persisted `kind` value `entrance` or `kitchen`; its absence means
the door is legacy/unspecified.
It never exposes snapshot versions, lifecycle timestamps, audit data, DynamoDB
keys, or the activation-state item. Both exact routes must remain
unauthenticated in API Gateway.

The menu route family deliberately splits reads from writes. Both
`GET /locations/{locationId}/menu` and protected GET requests below
`/locations/{locationId}/menu/{proxy+}` integrate with `get-menu`.
`POST`, `PUT`, and `DELETE` requests below the greedy route integrate
with `manage-menu`. The bare GET has no authorizer; the greedy GET route
uses the JWT authorizer and must retain the path-parameter name `proxy`.

## Multi-floor layout workflow

The layout editor stores floors and their contents as elements in one mutable
draft per location. When the user adds a floor, create an element with
`type: "floor"`, retain the returned `elementId`, and send that value as
`floorId` on each `floorArea`, wall, door, window, table, or `cashRegister`
placed on that floor. A floor area uses exactly `type: "floorArea"` (including
casing). It is the renderable area within a storey, not the `floor` storey
record itself, and is never bookable. It uses the common canonical 3D geometry
fields (`x`, `y`, `z`, `width`, `height`, `depth`, and `rotationY`) plus an
optional `floorId`, with no extra variant fields. Width, height, and depth must
be positive. A cash register similarly uses exactly `type: "cashRegister"`,
the common geometry fields, and an optional `floorId`. To render one canvas,
list the location's layout elements and filter non-floor elements by the
selected floor's `elementId`.

For a `floorArea` created from the 2D editor, the frontend adapter uses 50
canvas units per metre. It sends `canvas x / 50` as backend `x`, `canvas y / 50`
as backend `z`, `canvas w / 50` as `width`, and `canvas h / 50` as `depth`; it
sends backend `y = 0` and `rotationY = 0`. The frontend also owns and sends an
explicit positive `height`. The backend does not infer one, and this contract
does not prescribe a numeric height default.

A table or cash register may also carry a staff-assigned `label`, for example
`"Patio 4"` or `"Front till"`. The frontend sends that field in the element's
create/update payload and renders the returned value instead of deriving a
position-dependent label such as `T${index + 1}`. Surrounding whitespace is
trimmed; the remaining value must be nonblank and at most 128 characters.
Labels are not generated and need not be unique, including across floors. The
element's `elementId` remains its stable identity. Existing tables and cash
registers without `label` remain valid and may use a frontend fallback until
staff assigns one. Cash-register label metadata does not turn the fixture into
a bookable table.

Publishing and activation are location-wide: one version contains every floor
and all of their elements, and the whole version is activated together.
Publishing alone leaves the snapshot inactive. The first activation is
immediate. A replacement cannot take effect before the target snapshot's
`createdAt` plus five minutes in `dev` or 28 days in `prod`; a default request
activates immediately when eligible, otherwise at the earliest safe whole UTC
minute. Owners and super-users can move the same pending target with
`PUT /locations/{locationId}/layout/pending-activation` and the required body
`{"effectiveFrom":"<future whole-minute ISO 8601 timestamp>"}`. Normal success
is `202`. `DELETE` on the same route cancels a still-future transition and is
idempotent with an empty `204`; due/overdue or unsafe state returns `409`. To
choose another version, cancel first and then call the existing version
activation POST. Legacy flat drafts with no floor elements and no `floorId`
values remain valid. Draft editing is intentionally non-cascading, so deleting
a floor does not delete its children; move or delete those children before
publishing again. A draft `floorArea` may omit `floorId` while it is being
edited, but once a draft contains floor storeys, publication requires it (and
every other non-floor element) to reference a valid floor in that same draft.

Availability returns only slot starts satisfying
`now < slotStartUtc <= now + 21 days`. Every slot includes the published
`layoutVersion` effective for its whole interval, while each available table
still contains only `tableId` and `seats`; adding a display label does not
change the table object. At a scheduled cutover, slots ending at or before it
use the current version, slots starting at or after it use the pending version,
and crossing slots are omitted. Block creation follows the same boundary but
rejects a crossing slot with `409`. It stores the selected version only as
internal occupancy provenance and does not add `layoutVersion` to the block
request or response. Unblocking does not resolve layout state. Cash registers
and floor areas are renderable elements, not bookable tables: availability
ignores them and the table-block endpoint cannot target their IDs. An optional
cash-register label does not change that rule. The block route continues to
receive the table's `elementId` as `tableId`, never its label.

Pending-layout management does not currently inspect reservations or slot
occupancy. Once the pending layout is visible inside the 21-day booking
horizon, moving or cancelling it can invalidate a booking's stored layout
version/table assumptions. Do not expose late changes in production until the
team chooses a policy such as forbidding them inside the horizon or migrating
affected reservations explicitly.

The public active-layout read separates published floor records into `floors`
and returns floor areas, walls, doors, windows, tables, and cash registers in
`elements`. A client selects a floor by `floorId` and filters `elements` by that
value. A `floorArea` and its canonical geometry are preserved through
publication and returned to both staff version reads and this public response.
Door `kind`, when present, follows the same lifecycle so clients can
distinguish an `entrance` from a `kitchen` door. Existing doors without `kind`
remain valid and should be rendered as an unspecified door. A table's or cash
register's optional `label` is likewise returned unchanged after its initial
trimming. Legacy flat layouts return an empty `floors` array and elements
without `floorId`.

Floor-area and cash-register support reuse the protected layout-element CRUD,
the existing publish/activate workflow, and the public active-layout read.
They add no API route, DynamoDB table, environment variable, or IAM permission.
Optional table and cash-register labels reuse those same resources and
likewise require no new route, table, environment variable, or IAM permission.

Deploy this additive contract in compatibility order because each Lambda image
contains its own snapshot validator: update `list-layout-version`,
`get-availability`, and `block-table` first, then `publish-layout`, and finally
`manage-layout-element`. That keeps every deployed reader able to understand
newly written `floorArea` elements and cash-register labels even if a rollout
stops partway through. An atomic deployment of all five images is also safe.

For pending-activation management, deploy compatible layout readers and
`expire-layout-version` before `activate-layout-version`. Check for legacy
`scheduled` state records without `pendingTargetPreviousLifecycle` and let
them finish or remediate them deliberately before enabling the controls.
Infrastructure must route the exact JWT-protected
`PUT /locations/{locationId}/layout/pending-activation` and
`DELETE /locations/{locationId}/layout/pending-activation` operations to the
existing activation Lambda and add route-scoped invoke permissions. No new
Lambda, table, environment variable, or execution-role permission is required.

Stop and remove the local documentation container when finished:

```powershell
docker compose down
```

Swagger's requests originate in the browser. The selected API must allow
`http://localhost:8081` in its API Gateway CORS configuration even if the same
request already works from Postman or PowerShell. Admin origins must allow
`PUT`, `DELETE`, `Authorization`, and `Content-Type`; CORS preflight must not
require JWT authorization.

## Conventions (see docs/LAMBDA_REFERENCE.md for the full version)

- Every env var is read with `os.environ["NAME"]` at module import time (not
  `.get()`) - a missing env var should fail loudly at cold start, not
  silently at first use.
- JWT-protected routes: API Gateway only proves *a* valid staff account made
  the request. Checking *which* Cognito group they're in, and whether
  they're allowed to act on a specific `locationId`, is the handler's job -
  use `shared.auth.require_group()` / `shared.auth.get_sub()`.
- `NONE`-auth routes are intentionally public - customers never have
  Cognito accounts, so don't add JWT checks to those route branches.
  Authorization is route-specific: `get-menu`, `get-location`, and
  `list-layout-version` each serve public and protected routes. Dispatch only
  from the documented route identity (`routeKey` or path parameter), never
  from the presence of an authorization header or JWT claims.
- Never hardcode table/bucket names - always read them from env vars, since
  the same image runs unmodified against dev and prod resources.
