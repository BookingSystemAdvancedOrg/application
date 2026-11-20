"""site-translate

TRIGGER:
    API Gateway -- POST /translate -- Auth: NONE
    Restaurant sites call it as /api/translate on their own domain, through
    CloudFront and the sites' AWS WAF (challenge, rate limit, IP reputation),
    which adds x-booqy-viewer-host, x-booqy-viewer-ip and x-booqy-waf.

PURPOSE:
    The site languages (English, Spanish, Arabic, Persian; Swedish is the
    source and the default). site-kit sends the Swedish strings a page shows:
        {"lang": "en"|"es"|"ar"|"fa", "texts": [1..100], "keep": [0..200],
         "slug": "<slug>" (outside prod, direct calls only)}
    and gets {"lang", "texts" (same order and length), "partial"} back.

    Which site: the viewer host CloudFront passes on (trusted only with the
    edge secret), resolved like tenant-site-config (DOMAIN#<host>, an active
    domain row, an active tenant). Outside prod a call that has no known
    host may name the restaurant with `slug`. Unknown: 403 unknown_site. In
    prod every call must have come through the WAF (403 bot_check_failed).

    Each text is protected (names in `keep` plus the restaurant's own name
    and legal name from its tenant row, prices, times, numbers, phone
    numbers, e-mail addresses, URLs wrapped in translate="no"). A text the
    reviewed glossary (shared/site_translate_glossary.json: the kit's UI
    labels and recurring terms) covers is answered from it, free and
    uncounted. Everything else is looked up in
    the cache (in-memory LRU, then DynamoDB, keyed by a hash of the
    protected text), and only the misses go to Amazon Translate, within the
    character budgets (per IP per hour, per restaurant and for the whole
    platform per 30 days). A miss over a budget, a Translate failure or a
    translation that changed a protected part comes back as the Swedish
    source and sets "partial": the site stays Swedish there. Never 5xx for
    a translation failure.

    Privacy: logs carry tenantId, lang and counts only, never a text, a
    keep term or an IP. The cache holds the hash and the translation only.

ENV_VARS:
    ENVIRONMENT, TRANSLATION_TABLE_NAME, TENANT_TABLE_NAME,
    REQUEST_GUARD_TABLE_NAME, SITE_WAF_SECRET, CLOUDFRONT_EDGE_SECRET,
    SITE_TRANSLATE_IP_REQUESTS_PER_5MIN (120), SITE_TRANSLATE_IP_CHARS_PER_HOUR (200000),
    SITE_TRANSLATE_TENANT_CHARS_PER_30D (500000), SITE_TRANSLATE_GLOBAL_CHARS_PER_30D
    (5000000)
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

from shared import bot_check, dynamo, http, request_guard, tenant
from shared import site_translate as st

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ROUTE = ("POST", "/translate")
WINDOW_30D = 30 * 86400
_HOST = re.compile(r"(?=.{1,253}\Z)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*")
_SITE_CACHE_S = 60
_sites = {}


def _limit(name, default):
    try:
        value = int(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _prod():
    return (os.environ.get("ENVIRONMENT") or "prod") == "prod"


def _tenant_of(key):
    item = dynamo.table(os.environ["TENANT_TABLE_NAME"]).get_item(
        Key={"PK": key, "SK": "TENANT"}).get("Item") or {}
    tenant_id = item.get("tenantId")
    return tenant_id if isinstance(tenant_id, str) and tenant_id else None


def _active(tenant_id):
    row = tenant.get_tenant(tenant_id)
    return bool(row) and row.get("status") == "active"


def _by_host(raw):
    host = raw.strip().lower().rstrip(".").split(":", 1)[0]
    if not _HOST.fullmatch(host):
        return None
    tenant_id = _tenant_of(f"DOMAIN#{host}")
    if not tenant_id:
        return None
    row = dynamo.table(os.environ["TENANT_TABLE_NAME"]).get_item(
        Key={"PK": tenant.tenant_pk(tenant_id), "SK": f"DOMAIN#{host}"}).get("Item") or {}
    return tenant_id if row.get("status") == "active" else None


def _resolve(kind, value):
    """tenantId for ("host"|"slug", value), or None; cached a minute per
    container (a site asks several times per page)."""
    now = time.monotonic()
    hit = _sites.get((kind, value))
    if hit and hit[0] > now:
        return hit[1]
    tenant_id = _by_host(value) if kind == "host" else _tenant_of(f"SLUG#{value}")
    if tenant_id and not _active(tenant_id):
        tenant_id = None
    if len(_sites) > 1000:
        _sites.clear()
    _sites[(kind, value)] = (now + _SITE_CACHE_S, tenant_id)
    return tenant_id


def site_of(event, slug):
    host = http.edge_header(event, "x-booqy-viewer-host")
    tenant_id = _resolve("host", host) if host else None
    if not tenant_id and slug and not _prod():
        tenant_id = _resolve("slug", slug)
    return tenant_id


def site_terms(tenant_id):
    """The restaurant's own names, protected in every text whatever the
    site sends: name and legal name from the tenant row (also in capitals,
    as headings often write it)."""
    row = tenant.get_tenant(tenant_id) or {}
    terms = set()
    for field in ("name", "legalName"):
        value = row.get(field)
        if isinstance(value, str) and 0 < len(value.strip()) <= st.MAX_KEEP_CHARS:
            terms.add(value.strip())
            terms.add(value.strip().upper())
    return terms


def _budgets(tenant_id, ip):
    return [
        ("tr-ip-chars", ip or "", _limit("SITE_TRANSLATE_IP_CHARS_PER_HOUR", 200000), 3600),
        ("tr-tenant-chars", tenant_id, _limit("SITE_TRANSLATE_TENANT_CHARS_PER_30D", 500000), WINDOW_30D),
        ("tr-global-chars", "all", _limit("SITE_TRANSLATE_GLOBAL_CHARS_PER_30D", 5000000), WINDOW_30D),
    ]


def _take(budgets, amount, now):
    """True when every budget had room for ``amount`` (then all are
    counted); otherwise the ones already counted are given back."""
    taken = []
    for kind, value, max_amount, window in budgets:
        if not value:
            continue
        if not request_guard.consume(kind, value, amount, max_amount, window, now):
            for k, v, w in taken:
                request_guard.give_back(k, v, amount, w, now)
            return False
        taken.append((kind, value, window))
    return True


def _give_back(budgets, amount, now):
    for kind, value, _, window in budgets:
        if value:
            request_guard.give_back(kind, value, amount, window, now)


def translate(tenant_id, ip, lang, texts, keep, now):
    """(translated texts, partial, counts)."""
    epoch = int(now.timestamp())
    jobs = []  # per text: (lead, core, trail, marked, tokens, key or None)
    glossary_hits = 0
    for text in texts:
        core = text.strip()
        lead = text[:len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        marked, tokens = st.protect(core, keep)
        key = None
        if st.needs_translation(marked):
            reviewed = st.glossary(lang, core, marked, tokens)
            if reviewed is not None:
                # Answered by the reviewed glossary: no cache, no Translate,
                # no budget.
                core = reviewed
                glossary_hits += 1
            else:
                key = st.cache_key(lang, marked)
        jobs.append((lead, core, trail, marked, tokens, key))

    keys = list(dict.fromkeys(j[5] for j in jobs if j[5]))
    found = st.cache_get(keys, epoch)
    misses = {}
    for _, _, _, marked, tokens, key in jobs:
        if key and key not in found and key not in misses:
            misses[key] = (marked, tokens)

    counts = {"Requests": 1, "GlossaryHits": glossary_hits, "CacheHits": len(keys) - len(misses),
              "Misses": len(misses),
              "TranslatedChars": 0, "BudgetLimited": 0, "TokenCheckFailed": 0, "TranslateErrors": 0}
    if misses:
        chars = sum(len(m) for m, _ in misses.values())
        budgets = _budgets(tenant_id, ip)
        if _take(budgets, chars, now):
            raw = st.translate_many([(k, m) for k, (m, _) in misses.items()], lang)
            fresh, failed_chars = {}, 0
            for key, (marked, tokens) in misses.items():
                out = raw.get(key)
                if out is None:
                    counts["TranslateErrors"] += 1
                    failed_chars += len(marked)
                    continue
                plain = st.restore(out, tokens, lang)
                if plain is None:
                    counts["TokenCheckFailed"] += 1
                    continue
                fresh[key] = plain
            counts["TranslatedChars"] = chars - failed_chars
            # Characters that Translate never billed go back to the budgets.
            _give_back(budgets, failed_chars, now)
            st.cache_put(fresh, epoch)
            found.update(fresh)
        else:
            counts["BudgetLimited"] = len(misses)

    out, partial = [], False
    for lead, core, trail, _, _, key in jobs:
        if key is None:
            out.append(lead + core + trail)
        elif key in found:
            out.append(lead + found[key] + trail)
        else:
            out.append(lead + core + trail)
            partial = True
    return out, partial, counts


def handler(event, context=None):
    if http.route(event) != ROUTE:
        return http.error(404, "not_found")
    try:
        body = http.body(event, allowed=st.ALLOWED_FIELDS)
        lang, texts, keep, slug = st.validate(body, prod=_prod())
    except ValueError as exc:
        code = str(exc) if str(exc) in ("unsupported_lang", "invalid_request") else "invalid_request"
        return http.error(400, code)

    if _prod() and not bot_check.passed(event):
        logger.warning(json.dumps({"bot_check": "refused", "form": "translate"}))
        return http.error(403, bot_check.FAILED)
    try:
        tenant_id = site_of(event, slug)
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"site": "lookup_failed", "type": type(exc).__name__}))
        return http.error(503, "service_unavailable")
    if not tenant_id:
        return http.error(403, "unknown_site")

    now = datetime.now(timezone.utc)
    ip = http.source_ip(event)
    try:
        request_guard.check([("tr-ip", ip, _limit("SITE_TRANSLATE_IP_REQUESTS_PER_5MIN", 120), 300)], now)
    except request_guard.Limited as exc:
        return http.respond(429, {"error": "rate_limited"}, {"Retry-After": str(exc.retry_after)})

    try:
        own = site_terms(tenant_id)
    except (BotoCoreError, ClientError):
        own = set()
    if own:
        keep = sorted(set(keep) | own, key=lambda t: (-len(t), t))
    translated, partial, counts = translate(tenant_id, ip, lang, texts, keep, now)
    st.metrics(counts)
    logger.info(json.dumps({"tenantId": tenant_id, "lang": lang, "items": len(texts),
                            "glossary": counts["GlossaryHits"], "hits": counts["CacheHits"], "misses": counts["Misses"],
                            "chars": counts["TranslatedChars"], "budgetLimited": counts["BudgetLimited"],
                            "tokenCheckFailed": counts["TokenCheckFailed"],
                            "errors": counts["TranslateErrors"]}))
    return http.respond(200, {"lang": lang, "texts": translated, "partial": partial})
