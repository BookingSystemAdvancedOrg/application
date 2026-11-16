"""Allmänna villkor för restauranger: Booqy's terms for the restaurant as a
customer (price and payment in advance, card or invoice, late payment,
notice). The owner accepts the current version in admin next to the DPA
(tenant-account, POST /tenant/terms/accept). Like shared/dpa.py, every
acceptance is its own append-only row in its own partition, so IAM can
allow PutItem on TERMS#* keys only:

    PK TERMS#<tenantId>   SK <version>#<acceptedAt>
    tenantId, version, acceptedAt, acceptedBy {sub, email, name}

and the PROFILE gets a summary `terms` {version, acceptedAt,
acceptedBySub}. Accepting is never a gate for the live site: the admin
shows a reminder until the current version is accepted.

The text lives in admin (src/features/dpa/termsText.ts) and on booqy.se
(site/villkor.html, "För restauranger"). Bump CURRENT_VERSION together.
"""

CURRENT_VERSION = "2026-11-v1"


def summary(row):
    raw = row.get("terms") if isinstance(row, dict) else None
    return raw if isinstance(raw, dict) else {}


def status(row):
    """What admin needs: GET /tenant `terms`."""
    accepted = summary(row).get("version")
    accepted = accepted if isinstance(accepted, str) and accepted else None
    return {
        "currentVersion": CURRENT_VERSION,
        "acceptedVersion": accepted,
        "acceptedAt": summary(row).get("acceptedAt") if accepted else None,
        "upToDate": accepted == CURRENT_VERSION,
    }


def record_key(tenant_id, accepted_at):
    return {"PK": f"TERMS#{tenant_id}", "SK": f"{CURRENT_VERSION}#{accepted_at}"}
