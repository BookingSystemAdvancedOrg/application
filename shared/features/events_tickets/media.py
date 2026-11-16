"""Event images (ADR-K 1): the event's picture reuses the menu image upload.

The owner uploads through the existing `GET /menu-images/presigned-url`
(pre-signed-url Lambda: owner only, location checked against the token's
tenant, AVIF/JPEG/PNG/WebP only, 5-minute PUT URL) and sends the returned
`imageKey` with K2/K4. So there is no new bucket, CloudFront path or IAM: this
Lambda never touches S3. It only checks that the key has the exact shape
pre-signed-url hands out for THIS tenant and THIS location (the same rule as
manage-menu, site-builder and meta-ads), so a tenant can never show another
tenant's picture. Guests get the key (as for menu dishes) and build the URL
from the site's image CDN base. Alt text is required with an image.
"""

from __future__ import annotations

import re
from typing import Optional

from . import EventsError

ALT_MAX = 200
# pre-signed-url: menu-images/<tenantId>/locations/<locationId>/menu/<uuid4>.<ext>
_KEY = re.compile(r"menu-images/(?P<tenant>[^/]+)/locations/(?P<location>[^/]+)/menu/"
                  r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(?:avif|jpg|png|webp)")


def clean_image(body: dict, tenant_id: str, location_id: str) -> Optional[dict]:
    """{imageKey, imageAlt} from a K2/K4 body, or None for "no image".

    A key that is not this location's upload is `400 unknown_image` whether or
    not it exists (nothing about other tenants leaks); a missing or too long
    alt text with an image is a 400 on `imageAlt`."""
    key = body.get("imageKey")
    if key in (None, ""):
        return None
    match = _KEY.fullmatch(key) if isinstance(key, str) else None
    if not match or match["tenant"] != tenant_id or match["location"] != location_id:
        raise EventsError(400, "unknown_image", "imageKey")
    alt = body.get("imageAlt")
    if not isinstance(alt, str) or not alt.strip() or len(alt.strip()) > ALT_MAX:
        raise EventsError(400, "imageAlt is required with an image (at most 200 characters)", "imageAlt")
    return {"imageKey": key, "imageAlt": alt.strip()}


def image_view(ev: dict) -> dict:
    """The image fields of an event row for any view (owner, staff or guest)."""
    if not ev.get("imageKey"):
        return {}
    return {"imageKey": ev["imageKey"], "imageAlt": ev.get("imageAlt") or ""}
