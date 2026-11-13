"""Where a tenant's public site lives - for every link the platform sends
guests to (booking, order and catering links, Scrive and Stripe return
pages, QR codes, marketing mail).

In order:
  1. the restaurant's own domain once it is attached and its certificate
     issued (an active DOMAIN# row that is not kind platform; primaryDomain
     first), so links switch to it as soon as domain-attach finishes;
  2. its slug address <slug>.<platform domain> (<slug>.booqy.se in prod,
     <slug>.dev.booqy.se in dev): the active kind-platform row onboarding
     writes in the same step that creates the slug's CloudFront
     distribution tenant. Never built from the slug alone: a tenant set up
     before that row existed has no distribution tenant, so the host fails
     the TLS handshake and every link to it leads nowhere;
  3. outside prod, CUSTOMER_SITE_URL with ?restaurang=<slug> (dev sites on
     *.cloudfront.net can't tell the restaurant from their host).
Otherwise there is no site: (None, None).

ENV_VARS:
    TENANT_TABLE_NAME, ENVIRONMENT, CUSTOMER_SITE_URL (optional)
"""

import os
from urllib.parse import urlparse

from boto3.dynamodb.conditions import Key

from shared import dynamo, tenant

def _active(tenant_id):
    rows = dynamo.table(os.environ["TENANT_TABLE_NAME"]).query(
        KeyConditionExpression=Key("PK").eq(tenant.tenant_pk(tenant_id))
        & Key("SK").begins_with("DOMAIN#")
    ).get("Items") or []
    return [r for r in rows if r.get("status") == "active" and isinstance(r.get("domain"), str)]


def _own(active, tenant_row):
    return sorted((r["domain"] for r in active if r.get("kind") != "platform"),
                  key=lambda d: (d != tenant_row.get("primaryDomain"), d))


def _slug_host(active):
    return next(iter(sorted(r["domain"] for r in active if r.get("kind") == "platform")), None)


def _dev_site(tenant_row):
    url = os.environ.get("CUSTOMER_SITE_URL")
    if url and os.environ.get("ENVIRONMENT") != "prod" and tenant_row.get("slug"):
        return url.rstrip("/")
    return None


def base(tenant_id, tenant_row):
    """(base_url, slug) - slug only when the site needs ?restaurang=."""
    active = _active(tenant_id)
    host = next(iter(_own(active, tenant_row)), None) or _slug_host(active)
    if host:
        return f"https://{host}", None
    url = _dev_site(tenant_row)
    if url:
        return url, tenant_row["slug"]
    return None, None


def hosts(tenant_id, tenant_row):
    """Every host the tenant's site is served on: its own domains and its
    slug address, or outside prod the shared dev site's host."""
    active = _active(tenant_id)
    found = set(_own(active, tenant_row)) | {r["domain"] for r in active if r.get("kind") == "platform"}
    if found:
        return sorted(found)
    url = _dev_site(tenant_row)
    host = urlparse(url).hostname if url else None
    return [host] if host else []


def page(base_url, slug, path):
    query = f"?restaurang={slug}" if slug else ""
    return f"{base_url.rstrip('/')}{path}{query}"
