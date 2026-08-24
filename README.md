# application

Python Lambda backend for the reservation platform. Each of the 20 functions
in `functions/` is built and deployed as its own container image, on its own
ECR repository, defined in the separate `infrastructure` repo.

## What to read first

**`docs/LAMBDA_REFERENCE.md`** is the single source of truth for what every
function should do, which AWS resources it can access, what environment
variables it has, and how it's invoked (API Gateway route, DynamoDB Stream,
EventBridge Scheduler, or Function URL). You should be able to implement any
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

Stop and remove the local documentation container when finished:

```powershell
docker compose down
```

Swagger's requests originate in the browser. The selected API must allow
`http://localhost:8081` in its API Gateway CORS configuration even if the same
request already works from Postman or PowerShell.

## Conventions (see docs/LAMBDA_REFERENCE.md for the full version)

- Every env var is read with `os.environ["NAME"]` at module import time (not
  `.get()`) - a missing env var should fail loudly at cold start, not
  silently at first use.
- JWT-protected routes: API Gateway only proves *a* valid staff account made
  the request. Checking *which* Cognito group they're in, and whether
  they're allowed to act on a specific `locationId`, is the handler's job -
  use `shared.auth.require_group()` / `shared.auth.get_sub()`.
- `NONE`-auth routes (`get-menu`, `get-availability`,
  `create-pending-reservation`, `cancel-reservation`, `manage-auth`) are
  intentionally public - customers never have Cognito accounts, don't add
  JWT checks there.
- Never hardcode table/bucket names - always read them from env vars, since
  the same image runs unmodified against dev and prod resources.
