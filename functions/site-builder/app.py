"""site-builder (removed feature, stub)

TRIGGER:
    API Gateway: every /tenant/site-builder* and /public/site-builder/preview
    route that still exists in infrastructure.
    Step Functions `<prefix>site-builder-publish` tasks (no requestContext).
    Lambda invoke: {"hook": ...} from tenant-purge or platform-tenants.

PURPOSE:
    The site builder was removed from Booqy (owner 2026-11-17). Its AWS
    resources (Lambda, ECR, routes, table, workflow) stay until a later
    infrastructure release destroys them, so this stub keeps the Lambda
    deployable and makes sure nothing can use the feature: every route
    answers 410 Gone, every workflow task fails, and hooks do nothing.
    The contract no longer lists site-builder as a hook target, so
    tenant-purge and platform-tenants do not call it; if an older caller
    still does, tenant.purge answers ok (the table is empty) and other hooks
    answer gone. Delete this folder in the same release that removes the
    infrastructure (Lambda and ECR repository) for site-builder.

ENV_VARS:
    none read
"""

from shared import http

GONE = {"error": "gone", "code": "gone"}


def handler(event, context=None):
    if isinstance(event, dict) and "requestContext" not in event:
        if event.get("hook") == "tenant.purge":
            return {"ok": True, "done": True, "counts": {}}
        if "hook" in event:
            return {"ok": False, "status": 410, "code": "gone"}
        raise RuntimeError("site builder removed")
    return http.respond(410, dict(GONE))
