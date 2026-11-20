"""Machine translation of a restaurant site's own Swedish texts (SPEC site
languages, section 1).

The site (site-kit's DOM translator) sends the Swedish strings it shows;
they come back in English, Spanish, Arabic or Persian from Amazon Translate,
cached in one DynamoDB table so each string is translated once across all
sites.

Pure parts (unit tested without AWS):
    validate()   -- the request body
    protect()    -- names, numbers, prices, times, phone numbers, e-mail
                    addresses and URLs wrapped in <span translate="no">, which
                    Amazon Translate keeps as written
    cache_key()  -- sha256 of the protected text, language and formality
    restore()    -- checks that every protected token came back unchanged,
                    strips the markup; None when the check fails

Privacy: nothing here logs a text, a keep term or an IP address, and the
cache stores only the hash of the source and the translation.
"""

import hashlib
import html
import json
import logging
import os
import re
import threading
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo

logger = logging.getLogger(__name__)

SOURCE = "sv"
TARGETS = ("en", "es", "ar", "fa")
# Spanish in the informal register to match the Swedish "du" voice. Amazon
# Translate supports formality only for some targets; of ours only es.
FORMALITY = {"es": "INFORMAL"}
VERSION = "v1"

MAX_TEXTS = 100
MAX_TEXT_CHARS = 3000
MAX_TOTAL_CHARS = 20000
MAX_KEEP = 200
MAX_KEEP_CHARS = 120
ALLOWED_FIELDS = ("lang", "texts", "keep", "slug")

TTL_DAYS = 180
REFRESH_BELOW_DAYS = 30
LRU_SIZE = 5000
THREADS = 8
ATTEMPTS = 3
BACKOFF_S = 0.3
# Errors a retry can fix (throttling, a service hiccup).
RETRYABLE = ("ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException",
             "InternalServerException", "LimitExceededException")


# --- validation ------------------------------------------------------------------------------

def validate(body, *, prod):
    """(lang, texts, keep, slug) from the parsed body, or ValueError with an
    error code ("unsupported_lang", "invalid_request")."""
    lang = body.get("lang")
    if not isinstance(lang, str) or not lang:
        raise ValueError("invalid_request")
    if lang not in TARGETS:
        raise ValueError("unsupported_lang")
    texts = body.get("texts")
    if not isinstance(texts, list) or not 1 <= len(texts) <= MAX_TEXTS:
        raise ValueError("invalid_request")
    total = 0
    for text in texts:
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_TEXT_CHARS:
            raise ValueError("invalid_request")
        total += len(text.strip())
    if total > MAX_TOTAL_CHARS:
        raise ValueError("invalid_request")
    keep = body.get("keep", [])
    if keep is None:
        keep = []
    if not isinstance(keep, list) or len(keep) > MAX_KEEP:
        raise ValueError("invalid_request")
    terms = []
    for term in keep:
        if not isinstance(term, str) or not term.strip() or len(term.strip()) > MAX_KEEP_CHARS:
            raise ValueError("invalid_request")
        terms.append(term.strip())
    slug = body.get("slug")
    if slug is not None:
        if prod or not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?",
                                                                  slug.strip().lower()):
            raise ValueError("invalid_request")
        slug = slug.strip().lower()
    return lang, texts, sorted(set(terms), key=lambda t: (-len(t), t)), slug


# --- protecting what must not change ---------------------------------------------------------

_OPEN = '<span translate="no">'
_CLOSE = "</span>"
_DASH = chr(0x2013)
_NUM = r"\+?\d+(?:[ \u00a0.,:/" + _DASH + r"-]\d+)*"
_PATTERNS = (
    re.compile(r"(?:https?://|www\.)[^\s<>\"']+[^\s<>\"'.,;:!?)\]]", re.IGNORECASE),
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    # amounts, prices, times, dates, phone numbers, percentages and any other digits
    re.compile(r"(?:\bSEK[ \u00a0]?)?" + _NUM + r"(?:[ \u00a0]?(?:kr\b|SEK\b|:-|%))?"),
    re.compile(r"\bSEK\b"),
)
_SPAN = re.compile(r"<span\s+translate\s*=\s*[\"']no[\"']\s*>(.*?)</span\s*>", re.DOTALL | re.IGNORECASE)
_LETTER = re.compile(r"[^\W\d_]")


