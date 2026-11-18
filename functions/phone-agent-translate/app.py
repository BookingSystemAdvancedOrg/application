"""phone-agent-translate

TRIGGER:
    EventBridge rule on the custom bus (EVENT_BUS_NAME): source
    "booqy.phone-agent", detail-type "PhoneCall.Ended" (published once per
    call by calls.process, from the end webhook, the Connect relay or the
    sweep), async invoke with its own retries.

PURPOSE:
    The call transcript in Swedish and English for the admin. Each caller
    and agent turn of the call (`#T#` rows) gets `textSv` and `textEn` from
    Amazon Translate, from the turn's own language (`lang`); a target that is
    already the turn's language is not translated (left null: the original
    is that text). `translation` goes pending -> done, or failed after
    TRANSLATE_ATTEMPTS tries on that turn; empty turns are skipped. The
    original `text` never changes.

    Idempotent: done and skipped turns are left alone, so a second delivery
    of the event only retries failed or pending turns, and each write is
    conditional on the original text being unchanged (a privacy erase in
    between wins). Logs carry ids and counts, never transcript text.

    Retries: a turn that failed for a passing reason (throttling, a service
    error) makes the invocation raise TranslationIncomplete after all turns
    were tried, so Lambda's async retries run it again and, when they are
    used up, the on-failure destination (the translate DLQ, alarmed) keeps
    the event for a replay. A permanent failure (unsupported pair, text too
    long) stays `failed` and does not raise.

ENV_VARS:
    ENVIRONMENT, PHONE_AGENT_TABLE_NAME
"""

import json
import logging
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared.features import phone_agent as pa
from shared.features.phone_agent import calls

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_clients = {}
TARGETS = (("sv", "textSv"), ("en", "textEn"))
TRANSLATE_ATTEMPTS = 3
BACKOFF_S = 0.5
# Our language codes -> Amazon Translate's where they differ. Cantonese (the zh_HK recognizer
# writes traditional characters) goes as Chinese (Traditional); Portuguese is the pt_PT locale.
_TRANSLATE_CODES = {"pt": "pt-PT", "yue": "zh-TW", "fil": "tl"}
# Amazon Translate's source languages (docs, 2026-11-18); anything else goes as "auto".
_SUPPORTED = frozenset((
    "af sq am ar hy az bn bs bg ca zh zh-TW hr cs da fa-AF nl en et fa tl fi fr fr-CA ka de el gu ht ha he hi "
    "hu is id ga it ja kn kk ko lv lt mk ms ml mt mr mn no ps pl pt pt-PT pa ro ru sr si sk sl so es es-MX sw "
    "sv ta te th tr uk ur uz vi cy").split())
_DONE = ("done", "skipped")
# Errors a retry cannot fix: the turn stays failed and the invocation does not raise.
PERMANENT_ERRORS = ("UnsupportedLanguagePairException", "DetectedLanguageLowConfidenceException",
                    "TextSizeLimitExceededException", "InvalidRequestException")


class TranslationIncomplete(RuntimeError):
    """Some turns failed for a passing reason: Lambda retries the event."""


def client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def source_code(lang):
    code = _TRANSLATE_CODES.get(lang, lang)
    return code if code in _SUPPORTED else "auto"


def _translate(text, source, target):
    """Amazon Translate with bounded retries; raises the last error."""
    for attempt in range(TRANSLATE_ATTEMPTS):
        try:
            return client("translate").translate_text(Text=text, SourceLanguageCode=source,
                                                      TargetLanguageCode=target)["TranslatedText"]
        except (BotoCoreError, ClientError) as exc:
            code = exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else ""
            if attempt == TRANSLATE_ATTEMPTS - 1 or code in PERMANENT_ERRORS:
                raise
            time.sleep(BACKOFF_S * (2 ** attempt))
    raise RuntimeError("unreachable")


def translate_turn(turn, call_lang):
    """(fields to set, status) for one turn: textSv/textEn and done, skipped,
    failed (permanent) or retry (failed, a later try may work; saved as failed)."""
    text = str(turn.get("text") or "")
    if not text.strip():
        return {}, "skipped"
    lang = turn.get("lang") or call_lang or "sv"
    out = {}
    try:
        for target, field in TARGETS:
            if lang == target:
                continue
            out[field] = _translate(text, source_code(lang), target)
    except (BotoCoreError, ClientError) as exc:
        code = exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else type(exc).__name__
        logger.warning(json.dumps({"translate": "failed", "code": code}))
        return {}, "failed" if code in PERMANENT_ERRORS else "retry"
    return out, "done"


def _save(turn, fields, status):
    names = {"#x": "text", "#tr": "translation"}
    values = {":orig": turn.get("text") or "", ":s": status, ":done": "done"}
    sets = ["#tr = :s"]
    for i, (field, value) in enumerate(fields.items()):
        names[f"#f{i}"] = field
        values[f":v{i}"] = value
        sets.append(f"#f{i} = :v{i}")
    try:
        pa.table().update_item(
            Key={"PK": turn["PK"], "SK": turn["SK"]}, UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_exists(PK) AND #x = :orig AND "
                                "(attribute_not_exists(#tr) OR #tr <> :done)",
            ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        return True
    except ClientError as exc:
        if pa.conditional_failed(exc):   # erased, or done by a parallel delivery
            return False
        raise


def translate_call(tenant_id, call_id):
    """Every caller and agent turn of one call into Swedish and English."""
    call = calls.get_call(tenant_id, call_id)
    if not call:
        return {"ok": False, "code": "not_found"}
    turns = pa.query_all(pa.tenant_pk(tenant_id), call["SK"] + "#T#", ConsistentRead=True)
    counts = {"done": 0, "failed": 0, "skipped": 0, "unchanged": 0}
    retry = 0
    for turn in turns:
        if turn.get("role") not in calls.TRANSLATED_ROLES or turn.get("translation") in _DONE:
            continue
        fields, status = translate_turn(turn, call.get("language"))
        saved = "failed" if status == "retry" else status
        if _save(turn, fields, saved):
            counts[saved] += 1
            retry += status == "retry"
        else:
            counts["unchanged"] += 1
    logger.info(json.dumps({"tenantId": tenant_id, "callId": call_id, **counts, "retry": retry}))
    if retry:
        raise TranslationIncomplete(f"{retry} turns to retry")
    return {"ok": True, **counts}


def handler(event, context=None):
    detail = (event or {}).get("detail") or {}
    if (event or {}).get("detail-type") != "PhoneCall.Ended" or not isinstance(detail, dict):
        return {"ok": False, "code": "ignored"}
    tenant_id, call_id = detail.get("tenantId"), detail.get("callId")
    if not isinstance(tenant_id, str) or not isinstance(call_id, str) or not pa.call_key(tenant_id, call_id):
        return {"ok": False, "code": "invalid"}
    return translate_call(tenant_id, call_id)
