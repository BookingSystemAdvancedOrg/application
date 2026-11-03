"""Site builder (feature B1, ADR-B): the site document, its validation, and
the draft -> immutable version -> published pointer model.

Table `<prefix>site-builder` (SITE_BUILDER_TABLE_NAME), partition `TENANT#<t>`:

    SITE                 mode (builder | custom), version, running (publishId while one runs)
    DRAFT                doc, schemaVersion, basedOn, version, updatedAt, updatedBy
    VERSION#<n:06d>      doc, source, prefix v/<n>/, pages[], contrast[] (written once, never updated)
    PUBLISHED            versionNo, previousNo, publishId, publishedAt, version
    PUBLISH#<publishId>  status, kind, versionNo?, doc (snapshot), error?, ttl +90 d
    PREVIEW#<sha256>     createdBy, expiresAt, ttl +7 d

S3 (sites bucket, SITES_BUCKET_NAME): the render step writes
`sites/<t>/v/<n>/<page>/index.html` + `site.json`; Promote copies each page
to `sites/<t>/<page>/index.html`. Every key is built from the tenant id the
caller resolved from the token (or the workflow input that the API wrote),
never from the document.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import dynamo

FEATURE_ID = "site-builder"
FEATURE_KEY = "siteBuilder"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "site_schema" / "v1.json"
MAX_DOC_BYTES = 150 * 1024
PREVIEW_DAYS = 7
PUBLISH_TTL_DAYS = 90
MIN_CONTRAST = 4.5

_CLIENT_ID = re.compile(r"[0-9A-Za-z_-]{8,64}")

# First path segments the customer app (customer-front-end src/App.tsx) routes
# itself, plus the S3 prefixes under sites/<t>/ and the built assets folder.
# A builder page with one of these slugs would never be shown, so it is refused.
RESERVED_SLUGS = frozenset({
    "meny", "boka-bord", "bokningsvillkor", "bokning", "bestall", "bestallning", "catering", "om-oss",
    "kontakt", "integritetspolicy", "cookies", "presentkort", "ko", "forhandsvisning", "evenemang",
    "biljett", "assets", "v", "releases", "preview",
})


class SiteError(Exception):
    def __init__(self, status: int, code: str, field: Optional[str] = None):
        super().__init__(code)
        self.status, self.code, self.field = status, code, field


# --- basics ---------------------------------------------------------------------------

def table():
    return dynamo.table(os.environ["SITE_BUILDER_TABLE_NAME"])


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def pk(tenant_id):
    return f"TENANT#{tenant_id}"


def key(tenant_id, sk):
    return {"PK": pk(tenant_id), "SK": sk}


def version_sk(n: int) -> str:
    return f"VERSION#{int(n):06d}"


def valid_client_id(value) -> bool:
    return isinstance(value, str) and bool(_CLIENT_ID.fullmatch(value))


def conditional_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def plain(value):
    from decimal import Decimal
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def get(tenant_id, sk) -> Optional[dict]:
    return table().get_item(Key=key(tenant_id, sk), ConsistentRead=True).get("Item")


def site_prefix(tenant_id) -> str:
    return f"sites/{tenant_id}/"


def page_path(page: dict) -> str:
    """'' for the start page, else '<slug>/' (S3 path part)."""
    return f"{page['slug']}/" if page.get("slug") else ""


# --- the document ---------------------------------------------------------------------

_validator = None


def _schema_validator():
    global _validator
    if _validator is None:
        from jsonschema import Draft202012Validator
        _validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    return _validator


def _pointer(path) -> str:
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in path) if path else ""


def starter_doc(tenant_row: dict) -> dict:
    """The first draft: the restaurant's name and colours, a start page with
    the live blocks. Nothing is published until the owner presses Publicera."""
    name = (tenant_row.get("name") or "Vår restaurang").strip()[:80]
    branding = tenant_row.get("branding") or {}
    color = branding.get("primaryColor") if re.fullmatch(r"#[0-9a-fA-F]{6}", branding.get("primaryColor") or "") \
        else "#350001"
    return {
        "schemaVersion": 1,
        "theme": {"primaryColor": color, "accentColor": color, "font": "serif"},
        "nav": [{"pageId": "start"}],
        "pages": [{"id": "start", "slug": "", "title": name,
                   "seo": {"title": name[:70], "description": f"Välkommen till {name}."[:160]},
                   "blocks": [{"id": "hero", "type": "hero", "heading": name},
                              {"id": "boka", "type": "bookingCta"},
                              {"id": "meny", "type": "menu"},
                              {"id": "tider", "type": "openingHours"},
                              {"id": "kontakt", "type": "contact"}]}],
        "footer": {"text": name},
    }


def _links(value):
    """Every link and image key in a block, with its JSON pointer suffix."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("link", "image") and isinstance(v, str):
                yield [k], k, v
            else:
                for path, kind, item in _links(v):
                    yield [k, *path], kind, item
    elif isinstance(value, list):
        for i, v in enumerate(value):
            for path, kind, item in _links(v):
                yield [i, *path], kind, item


