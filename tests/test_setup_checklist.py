"""Kom igång's one rule (shared/setup_checklist.py): done or skipped, every
applicable step, every location. Pure, no AWS."""

import dataclasses

import pytest

from shared import setup_checklist as rule

ALL = {"ordering": True, "googleBusiness": True, "phoneAgent": True}
PHONE_ALL = {"tider": True, "nummer": True, "koppla": True, "prova": True}


def done_location(lid="loc-1", **extra):
    return rule.LocationFacts(location_id=lid, name=lid, has_hours=True, active_menu_items=3, live_layout=True,
                              printers=1, google_linked=True, phone=dict(PHONE_ALL), **extra)


def done_facts(**extra):
    base = dict(features=dict(ALL), dpa_up_to_date=True, stripe_ready=True, staff_count=2, phone_named=True,
                google_connected=True, locations=[done_location()])
    base.update(extra)
    return rule.Facts(**base)


def state_of(result, step_id):
    return next(s for s in result["steps"] if s["id"] == step_id)


def test_everything_done_is_complete():
    r = rule.evaluate(done_facts())
    assert r["complete"] is True
    assert (r["total"], r["done"], r["skipped"], r["open"]) == (9, 9, 0, 0)
    assert [s["id"] for s in r["steps"]] == [s.id for s in rule.STEPS]


def test_a_new_restaurant_is_open():
    r = rule.evaluate(rule.Facts(features=dict(ALL), locations=[rule.LocationFacts("loc-1")]))
    assert r["complete"] is False
    assert r["open"] == r["total"] == 9


def test_steps_follow_the_plan():
    r = rule.evaluate(done_facts(features={}))
    assert [s["id"] for s in r["steps"]] == ["dpa", "hours", "menu", "layout", "stripe", "staff"]
    assert r["complete"] is True


def test_skipped_counts_as_finished():
    f = done_facts(stripe_ready=False, staff_count=0, skips={"stripe", "staff"})
    r = rule.evaluate(f)
    assert r["complete"] is True
    assert (r["done"], r["skipped"]) == (7, 2)
    assert state_of(r, "stripe")["state"] == "skipped" and state_of(r, "stripe")["skippedHere"] is True


def test_dpa_can_not_be_skipped():
    r = rule.evaluate(done_facts(dpa_up_to_date=False, skips={"dpa"}))
    assert state_of(r, "dpa")["state"] == "open"
    assert r["complete"] is False


def test_location_steps_need_every_location():
    second = dataclasses.replace(done_location("loc-2"), active_menu_items=0)
    r = rule.evaluate(done_facts(locations=[done_location(), second]))
    menu = state_of(r, "menu")
    assert menu["state"] == "open"
    assert [p["state"] for p in menu["locations"]] == ["done", "open"]
    assert r["complete"] is False
    # skipping it at that location (not the other) finishes it
    r = rule.evaluate(done_facts(locations=[done_location(), second], skips={"menu@loc-2"}))
    assert state_of(r, "menu")["state"] == "skipped"
    assert r["complete"] is True
    # a skip at another location doesn't count here
    r = rule.evaluate(done_facts(locations=[done_location(), second], skips={"menu@loc-9", "menu"}))
    assert state_of(r, "menu")["state"] == "open"


def test_no_locations_is_never_complete():
    r = rule.evaluate(done_facts(locations=[]))
    assert state_of(r, "hours")["state"] == "open"
    assert r["complete"] is False


def test_own_skips_count():
    loc = dataclasses.replace(done_location(), printers=0, printers_skipped=True, google_linked=False,
                              google_skipped=True)
    r = rule.evaluate(done_facts(locations=[loc]))
    assert state_of(r, "printers")["state"] == "skipped"
    assert state_of(r, "google")["state"] == "skipped"
    assert state_of(r, "printers")["locations"][0]["skippedHere"] is False   # not the generic skip
    assert r["complete"] is True


def test_phone_needs_all_five_substeps():
    loc = dataclasses.replace(done_location(), phone={**PHONE_ALL, "prova": False})
    r = rule.evaluate(done_facts(locations=[loc]))
    phone = state_of(r, "phone")
    assert phone["state"] == "open"
    assert phone["locations"][0]["sub"] == [{"id": "namn", "done": True}, {"id": "tider", "done": True},
                                            {"id": "nummer", "done": True}, {"id": "koppla", "done": True},
                                            {"id": "prova", "done": False}]


def test_unknown_facts_fail_closed():
    loc = dataclasses.replace(done_location(), active_menu_items=None)
    r = rule.evaluate(done_facts(locations=[loc], stripe_ready=None))
    assert state_of(r, "menu")["state"] == "open" and state_of(r, "menu")["known"] is False
    assert state_of(r, "stripe")["state"] == "open"
    assert r["complete"] is False


def test_a_broken_check_counts_as_open(monkeypatch):
    def boom(f, loc):
        raise RuntimeError("bug")
    broken = dataclasses.replace(rule.BY_ID["staff"], check=boom)
    monkeypatch.setattr(rule, "STEPS", tuple(broken if s.id == "staff" else s for s in rule.STEPS))
    r = rule.evaluate(done_facts())
    assert state_of(r, "staff")["state"] == "open"
    assert r["complete"] is False


# --- new steps -----------------------------------------------------------------------------

@pytest.mark.parametrize("step", rule.STEPS, ids=lambda s: s.id)
def test_every_step_has_a_state(step):
    """A step added to STEPS must give a state for a fully set-up restaurant
    and for a brand-new one, have both labels and a known scope, and be
    reachable (done when set up). Fails for a step that returns no state."""
    assert step.label and step.label_en and step.scope in (rule.TENANT, rule.LOCATION)
    for facts in (done_facts(), rule.Facts(features=dict(ALL), locations=[rule.LocationFacts("loc-1")])):
        r = rule.evaluate(facts)
        row = state_of(r, step.id)
        assert row["state"] in rule.STATES
        for place in row.get("locations", []):
            assert place["state"] in rule.STATES
    assert state_of(rule.evaluate(done_facts()), step.id)["state"] == "done", \
        f"{step.id}: done_facts() must finish the step; give it a fact in Facts/LocationFacts"


def test_a_step_without_a_state_fails(monkeypatch):
    """The guard itself: a step whose state is missing stops the rule."""
    monkeypatch.setattr(rule, "_combine", lambda states: None)
    with pytest.raises(ValueError, match="has no state"):
        rule.evaluate(done_facts())


def test_new_step_is_part_of_the_rule(monkeypatch):
    extra = rule.Step("logo", "Ladda upp logga", "Logo", rule.TENANT, lambda f, _: False)
    monkeypatch.setattr(rule, "STEPS", rule.STEPS + (extra,))
    r = rule.evaluate(done_facts())
    assert r["total"] == 10 and state_of(r, "logo")["state"] == "open" and r["complete"] is False
    r = rule.evaluate(done_facts(skips={"logo"}))
    assert r["complete"] is True