def _spans(text, keep):
    """Non-overlapping (start, end) ranges to protect, longest first."""
    found = []
    for term in keep:
        for m in re.finditer(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text):
            found.append((m.start(), m.end()))
    for pattern in _PATTERNS:
        for m in pattern.finditer(text):
            found.append((m.start(), m.end()))
    found.sort(key=lambda s: (-(s[1] - s[0]), s[0]))
    taken = []
    for start, end in found:
        if all(end <= a or start >= b for a, b in taken):
            taken.append((start, end))
    return sorted(taken)


def protect(text, keep=()):
    """(marked, tokens): the HTML-escaped text with every protected part in
    <span translate="no">, and the protected parts (escaped) in order."""
    out, tokens, pos = [], [], 0
    for start, end in _spans(text, keep):
        out.append(html.escape(text[pos:start], quote=False))
        token = html.escape(text[start:end], quote=False)
        tokens.append(token)
        out.append(_OPEN + token + _CLOSE)
        pos = end
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out), tokens


def needs_translation(marked):
    """False when nothing outside the protected parts has a letter (a bare
    name, price or time): it stays as it is, nothing is sent or billed."""
    return bool(_LETTER.search(html.unescape(_SPAN.sub("", marked))))


def cache_key(lang, marked):
    formality = FORMALITY.get(lang, "")
    digest = hashlib.sha256(f"{VERSION}|{lang}|{formality}|{marked}".encode("utf-8")).hexdigest()
    return f"T#{lang}#{digest}"


_DASHES = re.compile(r"\s*[" + chr(0x2013) + chr(0x2014) + r"]\s*")


def restore(translated, tokens):
    """The plain translation, or None when a protected part did not come
    back exactly (missing, changed, duplicated) or stray markup is left."""
    if not isinstance(translated, str):
        return None
    if Counter(_SPAN.findall(translated)) != Counter(tokens):
        return None
    parts, pos = [], 0
    for m in _SPAN.finditer(translated):
        parts.append(_free_text(translated[pos:m.start()]))
        parts.append(m.group(1))
        pos = m.end()
    parts.append(_free_text(translated[pos:]))
    if any(part is None for part in parts):
        return None
    return html.unescape("".join(parts)).strip()


def _free_text(segment):
    """A translated stretch between protected parts: None with stray markup;
    long dashes become a comma or a hyphen (house style)."""
    if re.search(r"</?span\b|translate\s*=", segment, re.IGNORECASE):
        return None
    return _DASHES.sub(lambda m: ", " if m.group(0) != m.group(0).strip() else "-", segment)


# --- cache -----------------------------------------------------------------------------------

class _Lru:
    def __init__(self, size):
        self.size, self.items, self.lock = size, OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return self.items[key]
        return None

    def put(self, key, value):
        with self.lock:
            self.items[key] = value
            self.items.move_to_end(key)
            while len(self.items) > self.size:
                self.items.popitem(last=False)


_lru = _Lru(LRU_SIZE)
_clients = {}


def _translate_client():
    if "translate" not in _clients:
        _clients["translate"] = boto3.client("translate", config=Config(retries={"max_attempts": 1},
                                                                        connect_timeout=3, read_timeout=8))
    return _clients["translate"]


def _table_name():
    return os.environ.get("TRANSLATION_TABLE_NAME") or ""


