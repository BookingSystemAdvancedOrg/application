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
    glossary()   -- the reviewed translation of a kit UI string or recurring
                    term (site_translate_glossary.json), used instead of
                    Amazon Translate: short labels have no context there
                    ("Bekräfta bord" came back as a spreadsheet table)
    terminology()  -- the Booqy custom terminology for whole sentences
                    (bord, platser, sällskap, ...), passed to Translate as
                    TerminologyNames; the CSV lives in infrastructure
                    site-translate/terminology.csv

Privacy: nothing here logs a text, a keep term or an IP address, and the
cache stores only the hash of the source and the translation.
"""

import hashlib
import html
import json
import logging
import os
import re
from pathlib import Path
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
# v2: names and numbers in Arabic and Persian come back inside Unicode
# isolates (see restore), so the v1 entries are not reused.
# v3: Translate gets the Booqy custom terminology (bord = restaurant table,
# platser = seats, ...), so the v2 entries (tables as spreadsheets, seats as
# web sites) are not served again.
VERSION = "v3"
RTL = ("ar", "fa")

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
# No new Translate call starts later than this after the request began, so a
# slow or throttled Translate can't run the Lambda (28 s) or API Gateway
# (29 s) into a timeout: the late strings stay Swedish (partial) instead.
DEADLINE_S = 15
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


def terminology():
    """(name, version) of the Booqy custom terminology Terraform imported
    (infrastructure site-translate/terminology.csv), or (None, "") when the
    function runs without one. The version is a hash of the CSV, so an edited
    terminology gets new cache entries."""
    name = (os.environ.get("SITE_TRANSLATE_TERMINOLOGY") or "").strip()
    if not name:
        return None, ""
    return name, (os.environ.get("SITE_TRANSLATE_TERMINOLOGY_HASH") or "").strip()


def cache_key(lang, marked):
    formality = FORMALITY.get(lang, "")
    terms = terminology()[1]
    digest = hashlib.sha256(f"{VERSION}|{lang}|{formality}|{terms}|{marked}".encode("utf-8")).hexdigest()
    return f"T#{lang}#{digest}"


_DASHES = re.compile(r"\s*[" + chr(0x2013) + chr(0x2014) + r"]\s*")


def restore(translated, tokens, lang=None):
    """The plain translation, or None when a protected part did not come
    back exactly (missing, changed, duplicated) or stray markup is left.
    In Arabic and Persian each protected part with a Latin letter or a digit
    is wrapped in Unicode isolates, so a name, price or phone number keeps
    its own order inside the right-to-left sentence."""
    if not isinstance(translated, str):
        return None
    if Counter(_SPAN.findall(translated)) != Counter(tokens):
        return None
    parts, pos = [], 0
    for m in _SPAN.finditer(translated):
        parts.append(_free_text(translated[pos:m.start()]))
        parts.append(_isolate(m.group(1), lang))
        pos = m.end()
    parts.append(_free_text(translated[pos:]))
    if any(part is None for part in parts):
        return None
    return html.unescape("".join(parts)).strip()


_FSI, _PDI = "\u2068", "\u2069"
_LTR = re.compile(r"[A-Za-z0-9\u00c0-\u024f]")


def _isolate(token, lang):
    """A protected part as it goes back into the sentence (still escaped)."""
    if lang in RTL and _LTR.search(token):
        return _FSI + token + _PDI
    return token


def _free_text(segment):
    """A translated stretch between protected parts: None with stray markup;
    long dashes become a comma or a hyphen (house style)."""
    if re.search(r"</?span\b|translate\s*=", segment, re.IGNORECASE):
        return None
    return _DASHES.sub(lambda m: ", " if m.group(0) != m.group(0).strip() else "-", segment)


# --- glossary --------------------------------------------------------------------------------

GLOSSARY_FILE = Path(__file__).with_name("site_translate_glossary.json")
_PLACEHOLDER = re.compile(r"\{(\d+)\}")
_SPACES = re.compile(r"\s+")


def _norm(text):
    return _SPACES.sub(" ", text).strip()


def load_glossary(path=GLOSSARY_FILE):
    """{swedish: {lang: text}} from the reviewed glossary file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    columns = data["columns"]
    return {_norm(row[0]): dict(zip(columns[1:], row[1:])) for row in data["entries"]}


GLOSSARY = load_glossary()


