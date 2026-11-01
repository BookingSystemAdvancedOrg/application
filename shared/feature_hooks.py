"""Feature hooks: shared Lambdas asking a feature's own API Lambda for its
data instead of reading feature tables (CONTRACTS.md section 9).

    from shared import feature_hooks
    results = feature_hooks.call("privacy.export", {"tenantId": t, "email": e})
    # {"gift-cards": {"ok": True, "items": [...]}, ...}

Each target is invoked synchronously as `<prefix><feature-id>:live` with
the envelope `{"hook", "hookVersion": 1, "tenantId", ...fields}`. A
feature that isn't deployed yet (ResourceNotFoundException) has no data
and is skipped (logged once per container). A crash, timeout, non-JSON
answer or any other AWS error raises HookFailed(feature_id): the caller
answers 503 (API) or retries on its next run (jobs). `{"ok": false, ...}`
answers are returned as they are; what they mean is the caller's call.

Tenant-scoped callers pass `targets=targets_for(hook, tenant_row)`: a
feature is only asked when the tenant has its entitlement, so a tenant
without the new features never causes a Lambda invoke (and needs no IAM
grant). tenant.purge asks every target; there an AccessDenied counts as
"not deployed" too, so offboarding never fails on a feature that isn't
wired up yet.

The invoker is a module attribute so tests replace it (tests/hooks_fake.py).
"""

import json
import logging
import os

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from shared import contracts

logger = logging.getLogger(__name__)

HOOK_VERSION = 1
QUALIFIER = "live"
_TIMEOUTS = {"tenant.purge": 60}
_DEFAULT_TIMEOUT = 10

_FEATURE_KEY = {feature_id: key for key, feature_id in contracts.FEATURE_IDS.items()}
_SKIPPED_ON_PURGE = ("AccessDeniedException",)

_clients: dict = {}
_logged_missing: set = set()


class HookFailed(Exception):
    def __init__(self, feature_id):
        super().__init__(f"hook target {feature_id} failed")
        self.feature_id = feature_id


class NotDeployed(Exception):
    """The target function (or its live alias) doesn't exist."""


def targets_for(hook: str, tenant_row) -> list:
    """The hook's targets whose entitlement the tenant has on."""
    features = ((tenant_row or {}).get("entitlements") or {}).get("features") or {}
    return [f for f in contracts.HOOKS[hook]["targets"] if features.get(_FEATURE_KEY[f])]


def prefix() -> str:
    env = os.environ["ENVIRONMENT"]
    return "" if env == "prod" else f"{env}-"


def function_name(feature_id: str) -> str:
    return f"{prefix()}{feature_id}"


def _client(timeout):
    if timeout not in _clients:
        _clients[timeout] = boto3.client("lambda", config=Config(
            connect_timeout=5, read_timeout=timeout, retries={"mode": "standard", "max_attempts": 2}))
    return _clients[timeout]


def _lambda_invoke(name: str, body: bytes, timeout: int):
    """-> (FunctionError or None, response bytes). Raises NotDeployed."""
    try:
        answer = _client(timeout).invoke(FunctionName=name, Qualifier=QUALIFIER, Payload=body)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            raise NotDeployed(name) from exc
        raise
    return answer.get("FunctionError"), answer["Payload"].read()


invoke = _lambda_invoke


def call(hook: str, payload: dict, targets=None) -> dict:
    """{feature_id: response} for every deployed target of `hook`.
    `payload` must carry `tenantId`; `targets` narrows the contract's list."""
    spec = contracts.HOOKS[hook]
    if not isinstance(payload, dict) or not payload.get("tenantId"):
        raise ValueError("hook payload needs tenantId")
    chosen = list(spec["targets"]) if targets is None else list(targets)
    unknown = set(chosen) - set(spec["targets"])
    if unknown:
        raise ValueError(f"{hook} has no target {sorted(unknown)}")
    body = json.dumps({**payload, "hook": hook, "hookVersion": HOOK_VERSION}, default=str).encode()
    timeout = _TIMEOUTS.get(hook, _DEFAULT_TIMEOUT)
    results = {}
    for feature_id in chosen:
        name = function_name(feature_id)
        try:
            error, raw = invoke(name, body, timeout)
        except ClientError as exc:
            if hook != "tenant.purge" or exc.response.get("Error", {}).get("Code") not in _SKIPPED_ON_PURGE:
                logger.error("hook %s: %s failed (%s)", hook, name, type(exc).__name__)
                raise HookFailed(feature_id) from exc
            logger.info("hook %s: %s not allowed yet, skipped", hook, name)
            continue
        except NotDeployed:
            if name not in _logged_missing:
                _logged_missing.add(name)
                logger.info("hook %s: %s not deployed, skipped", hook, name)
            continue
        except (BotoCoreError, ClientError) as exc:
            logger.error("hook %s: %s failed (%s)", hook, name, type(exc).__name__)
            raise HookFailed(feature_id) from exc
        if error:
            logger.error("hook %s: %s FunctionError %s", hook, name, error)
            raise HookFailed(feature_id)
        try:
            answer = json.loads(raw)
        except (TypeError, ValueError):
            answer = None
        if not isinstance(answer, dict) or not isinstance(answer.get("ok"), bool):
            logger.error("hook %s: %s answered without ok", hook, name)
            raise HookFailed(feature_id)
        results[feature_id] = answer
    return results
