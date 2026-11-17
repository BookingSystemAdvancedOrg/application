"""Staff permissions per admin area (owner request 2026-11-16).

Every page of Booqy Admin that a restaurant works in is an *area*. The owner
decides which areas staff may open:

* group defaults: the areas every staff user of the restaurant gets
  (`staffAccess.defaults` on the tenant PROFILE row);
* per person: extra areas on top of the defaults (`add`) and defaults taken
  away from that one person (`remove`), in `staffAccess.users[<sub>]`.

    staff areas = (defaults | add) - remove

The owner (owner_user) always has every area and can't lose it. Booqy's own
operators never sign in to the tenant pool, so they are never listed here.

The data lives on the tenant row on purpose: every JWT Lambda already reads
that row (shared/tenant.py, cached per container), so enforcing an area costs
no extra read and no new IAM permission, and a change reaches every Lambda
within the tenant cache time (a refusal re-reads the row first, so a fresh
grant works at once).

An area opens the page and the work staff do there. Actions that were the
owner's before (refunds, amounts, settings inside a page, exports, roles)
stay the owner's: an area never makes staff an owner. The pages that were
owner only (Personal, Presentkort, Marknadsföring) open their own
work to granted staff, except who may do what (roles, permissions, deleting
accounts), which is the owner's alone.
"""

from __future__ import annotations

from typing import Optional

# In the admin's menu order (admin-front-end src/shared/nav.ts).
AREAS = (
    "statistik",
    "bokningar",
    "bestallningar",
    "catering",
    "personalliggare",
    "telefon",
    "meny",
    "layout",
    "personal",
    "gaster",
    "presentkort",
    "evenemang",
    "marknadsforing",
)

# Areas of removed pages. Stored grants with them are dropped when read
# (_clean keeps AREAS only), and an older admin that still sends one gets it
# accepted and dropped instead of a 400.
RETIRED_AREAS = ("webbplats",)  # the site builder, removed 2026-11-17

# What staff could open before permissions existed: a restaurant whose owner
# has never saved a choice keeps working exactly as before.
LEGACY_DEFAULTS = (
    "statistik",
    "bokningar",
    "bestallningar",
    "catering",
    "personalliggare",
    "telefon",
    "meny",
    "layout",
    "gaster",
    "evenemang",
)

# A tenant row holds at most this many per-person entries (a row is 400 KB;
# one entry is well under 1 KB).
MAX_USERS = 500
_MAX_SUB = 128

ERROR = "no_access"


def _clean(values) -> list:
    if not isinstance(values, (list, tuple, set)):
        return []
    seen = {v for v in values if isinstance(v, str)}
    return [a for a in AREAS if a in seen]


def _config(tenant_row: Optional[dict]) -> dict:
    raw = (tenant_row or {}).get("staffAccess")
    return raw if isinstance(raw, dict) else {}


def defaults(tenant_row: Optional[dict]) -> list:
    """The staff group's areas. Never saved: what staff had before, minus
    Statistik when the owner had turned that off (`insights.staffAccess`)."""
    config = _config(tenant_row)
    if isinstance(config.get("defaults"), (list, tuple, set)):
        return _clean(config["defaults"])
    out = list(LEGACY_DEFAULTS)
    if ((tenant_row or {}).get("insights") or {}).get("staffAccess") == "none":
        out.remove("statistik")
    return out


def person(tenant_row: Optional[dict], sub: Optional[str]) -> dict:
    """One staff user's own changes: {"add": [...], "remove": [...]}."""
    users = _config(tenant_row).get("users")
    entry = users.get(sub) if isinstance(users, dict) and isinstance(sub, str) else None
    entry = entry if isinstance(entry, dict) else {}
    return {"add": _clean(entry.get("add")), "remove": _clean(entry.get("remove"))}


AGENT_SUB_PREFIX = "agent:"            # = tenant.SYSTEM_SUB_PREFIX (tenant imports this module)
AGENT_AREAS = ("bokningar", "layout")


def areas_for(tenant_row: Optional[dict], role: Optional[str], sub: Optional[str]) -> list:
    """Every area the caller may open, in menu order."""
    if role == "owner_user":
        return list(AREAS)
    if role != "staff_user":
        return []
    if isinstance(sub, str) and sub.startswith(AGENT_SUB_PREFIX):
        # The phone agent's system principal (tenant.SYSTEM_SUB_PREFIX, never a
        # Cognito login) books and reads bookings whatever the staff defaults say.
        return [a for a in AREAS if a in AGENT_AREAS]
    own = person(tenant_row, sub)
    allowed = (set(defaults(tenant_row)) | set(own["add"])) - set(own["remove"])
    return [a for a in AREAS if a in allowed]