def validate_doc(doc, tenant_id: str, location_ids) -> None:
    """Schema, size, ids, own-page links, own images, own locations.
    Raises SiteError(400, code, field=<JSON pointer>)."""
    if not isinstance(doc, dict):
        raise SiteError(400, "schema_invalid", "")
    if len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_DOC_BYTES:
        raise SiteError(400, "doc_too_large", "")
    errors = sorted(_schema_validator().iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path])
    if errors:
        best = errors[0]
        while best.context:  # oneOf over block types: the branch that got furthest
            best = max(best.context, key=lambda e: len(e.absolute_path))
        path = list(best.absolute_path)
        if path and path[-1] == "type" and best.validator == "const":  # no block type matched: the block
            path = path[:-1]
        raise SiteError(400, "schema_invalid", _pointer(path))
    page_ids = [p["id"] for p in doc["pages"]]
    slugs = [p["slug"] for p in doc["pages"]]
    for i, page_id in enumerate(page_ids):
        if page_ids.index(page_id) != i:
            raise SiteError(400, "schema_invalid", f"/pages/{i}/id")
        if slugs.index(slugs[i]) != i or slugs[i] in RESERVED_SLUGS:
            raise SiteError(400, "schema_invalid", f"/pages/{i}/slug")
    if "" not in slugs:
        raise SiteError(400, "schema_invalid", "/pages")
    for i, entry in enumerate(doc["nav"]):
        if entry["pageId"] not in page_ids:
            raise SiteError(400, "invalid_link", f"/nav/{i}/pageId")
    image_prefix = f"menu-images/{tenant_id}/"
    for p, page in enumerate(doc["pages"]):
        block_ids = [b["id"] for b in page["blocks"]]
        for b, block in enumerate(page["blocks"]):
            base = f"/pages/{p}/blocks/{b}"
            if block_ids.index(block["id"]) != b:
                raise SiteError(400, "schema_invalid", f"{base}/id")
            if block.get("locationId") is not None and block["locationId"] not in location_ids:
                raise SiteError(400, "schema_invalid", f"{base}/locationId")
            for path, kind, value in _links(block):
                pointer = base + _pointer(path)
                if kind == "link":
                    if value.startswith("page:") and value[5:] not in page_ids:
                        raise SiteError(400, "invalid_link", pointer)
                elif not value.startswith(image_prefix) or ".." in value:
                    raise SiteError(400, "unknown_image", pointer)


# --- contrast (same algorithm and constants as site-kit accentPair) --------------------

WHITE, NEAR_BLACK, TICKET_DARK = (255, 255, 255), (29, 26, 26), (42, 37, 35)


