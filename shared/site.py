"""Where a tenant's public site lives - for links the platform sends guests
to (Stripe's return pages, order status links).

The tenant's active domain (its primary domain first); without one, outside
prod, CUSTOMER_SITE_URL with ?restaurang=<slug> (dev sites on
*.cloudfront.net can't tell the restaurant from their host). In prod
without a domain there is no site: (None, None).

ENV_VARS:
    TENANT_TABLE_NAME, ENVIRONMENT, CUSTOMER_SITE_URL (optional)
"""

import os
from urllib.parse import urlparse

from boto3.dynamodb.conditions import Key

from shared import dynamo, tenant


def base(tenant_id, tenant_row):
    """(base_url, slug) - slug only when the site needs ?restaurang=."""
    rows = dynamo.table(os.environ["TENANT_TABLE_NAME"]).query(
        KeyConditionExpression=Key("PK").eq(tenant.tenant_pk(tenant_id))
        & Key("SK").begins_with("DOMAIN#")
    ).get("Items") or []
    active = sorted(
        (r for r in rows if r.get("status") == "active" and isinstance(r.get("domain"), str)),
        key=lambda r: (r["domain"] != tenant_row.get("primaryDomain"), r["domain"]),
    )
    if active:
        return f"https://{active[0]['domain']}", None
    url = os.environ.get("CUSTOMER_SITE_URL")
    if url and os.environ.get("ENVIRONMENT") != "prod" and tenant_row.get("slug"):
        return url.rstrip("/"), tenant_row["slug"]
    return None, None


def hosts(tenant_id, tenant_row):
    """Every host the tenant's site is served on: its active domains, or
    outside prod the shared dev site's host."""
    rows = dynamo.table(os.environ["TENANT_TABLE_NAME"]).query(
        KeyConditionExpression=Key("PK").eq(tenant.tenant_pk(tenant_id))
        & Key("SK").begins_with("DOMAIN#")
    ).get("Items") or []
    found = sorted(r["domain"] for r in rows if r.get("status") == "active" and isinstance(r.get("domain"), str))
    if found:
        return found
    url = os.environ.get("CUSTOMER_SITE_URL")
    if url and os.environ.get("ENVIRONMENT") != "prod" and tenant_row.get("slug"):
        host = urlparse(url).hostname
        return [host] if host else []
    return []


def page(base_url, slug, path):
    query = f"?restaurang={slug}" if slug else ""
    return f"{base_url.rstrip('/')}{path}{query}"