def allows(tenant_row: Optional[dict], role: Optional[str], sub: Optional[str], area) -> bool:
    """May the caller open `area`? A tuple means any of them."""
    wanted = area if isinstance(area, (tuple, list, set)) else (area,)
    unknown = [a for a in wanted if a not in AREAS]
    if unknown:  # a typo must not open or close anything silently
        raise KeyError(f"unknown areas {unknown}")
    if role == "owner_user":
        return True
    have = set(areas_for(tenant_row, role, sub))
    return any(a in have for a in wanted)


def owner_view(tenant_row: Optional[dict]) -> dict:
    """What the owner edits: the group defaults and every person's changes."""
    config = _config(tenant_row)
    users = config.get("users") if isinstance(config.get("users"), dict) else {}
    return {
        "areas": list(AREAS),
        "defaults": defaults(tenant_row),
        "users": {
            sub: person(tenant_row, sub)
            for sub in sorted(users)
            if isinstance(sub, str)
        },
        "version": int(config.get("version") or 0),
    }


# --- owner edits (PATCH /tenant {"staffAccess": ...}) ------------------------------


def _areas_value(value, name: str) -> list:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{name} must be a list of areas")
    unknown = sorted(set(value) - set(AREAS) - set(RETIRED_AREAS))
    if unknown:
        raise ValueError(f"{name} has unknown areas: {', '.join(unknown)}")
    return _clean(value)


def apply_edit(tenant_row: Optional[dict], body, now: str, by: str) -> dict:
    """The new `staffAccess` value after an owner edit.

    body: {"version": n, "defaults"?: [...], "users"?: {sub: {"add": [...],
    "remove": [...]} | null}}. `version` must match the stored one (two
    owners editing at once get 409 changed_retry). A person entry replaces
    that person's changes; null or empty lists clear them. An area both
    added and removed for one person is refused.
    """
    if not isinstance(body, dict):
        raise ValueError("staffAccess must be an object")
    unknown = sorted(set(body) - {"version", "defaults", "users"})
    if unknown:
        raise ValueError(f"unsupported staffAccess fields: {', '.join(unknown)}")
    version = body.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("staffAccess.version is required")
    current = owner_view(tenant_row)
    if version != current["version"]:
        raise Conflict()

    new_defaults = current["defaults"]
    if "defaults" in body:
        new_defaults = _areas_value(body["defaults"], "staffAccess.defaults")

    users = {sub: dict(entry) for sub, entry in current["users"].items()}
    if "users" in body:
        changes = body["users"]
        if not isinstance(changes, dict):
            raise ValueError("staffAccess.users must be an object")
        for sub, entry in changes.items():
            if not isinstance(sub, str) or not sub.strip() or len(sub) > _MAX_SUB or "#" in sub:
                raise ValueError("staffAccess.users has an invalid user id")
            if entry is None:
                users.pop(sub, None)
                continue
            if not isinstance(entry, dict) or set(entry) - {"add", "remove"}:
                raise ValueError("staffAccess.users entries take add and remove")
            add = _areas_value(entry.get("add", []), "add")
            remove = _areas_value(entry.get("remove", []), "remove")
            if set(add) & set(remove):
                raise ValueError("an area can't be both added and removed")
            if add or remove:
                users[sub] = {"add": add, "remove": remove}
            else:
                users.pop(sub, None)
    if len(users) > MAX_USERS:
        raise ValueError("too many staff users")

    # Store only what changes access: an extra that is already a default
    # and a removal of something that isn't one are dropped.
    base = set(new_defaults)
    tidy = {}
    for sub, entry in users.items():
        add = [a for a in entry["add"] if a not in base]
        remove = [a for a in entry["remove"] if a in base]
        if add or remove:
            tidy[sub] = {"add": add, "remove": remove}

    return {
        "defaults": new_defaults,
        "users": tidy,
        "version": current["version"] + 1,
        "updatedAt": now,
        "updatedBy": by,
    }


class Conflict(Exception):
    """The stored permissions changed since the owner loaded them."""