def parse_hex(value: str):
    m = re.fullmatch(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", (value or "").strip())
    if not m:
        return None
    full = m.group(1) if len(m.group(1)) == 6 else "".join(c * 2 for c in m.group(1))
    return tuple(int(full[i:i + 2], 16) for i in (0, 2, 4))


def luminance(rgb) -> float:
    def channel(v):
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def to_hex(rgb) -> str:
    # JS Math.round rounds .5 up; Python round() is banker's rounding.
    return "#" + "".join(f"{int(v + 0.5):02x}" for v in rgb)


def mix(rgb, to, t):
    return tuple(v + (to[i] - v) * t for i, v in enumerate(rgb))


def text_contrast(rgb) -> float:
    return max(contrast(rgb, WHITE), contrast(rgb, NEAR_BLACK))


def text_on(value: str) -> str:
    rgb = parse_hex(value)
    if not rgb:
        return "#ffffff"
    return "#ffffff" if contrast(rgb, WHITE) >= contrast(rgb, NEAR_BLACK) else "#1d1a1a"


def _steps(start):
    """0.05, 0.10, ... like the JS loop `for (t = start; t <= 1.0001; t += 0.05)`."""
    t = start
    while t <= 1.0001:
        yield t
        t += 0.05


def accent_pair(value: str, mode: str) -> dict:
    rgb = parse_hex(value)
    if not rgb:
        return {"accent": value, "onAccent": "#ffffff"}
    best = rgb
    if mode == "light":
        if text_contrast(rgb) < 4.5:
            for t in _steps(0.05):
                darker, lighter = mix(rgb, NEAR_BLACK, t), mix(rgb, WHITE, t)
                if text_contrast(darker) >= 4.5:
                    best = darker
                    break
                if text_contrast(lighter) >= 4.5:
                    best = lighter
                    break
    else:
        best = WHITE
        for t in _steps(0):
            mixed = mix(rgb, WHITE, t)
            if contrast(mixed, TICKET_DARK) >= 3 and text_contrast(mixed) >= 4.5:
                best = mixed
                break
    accent = to_hex(best)
    return {"accent": accent, "onAccent": text_on(accent)}


def contrast_report(doc: dict) -> list:
    """Each theme colour, nudged per mode like site-kit, with the contrast of
    its text. Raises SiteError(400, contrast_too_low) for a pair under 4.5:1."""
    out = []
    for field in ("primaryColor", "accentColor"):
        for mode in ("light", "dark"):
            pair = accent_pair(doc["theme"][field], mode)
            ratio = contrast(parse_hex(pair["accent"]), parse_hex(pair["onAccent"]))
            out.append({"field": f"/theme/{field}", "mode": mode, "accent": pair["accent"],
                        "onAccent": pair["onAccent"], "ratio": Decimal(str(round(ratio, 2)))})
            if ratio < MIN_CONTRAST:
                raise SiteError(400, "contrast_too_low", f"/theme/{field}")
    return out


# --- preview tokens -------------------------------------------------------------------

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def preview_expiry(at: datetime) -> datetime:
    return at + timedelta(days=PREVIEW_DAYS)


# --- versions and the pointer ---------------------------------------------------------

def versions(tenant_id) -> list:
    tbl, items, kwargs = table(), [], {
        "KeyConditionExpression": Key("PK").eq(pk(tenant_id)) & Key("SK").begins_with("VERSION#"),
        "ConsistentRead": True, "ScanIndexForward": False,
        "ProjectionExpression": "SK, versionNo, #s, publishedAt, publishedBy, pages",
        "ExpressionAttributeNames": {"#s": "source"}}
    while True:
        page = tbl.query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def next_version_no(tenant_id) -> int:
    page = table().query(KeyConditionExpression=Key("PK").eq(pk(tenant_id)) & Key("SK").begins_with("VERSION#"),
                         ScanIndexForward=False, Limit=1, ConsistentRead=True, ProjectionExpression="SK")
    items = page.get("Items") or []
    return (int(items[0]["SK"].split("#", 1)[1]) + 1) if items else 1


def doc_copy(doc) -> dict:
    return copy.deepcopy(plain(doc))
