"""Kom igång: the one rule for "this restaurant has finished its setup".

Booqy Admin shows the checklist on Översikt, Booqy Console shows the
progress per customer and tells the operators when it is finished. Both read
what functions/tenant-setup computes here, so the browser never decides.

THE RULE (docs/SETUP-CHECKLIST.md):
    A tenant's setup is complete when every applicable step is finished,
    across all of the tenant's locations. A step is finished when it is
    done or skipped:
      - a tenant step (dpa, stripe, staff) once for the tenant;
      - a location step (hours, menu, printers, layout, google, phone) at
        every location of the tenant; a tenant without locations is never
        complete.
    A step applies when the plan has its feature (printers: ordering,
    google: googleBusiness, phone: phoneAgent); the rest always apply.
    Skipped = the owner pressed "Hoppa över" (SETUPSKIP# rows) or used the
    step's own skip ("Inga skrivare än" for printers, "Vi har ingen
    Google-profil" for google). Every step except dpa can be skipped: the
    personuppgiftsbiträdesavtal must be accepted before anything is
    published.
    A step whose state can't be computed counts as open (fail closed).

New steps: add a Step to STEPS. It is then part of the rule, the admin list
and the console count without other changes; tests/test_setup_checklist.py
fails for a step that returns no state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

DONE, SKIPPED, OPEN = "done", "skipped", "open"
STATES = (DONE, SKIPPED, OPEN)
TENANT, LOCATION = "tenant", "location"

# The phone assistant's five sub-steps (admin features/phone-agent/setup.ts).
PHONE_SUBSTEPS = ("namn", "tider", "nummer", "koppla", "prova")


@dataclass
class Facts:
    """Everything the steps look at, read by tenant-setup's loader. Plain
    data, so the rule is tested without AWS."""
    features: dict = field(default_factory=dict)
    dpa_up_to_date: Optional[bool] = None
    stripe_ready: Optional[bool] = None
    staff_count: Optional[int] = None
    phone_named: Optional[bool] = None          # phone config saved once (version > 0)
    google_connected: Optional[bool] = None
    # "<step>" for a tenant step, "<step>@<locationId>" for a location step
    skips: set = field(default_factory=set)
    locations: list = field(default_factory=list)   # list[LocationFacts]


@dataclass
class LocationFacts:
    location_id: str
    name: str = ""
    has_hours: Optional[bool] = None
    active_menu_items: Optional[int] = None
    live_layout: Optional[bool] = None
    printers: Optional[int] = None
    printers_skipped: bool = False
    google_linked: Optional[bool] = None
    google_skipped: bool = False
    phone: Optional[dict] = None                # {"tider": bool, "nummer": bool, "koppla": bool, "prova": bool}


@dataclass(frozen=True)
class Step:
    id: str
    label: str            # Swedish, Booqy Admin
    label_en: str         # English, Booqy Console
    scope: str            # TENANT or LOCATION
    # facts, location (None for a tenant step) -> True done, False not done, None unknown
    check: Callable
    feature: Optional[str] = None
    skippable: bool = True
    # facts, location -> True when the step's own skip is on
    own_skip: Optional[Callable] = None
    # facts, location -> {sub-step id: bool}
    sub: Optional[Callable] = None


def _positive(n) -> Optional[bool]:
    return None if n is None else n > 0


def _phone_sub(f: Facts, loc: LocationFacts) -> Optional[dict]:
    if loc.phone is None or f.phone_named is None:
        return None
    return {"namn": bool(f.phone_named), **{k: bool(loc.phone.get(k)) for k in PHONE_SUBSTEPS[1:]}}


def _phone_done(f: Facts, loc: LocationFacts) -> Optional[bool]:
    sub = _phone_sub(f, loc)
    return None if sub is None else all(sub.values())


def _google_done(f: Facts, loc: LocationFacts) -> Optional[bool]:
    if f.google_connected is None or loc.google_linked is None:
        return None
    return bool(f.google_connected and loc.google_linked)


# The order is the order on Översikt.
STEPS: tuple[Step, ...] = (
    Step("dpa", "Godkänn personuppgiftsbiträdesavtalet", "Accept the data processing agreement", TENANT,
         lambda f, _: f.dpa_up_to_date, skippable=False),
    Step("hours", "Lägg in öppettider", "Opening hours", LOCATION, lambda f, loc: loc.has_hours),
    Step("menu", "Lägg upp menyn", "Menu", LOCATION, lambda f, loc: _positive(loc.active_menu_items)),
    Step("printers", "Kvittoskrivare", "Receipt printers", LOCATION, lambda f, loc: _positive(loc.printers),
         feature="ordering", own_skip=lambda f, loc: loc.printers_skipped),
    Step("layout", "Publicera och aktivera en bordslayout", "Table layout live", LOCATION,
         lambda f, loc: loc.live_layout),
    Step("stripe", "Slutför Stripe för kortbetalningar", "Stripe for card payments", TENANT,
         lambda f, _: f.stripe_ready),
    Step("staff", "Bjud in personal", "Staff invited", TENANT, lambda f, _: _positive(f.staff_count)),
    Step("google", "Google-profil (valfritt)", "Google profile (optional)", LOCATION, _google_done,
         feature="googleBusiness", own_skip=lambda f, loc: loc.google_skipped),
    Step("phone", "Telefonassistent", "Phone assistant", LOCATION, _phone_done, feature="phoneAgent", sub=_phone_sub),
)

BY_ID = {s.id: s for s in STEPS}


def skip_key(step_id: str, location_id: Optional[str] = None) -> str:
    return f"{step_id}@{location_id}" if location_id else step_id


def applies(step: Step, features: dict) -> bool:
    return step.feature is None or bool((features or {}).get(step.feature))


def _state(step: Step, f: Facts, loc: Optional[LocationFacts]) -> dict:
    """One step at one place: {state, skippedHere, sub?}. A check that fails
    or returns something other than a bool counts as open."""
    try:
        done = step.check(f, loc)
    except Exception:  # noqa: BLE001 - a broken step must never finish a tenant
        done = None
    key = skip_key(step.id, loc.location_id if loc else None)
    skipped_here = step.skippable and key in f.skips
    own = bool(step.own_skip(f, loc)) if step.own_skip and loc else False
    if done is True:
        state = DONE
    elif step.skippable and (skipped_here or own):
        state = SKIPPED
    else:
        state = OPEN
    out = {"state": state, "known": isinstance(done, bool), "skippedHere": bool(skipped_here)}
    if step.sub and loc is not None:
        sub = step.sub(f, loc)
        if sub is not None:
            out["sub"] = [{"id": k, "done": bool(v)} for k, v in sub.items()]
    return out


def _combine(states: list[str]) -> str:
    """A location step over every location: open if any is open, done when
    all are done, else skipped (finished, at least one by skipping)."""
    if not states or OPEN in states:
        return OPEN
    return DONE if all(s == DONE for s in states) else SKIPPED


def evaluate(f: Facts) -> dict:
    """The checklist and the rule's answer. Shape (also PROFILE.setupState):
        {complete, total, done, skipped, open,
         steps: [{id, label, labelEn, scope, skippable, state, known,
                  skippedHere?, locations?: [{locationId, name, state, known, skippedHere, sub?}]}]}
    """
    steps = []
    for step in STEPS:
        if not applies(step, f.features):
            continue
        row = {"id": step.id, "label": step.label, "labelEn": step.label_en, "scope": step.scope,
               "skippable": step.skippable}
        if step.scope == TENANT:
            row.update(_state(step, f, None))
        else:
            places = [{"locationId": loc.location_id, "name": loc.name, **_state(step, f, loc)} for loc in f.locations]
            row["locations"] = places
            row["state"] = _combine([p["state"] for p in places])
            row["known"] = all(p["known"] for p in places) and bool(places)
        if row.get("state") not in STATES:
            raise ValueError(f"step {step.id} has no state")
        steps.append(row)
    counts = {s: sum(1 for r in steps if r["state"] == s) for s in STATES}
    return {
        "complete": bool(steps) and counts[OPEN] == 0,
        "total": len(steps),
        "done": counts[DONE],
        "skipped": counts[SKIPPED],
        "open": counts[OPEN],
        "steps": steps,
    }