def cache_get(keys, now):
    """{key: translated} for the keys found (LRU, then DynamoDB). A hit with
    under REFRESH_BELOW_DAYS left gets its TTL pushed out."""
    found, wanted = {}, []
    for key in keys:
        hit = _lru.get(key)
        if hit is not None:
            found[key] = hit
        else:
            wanted.append(key)
    name = _table_name()
    if not wanted or not name:
        return found
    stale = []
    for i in range(0, len(wanted), 100):
        request = {name: {"Keys": [{"PK": {"S": k}} for k in wanted[i:i + 100]],
                          "ProjectionExpression": "PK, #t, #ttl",
                          "ExpressionAttributeNames": {"#t": "text", "#ttl": "ttl"}}}
        for attempt in range(4):
            try:
                result = dynamo.client().batch_get_item(RequestItems=request)
            except (BotoCoreError, ClientError) as exc:
                logger.error(json.dumps({"cache": "get_failed", "type": type(exc).__name__}))
                break
            for item in result.get("Responses", {}).get(name, []):
                key, text = item["PK"]["S"], item.get("text", {}).get("S")
                if text is None:
                    continue
                found[key] = text
                _lru.put(key, text)
                ttl = int(item.get("ttl", {}).get("N", "0"))
                if ttl - now < REFRESH_BELOW_DAYS * 86400:
                    stale.append(key)
            request = result.get("UnprocessedKeys") or {}
            if not request:
                break
            time.sleep(0.05 * (2 ** attempt))
    for key in stale:
        try:
            dynamo.table(name).update_item(Key={"PK": key}, UpdateExpression="SET #ttl = :ttl",
                                           ExpressionAttributeNames={"#ttl": "ttl"},
                                           ExpressionAttributeValues={":ttl": now + TTL_DAYS * 86400})
        except (BotoCoreError, ClientError):
            pass
    return found


def cache_put(entries, now):
    """Stores {key: translated} (best effort: a failed write only costs a
    second translation later)."""
    for key, text in entries.items():
        _lru.put(key, text)
    name = _table_name()
    if not entries or not name:
        return
    items = [{"PutRequest": {"Item": {"PK": {"S": k}, "text": {"S": v},
                                      "ttl": {"N": str(now + TTL_DAYS * 86400)}}}}
             for k, v in entries.items()]
    for i in range(0, len(items), 25):
        request = {name: items[i:i + 25]}
        for attempt in range(4):
            try:
                result = dynamo.client().batch_write_item(RequestItems=request)
            except (BotoCoreError, ClientError) as exc:
                logger.error(json.dumps({"cache": "put_failed", "type": type(exc).__name__}))
                break
            request = result.get("UnprocessedItems") or {}
            if not request:
                break
            time.sleep(0.05 * (2 ** attempt))


# --- Amazon Translate ------------------------------------------------------------------------

def translate_one(marked, lang):
    """The raw translation of one marked text, or None (logged by code)."""
    kwargs = {"Text": marked, "SourceLanguageCode": SOURCE, "TargetLanguageCode": lang}
    if lang in FORMALITY:
        kwargs["Settings"] = {"Formality": FORMALITY[lang]}
    for attempt in range(ATTEMPTS):
        try:
            return _translate_client().translate_text(**kwargs)["TranslatedText"]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in RETRYABLE or attempt == ATTEMPTS - 1:
                logger.warning(json.dumps({"translate": "failed", "code": code}))
                return None
        except BotoCoreError as exc:
            if attempt == ATTEMPTS - 1:
                logger.warning(json.dumps({"translate": "failed", "code": type(exc).__name__}))
                return None
        time.sleep(BACKOFF_S * (2 ** attempt))
    return None


def translate_many(items, lang):
    """{key: raw translation or None} for [(key, marked)], 8 at a time."""
    if not items:
        return {}
    if len(items) == 1:
        key, marked = items[0]
        return {key: translate_one(marked, lang)}
    with ThreadPoolExecutor(max_workers=min(THREADS, len(items))) as pool:
        results = pool.map(lambda item: translate_one(item[1], lang), items)
        return {key: out for (key, _), out in zip(items, results)}


def metrics(values):
    """CloudWatch metrics (namespace Booqy/SiteTranslate) as one embedded
    metric format log line: counts only, never text."""
    print(json.dumps({
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
            "Namespace": "Booqy/SiteTranslate", "Dimensions": [[]],
            "Metrics": [{"Name": name, "Unit": "Count"} for name in values]}]},
        **values,
    }))