def template(marked):
    """The protected text with each protected part as {0}, {1}, ... (in
    order), unescaped: the glossary key for texts with numbers or names.
    None when the free text has a brace of its own."""
    parts, pos, n = [], 0, 0
    for m in _SPAN.finditer(marked):
        free = html.unescape(marked[pos:m.start()])
        if "{" in free or "}" in free:
            return None
        parts.append(free)
        parts.append("{%d}" % n)
        n += 1
        pos = m.end()
    free = html.unescape(marked[pos:])
    if "{" in free or "}" in free:
        return None
    parts.append(free)
    return _norm("".join(parts))


def _fill(text, tokens, lang):
    return _PLACEHOLDER.sub(lambda m: html.unescape(_isolate(tokens[int(m.group(1))], lang)), text)


def glossary(lang, core, marked, tokens, entries=None):
    """The reviewed translation of the whole text, or None. Tried in order:
    the text exactly, the text with its protected parts as placeholders,
    then both without a trailing colon (the colon is put back), then without
    a trailing full stop (put back), then "Label: rest" with both halves in
    the glossary ("Bokningstid: 2 tim, kan inte förlängas.")."""
    entries = GLOSSARY if entries is None else entries
    found = _whole(lang, core, marked, tokens, entries)
    if found is not None:
        return found
    keys = [(_norm(core), None), (template(marked), tokens)]
    for key, toks in keys:
        if not key:
            continue
        stop = key.endswith(".") and not key.endswith("..")
        body = key[:-1].rstrip() if stop else key
        found = _keyed(lang, body, toks, entries) if stop else None
        if found is None:
            found = _labelled(lang, body, toks, entries)
        if found is not None:
            return found + "." if stop else found
    return None


def _keyed(lang, key, toks, entries):
    """One glossary key (exact, or as a template with toks), or None."""
    entry = entries.get(key)
    if not entry or not entry.get(lang):
        return None
    text = entry[lang]
    if toks is None:
        return None if _PLACEHOLDER.search(text) else text
    used = sorted(int(x) for x in _PLACEHOLDER.findall(text))
    if used != list(range(len(toks))):
        return None
    return _fill(text, toks, lang)


def _labelled(lang, key, toks, entries):
    """ "Label: rest" where the label (no placeholders) and the rest are both
    glossary entries."""
    head, sep, tail = key.partition(": ")
    if not sep or not tail or "{" in head:
        return None
    label = _keyed(lang, head + ":", None, entries)
    if label is None:
        return None
    rest = _keyed(lang, tail, toks, entries)
    return None if rest is None else label + " " + rest


def _whole(lang, core, marked, tokens, entries):
    """The text exactly or as a template, with or without a trailing colon."""
    keys = [(_norm(core), None), (template(marked), tokens)]
    for key, toks in list(keys):
        if key and key.endswith(":") and len(key) > 1:
            keys.append((key[:-1].rstrip(), toks))
    for i, (key, toks) in enumerate(keys):
        entry = entries.get(key) if key else None
        if not entry or not entry.get(lang):
            continue
        text = entry[lang]
        if toks is None:
            if _PLACEHOLDER.search(text):
                continue
        else:
            used = sorted(int(x) for x in _PLACEHOLDER.findall(text))
            if used != list(range(len(toks))):
                continue
            text = _fill(text, toks, lang)
        colon = i >= 2
        return text + ":" if colon else text
    return None


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

def translate_one(marked, lang, deadline=None):
    """The raw translation of one marked text, or None (logged by code).
    ``deadline`` (time.monotonic()): no attempt starts after it."""
    kwargs = {"Text": marked, "SourceLanguageCode": SOURCE, "TargetLanguageCode": lang}
    name = terminology()[0]
    if name:
        # Restaurant words in whole sentences (bord, platser, sällskap, ...):
        # without it "bord" came back as a spreadsheet table, "platser" as web sites.
        kwargs["TerminologyNames"] = [name]
    if lang in FORMALITY:
        kwargs["Settings"] = {"Formality": FORMALITY[lang]}
    for attempt in range(ATTEMPTS):
        if deadline is not None and time.monotonic() >= deadline:
            logger.warning(json.dumps({"translate": "failed", "code": "deadline"}))
            return None
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


def translate_many(items, lang, deadline=None):
    """{key: raw translation or None} for [(key, marked)], 8 at a time.
    ``deadline`` defaults to DEADLINE_S from now."""
    if not items:
        return {}
    if deadline is None:
        deadline = time.monotonic() + DEADLINE_S
    if len(items) == 1:
        key, marked = items[0]
        return {key: translate_one(marked, lang, deadline)}
    with ThreadPoolExecutor(max_workers=min(THREADS, len(items))) as pool:
        results = pool.map(lambda item: translate_one(item[1], lang, deadline), items)
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
