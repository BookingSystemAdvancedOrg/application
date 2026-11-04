"""Organic Facebook and Instagram posting (feature G, ADR-G): shared logic
for the social-posting Lambda.

Table `<prefix>social-posting` (SOCIAL_POSTING_TABLE_NAME):

    TENANT#<t> / POST#<postId>   one post, one or two channels with their own state

No token of its own: the Meta connection is feature A's, read only
(`TENANT#<t>/CONNECTION` in `<prefix>meta-ads` and the secret
`booqy/<env>/meta-ads/<tenantId>`, through shared/features/meta_ads). Meta
calls go through A's port (shared/meta_graph, simulator unless META_CLIENT=real).
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import dynamo

FEATURE_ID = "social-posting"
FEATURE_KEY = "socialPosting"
CHANNELS = ("facebook", "instagram")
STATUSES = ("draft", "scheduled", "publishing", "published", "partly_published", "failed", "cancelled")
EDITABLE = ("draft", "scheduled")
SCHEDULABLE = ("draft", "scheduled", "failed", "partly_published")
SOURCE_KINDS = ("dish", "special", "free")
MAX_CAPTION = 2000
MAX_DRAFTS = 10
MAX_DAYS_AHEAD = 90
LEASE_MINUTES = 5
IMAGE_URL_SECONDS = 15 * 60
DONE_TTL_DAYS = 400
RECENT_POSTS = 25
# What the Meta login must have granted for each channel (ADR-G 2).
PERMISSIONS = {"facebook": ("pages_manage_posts",), "instagram": ("instagram_content_publish",)}

_POST_ID = re.compile(r"[0-9A-Za-z_-]{8,80}")
_ID = re.compile(r"[0-9A-Za-z_-]{1,128}")


class PostError(Exception):
    def __init__(self, status: int, code: str, field: Optional[str] = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.field = field


def table():
    return dynamo.table(os.environ["SOCIAL_POSTING_TABLE_NAME"])


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def conditional_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def pk(tenant_id) -> str:
    return f"TENANT#{tenant_id}"


def post_key(tenant_id, post_id) -> dict:
    return {"PK": pk(tenant_id), "SK": f"POST#{post_id}"}


def valid_post_id(value) -> bool:
    return isinstance(value, str) and bool(_POST_ID.fullmatch(value))


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def schedule_name(post_id) -> str:
    name = f"post-{post_id}"
    return name if len(name) <= 64 else f"post-{hashlib.sha256(post_id.encode()).hexdigest()[:40]}"


def image_prefix(tenant_id, location_id) -> str:
    return f"menu-images/{tenant_id}/locations/{location_id}/"


def get_post(tenant_id, post_id) -> Optional[dict]:
    if not valid_post_id(post_id):
        return None
    item = table().get_item(Key=post_key(tenant_id, post_id), ConsistentRead=True).get("Item")
    return item if item and item.get("tenantId") == tenant_id else None


def posts(tenant_id) -> list:
    kwargs = {"KeyConditionExpression": Key("PK").eq(pk(tenant_id)) & Key("SK").begins_with("POST#"),
              "ScanIndexForward": False}
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def view(item) -> dict:
    out = {k: plain(item[k]) for k in ("postId", "locationId", "source", "caption", "imageKey", "status",
                                        "scheduledAt", "alcoholConfirmed", "createdAt", "updatedAt", "version")
           if item.get(k) is not None}
    out["channels"] = {ch: {k: plain(v) for k, v in state.items() if k not in ("leaseUntil",)}
                       for ch, state in (item.get("channels") or {}).items()}
    return out


def format_price(price) -> str:
    try:
        value = Decimal(str(price))
    except Exception:
        return ""
    if value == value.to_integral_value():
        return f"{int(value)} kr"
    return f"{value:.2f}".replace(".", ",") + " kr"


def template_caption(item) -> str:
    """Swedish draft from a menu item (ADR-G 7): name, description, full price incl. VAT."""
    parts = [str(item.get("name") or "").strip()]
    if item.get("description"):
        parts.append(str(item["description"]).strip())
    price = format_price(item.get("price")) if item.get("price") is not None else ""
    if price:
        parts.append(price)
    return "\n\n".join(p for p in parts if p)[:MAX_CAPTION]


def validate_caption(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PostError(400, "caption is required", "caption")
    if len(value) > MAX_CAPTION:
        raise PostError(400, "caption_too_long", "caption")
    return value.strip()


def validate_channels(value) -> list:
    if not isinstance(value, list) or not value or any(c not in CHANNELS for c in value) \
            or len(set(value)) != len(value):
        raise PostError(400, "invalid_channels", "channels")
    return [c for c in CHANNELS if c in value]


def validate_image(tenant_id, location_id, value) -> str:
    if not isinstance(value, str) or not value.startswith(image_prefix(tenant_id, location_id)) \
            or ".." in value or len(value) > 512:
        raise PostError(400, "image_not_allowed", "imageKey")
    return value


def new_channels(channels) -> dict:
    return {ch: {"state": "queued", "attempts": 0} for ch in channels}


def missing_permissions(connection, channels=CHANNELS) -> list:
    granted = set((connection or {}).get("scopes") or [])
    out = []
    for ch in channels:
        if any(p not in granted for p in PERMISSIONS[ch]):
            out.append(ch)
        elif ch == "instagram" and not (connection or {}).get("instagramId"):
            out.append(ch)
    return out


def overall(channels: dict) -> str:
    states = [c.get("state") for c in channels.values()]
    if states and all(s == "published" for s in states):
        return "published"
    if any(s == "published" for s in states):
        return "partly_published"
    return "failed"


def done_ttl(at: datetime) -> int:
    return int((at + timedelta(days=DONE_TTL_DAYS)).timestamp())
