"""In-app help assistant for Booqy Admin and Booqy Console (infrastructure help-assistant/).

    GET  /tenant/help-assistant             {enabled, mode: "ai" | "kb"}       Admin, tenant JWT
    POST /tenant/help-assistant/answers     {question, history?} -> {answer, mode}
    GET  /platform/help-assistant           the same for Booqy Console, operator JWT (platform_admin)
    POST /platform/help-assistant/answers

Admin answers are grounded in the admin corpus and filtered by the caller's
role and the tenant's plan; console answers use the console corpus
(shared/help_assistant.py). Off unless HELP_ASSISTANT_ENABLED=true and the
PLATFORM#HELP_ASSISTANT/SETTINGS row doesn't say {enabled: false} (the kill
switch). Per user: 6 questions a minute, 60 an hour. Bedrock answers are
capped per tenant (operators share one "platform" budget) and for the whole
platform per 30-day window; past a cap, or on any model error, the answer is
composed from the corpus alone.

Live numbers ("how many guests today?") are never fetched here: the answer
carries `live: {metric, text}` and the app reads the number from the
existing authenticated APIs with the caller's own token, so tenant,
location and role scoping stay with those APIs. Read only.

Privacy: questions and answers are never stored or logged; the log line has
only the outcome. The conversation lives in the app tab's memory.
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
OPERATOR_GROUP = "platform_admin"

_switch = {"at": 0.0, "on": True}
_model = None


class Caller:
    def __init__(self, app, sub, role, tenant_id=None, tenant_row=None):
        self.app, self.sub, self.role, self.tenant_id, self.tenant_row = app, sub, role, tenant_id, tenant_row


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


def _within_caps(budget) -> bool:
    """Counts one model answer for the budget (tenant or "platform") and the whole platform."""
    try:
        request_guard.check([
            ("help-tenant", budget, _cap("HELP_ASSISTANT_TENANT_MONTHLY", 300), WINDOW_30_DAYS),
            ("help-platform", "all", _cap("HELP_ASSISTANT_PLATFORM_MONTHLY", 10000), WINDOW_30_DAYS),
        ], _now())
        return True
    except request_guard.Limited:
        return False


def _groups(claims: dict) -> set:
    raw = claims.get("cognito:groups") or ""
    if isinstance(raw, list):
        return set(raw)
    return {g.strip(" ,\"'") for g in str(raw).strip("[]").replace(",", " ").split() if g.strip(" ,\"'")}


def _operator(event) -> Caller:
    """The platform authorizer (operator pool) already checked the token; require the operator group too."""
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    sub = claims.get("sub")
    if not sub or OPERATOR_GROUP not in _groups(claims):
        raise tenant.TenantError(403, "forbidden")
    return Caller("console", sub, "operator")


def status(event, caller):
    return http.respond(200, {"enabled": True, "mode": "ai" if _ai() else "kb"})


def answers(event, caller):
    question, history = help.parse_request(http.body(event, allowed=("question", "history")))
    try:
        request_guard.check([("help-user-min", caller.sub, PER_MINUTE, 60),
                             ("help-user-hour", caller.sub, PER_HOUR, 3600)], _now())
    except request_guard.Limited as exc:
        return http.respond(429, {"error": "rate_limited"}, headers={"Retry-After": str(exc.retry_after)})

    app = caller.app
    view = help.caller_view(caller.tenant_row, caller.role)
    data = help.corpus(app)
    previous = next((t["text"] for t in reversed(history) if t["role"] == "user"), "")
    answer, found = help.answer_locally(data, question, view, previous, app)
    outcome, mode = ("kb" if found else ("refused" if answer["text"] == help.TEXTS[app]["refuse"] else "unknown")), "kb"
    if found and _ai():
        if not _within_caps(caller.tenant_id or "platform"):
            outcome = "capped"
        else:
            try:
                raw = _text_model().complete(help.SYSTEM[app], help.prompt(question, history, found, app, answer.get("live")),
                                             max_tokens=MAX_TOKENS, json_schema=help.ANSWER_SCHEMA)
                model_answer = help.from_model(raw, found, question, app)
                if model_answer is None:
                    outcome = "model_bad_output"
                else:
                    answer, outcome, mode = model_answer, "ai", "ai"
            except TextModelError as exc:
                outcome = f"model_{exc.code}"
    # Outcome only: never the question, the answer or who asked.
    logger.info(json.dumps({"help": outcome, "app": app, "known": answer["known"], "role": caller.role}))
    return http.respond(200, {"answer": answer, "mode": mode})


ROUTES = {
    "GET /tenant/help-assistant": status,
    "POST /tenant/help-assistant/answers": answers,
    "GET /platform/help-assistant": status,
    "POST /platform/help-assistant/answers": answers,
}


def handler(event, context=None):
    method, path = http.route(event)
    fn = ROUTES.get(f"{method} {path}")
    if fn is None:
        return http.error(404, "not_found")
    try:
        if path.startswith("/platform/"):
            caller = _operator(event)
        else:
            ctx = tenant.for_jwt(event)
            caller = Caller("admin", ctx.sub, ctx.role, ctx.tenant_id, ctx.tenant)
        if not _enabled():
            if fn is status:
                return http.respond(200, {"enabled": False, "mode": "off"})
            return http.error(403, "help_assistant_off")
        return fn(event, caller)
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"help": "aws", "error": type(exc).__name__}))
        return http.error(503, "service_unavailable")
