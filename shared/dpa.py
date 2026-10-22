"""Personuppgiftsbiträdesavtal (DPA) between IT-hjälparna and a restaurant.

The owner accepts the current version in admin (tenant-account,
POST /tenant/dpa/accept). Every acceptance is its own append-only row in the
tenant table, in its own partition so IAM can allow PutItem on DPA#* keys
only (it can never overwrite a PROFILE):

    PK DPA#<tenantId>   SK <version>#<acceptedAt>
    tenantId, version, acceptedAt, acceptedBy {sub, email, name}

and the tenant PROFILE gets a summary `dpa` {version, acceptedAt,
acceptedBySub} so GET /tenant and the public gate need no extra read.

Going live: only tenants created with `dpaRequired: true` (Booqy Console sets
it on new tenants) are gated. Until the owner has accepted any version, their
public routes answer 404 (tenant.for_public). Tenants created before this
existed have no flag and are never gated; they get a reminder in admin. A
newer DPA version never takes a live site offline either - it only shows the
reminder again.

The text itself lives in admin (src/features/dpa/dpaText.ts) and as a draft
in docs/dpa-draft.md. Bump CURRENT_VERSION together with both.
"""

CURRENT_VERSION = "2026-10-v1"


def summary(row):
    """The PROFILE `dpa` map, or {} when nothing is accepted."""
    raw = row.get("dpa") if isinstance(row, dict) else None
    return raw if isinstance(raw, dict) else {}


def accepted_version(row):
    version = summary(row).get("version")
    return version if isinstance(version, str) and version else None


def blocks_public(row):
    """True only for a tenant that must accept before going live and has not
    accepted any version yet. Everything else (no flag, any acceptance) is
    live - this must never take an existing restaurant offline."""
    return isinstance(row, dict) and row.get("dpaRequired") is True and accepted_version(row) is None


def status(row):
    """What admin needs: GET /tenant `dpa`."""
    accepted = accepted_version(row)
    return {
        "currentVersion": CURRENT_VERSION,
        "acceptedVersion": accepted,
        "acceptedAt": summary(row).get("acceptedAt") if accepted else None,
        "required": isinstance(row, dict) and row.get("dpaRequired") is True,
        "upToDate": accepted == CURRENT_VERSION,
    }


def record_key(tenant_id, accepted_at):
    return {"PK": f"DPA#{tenant_id}", "SK": f"{CURRENT_VERSION}#{accepted_at}"}
