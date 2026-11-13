"""In-app help assistant for Booqy Admin (infrastructure help-assistant/).

    GET  /tenant/help-assistant           {enabled, mode: "ai" | "kb"}
    POST /tenant/help-assistant/answers   {question, history?} -> {answer, mode}

Tenant-pool JWT, owners and staff. The answer is grounded in the help
knowledge base and filtered by the caller's role and the tenant's plan
(shared/help_assistant.py). Off unless HELP_ASSISTANT_ENABLED=true and the
PLATFORM#HELP_ASSISTANT/SETTINGS row doesn't say {enabled: false} (the kill
switch). Per user: 6 questions a minute, 60 an hour. Bedrock answers are
capped per tenant and for the platform per 30-day window; past a cap, or on
any model error, the answer comes from the knowledge base alone.

Privacy: questions and answers are never stored or logged; the log line has
only the outcome. The conversation lives in the admin tab's memory.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

from shared import external_client, help_assistant as help, http, request_guard, tenant
from shared.bedrock import TextModelError
from shared.dynamo import table

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PER_MINUTE, PER_HOUR = 6, 60
WINDOW_30_DAYS = 30 * 24 * 3600
MAX_TOKENS = 700
KILL_SWITCH_KEY = {"PK": "PLATFORM#HELP_ASSISTANT", "SK": "SETTINGS"}
KILL_SWITCH_SECONDS = 60

_switch = {"at": 0.0, "on": True}
_model = None


def _now():
    return datetime.now(timezone.utc)


def _enabled() -> bool:
    if os.environ.get("HELP_ASSISTANT_ENABLED") != "true":
        return False
    if time.monotonic() - _switch["at"] > KILL_SWITCH_SECONDS:
        try:
            row = table(os.environ["TENANT_TABLE_NAME"]).get_item(Key=KILL_SWITCH_KEY).get("Item") or {}
            _switch["on"] = row.get("enabled") is not False
        except (BotoCoreError, ClientError):
            logger.error(json.dumps({"help": "kill_switch_unreadable"}))
        _switch["at"] = time.monotonic()
    return _switch["on"]


def _ai() -> bool:
    return external_client.mode("bedrock") == external_client.REAL and bool(os.environ.get("BEDROCK_MODEL_ID"))


def _text_model():
    global _model
    if _model is None:
        from shared import bedrock
        _model = bedrock.client()
    return _model


def _cap(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _within_caps(tenant_id) -> bool:
    """Counts one model answer for the tenant and the platform; False past a cap."""
    try:
        request_guard.check([
            ("help-tenant", tenant_id, _cap("HELP_ASSISTANT_TENANT_MONTHLY", 300), WINDOW_30_DAYS),
            ("help-platform", "all", _cap("HELP_ASSISTANT_PLATFORM_MONTHLY", 10000), WINDOW_30_DAYS),
        ], _now())
        return True
    except request_guard.Limited:
        return False


def status(event, ctx):
    return http.respond(200, {"enabled": True, "mode": "ai" if _ai() else "kb"})


def answers(event, ctx):
    question, history = help.parse_request(http.body(event, allowed=("question", "history")))
    try:
        request_guard.check([("help-user-min", ctx.sub, PER_MINUTE, 60),
                             ("help-user-hour", ctx.sub, PER_HOUR, 3600)], _now())
    except request_guard.Limited as exc:
        return http.respond(429, {"error": "rate_limited"}, headers={"Retry-After": str(exc.retry_after)})

    view = help.caller_view(ctx.tenant, ctx.role)
    found, owner_hit = help.candidates(question, view)
    if not found:
        last = next((t["text"] for t in reversed(history) if t["role"] == "user"), "")
        if last:
            found, owner_hit = help.candidates(f"{last} {question}", view)
    outcome, mode = "kb", "kb"
    if not found:
        answer, outcome = help.unknown_answer(owner_hit), "unknown"
    else:
        answer = help.kb_answer(found[0])
        if _ai():
            if not _within_caps(ctx.tenant_id):
                outcome = "capped"
            else:
                try:
                    raw = _text_model().complete(help.SYSTEM, help.prompt(question, history, found),
                                                 max_tokens=MAX_TOKENS, json_schema=help.ANSWER_SCHEMA)
                    model_answer = help.from_model(raw, found)
                    if model_answer is None:
                        outcome = "model_bad_output"
                    else:
                        answer, outcome, mode = model_answer, "ai", "ai"
                except TextModelError as exc:
                    outcome = f"model_{exc.code}"
    # Outcome only: never the question, the answer or who asked.
    logger.info(json.dumps({"help": outcome, "known": answer["known"], "role": ctx.role}))
    return http.respond(200, {"answer": answer, "mode": mode})


ROUTES = {
    "GET /tenant/help-assistant": status,
    "POST /tenant/help-assistant/answers": answers,
}


def handler(event, context=None):
    method, path = http.route(event)
    fn = ROUTES.get(f"{method} {path}")
    if fn is None:
        return http.error(404, "not_found")
    try:
        ctx = tenant.for_jwt(event)
        if not _enabled():
            if fn is status:
                return http.respond(200, {"enabled": False, "mode": "off"})
            return http.error(403, "help_assistant_off")
        return fn(event, ctx)
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"help": "aws", "error": type(exc).__name__}))
        return http.error(503, "service_unavailable")
