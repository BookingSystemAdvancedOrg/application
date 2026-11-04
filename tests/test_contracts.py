"""contracts/booqy.yaml and the files generated from it (CONTRACTS.md 1.2)."""

import copy
import importlib.util
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gen_contracts", APP / "scripts" / "gen_contracts.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

RAW = gen.YAML_PATH.read_bytes()
DATA = gen.load(RAW)
TEXTS = gen.render(RAW)


def test_application_file_is_generated():
    assert (APP / gen.TARGETS["application"]).read_text(encoding="utf-8") == TEXTS["application"]


@pytest.mark.parametrize("target", [t for t in gen.TARGETS if t != "application"])
def test_sibling_files_match_when_present(target):
    path = (APP / gen.TARGETS[target]).resolve()
    if not path.exists():
        pytest.skip(f"{path} not on disk (application CI checks only its own file)")
    assert path.read_text(encoding="utf-8") == TEXTS[target]


def test_output_is_deterministic():
    assert gen.render(RAW) == TEXTS
    assert all(t.startswith(("# ", "// ")) and gen.HEADER in t.splitlines()[0] for t in TEXTS.values())


def test_yaml_is_consistent():
    assert gen.validate(DATA) == []


def test_feature_counts():
    from shared import contracts
    assert len(contracts.FEATURE_KEYS) == 18 and len(contracts.NEW_FEATURE_KEYS) == 12
    assert set(contracts.SITE_CONFIG_FEATURES) <= set(contracts.FEATURE_KEYS)
    assert "kassa" not in contracts.SITE_CONFIG_FEATURES


def test_run0_addendum_entries():
    """CONTRACTS.md 17: features K and L."""
    from shared import contracts
    assert contracts.FEATURE_KEYS[-2:] == ("events", "phoneAgent")
    assert contracts.FEATURE_IDS["events"] == "events-tickets" and contracts.FEATURE_IDS["phoneAgent"] == "phone-agent"
    assert "events" in contracts.SITE_CONFIG_FEATURES and "phoneAgent" not in contracts.SITE_CONFIG_FEATURES
    assert contracts.LIMITS["phoneAgentMinutesPerMonth"] == {"feature": "phoneAgent", "min": 0, "max": 100000,
                                                              "default": 0}
    purge = contracts.HOOKS["tenant.purge"]["targets"]
    assert len(purge) == len(set(purge)) == 12
    assert set(purge) == {contracts.FEATURE_IDS[k] for k in contracts.NEW_FEATURE_KEYS}
    for hook in ("privacy.export", "privacy.erase"):
        assert contracts.HOOKS[hook]["targets"][-2:] == ["events-tickets", "phone-agent"]
    assert contracts.HOOKS["stripe.event"]["targets"] == ["gift-cards", "events-tickets"]
    assert contracts.HOOKS["accounting.events"]["targets"] == ["gift-cards", "events-tickets"]
    assert contracts.HOOKS["insights.events"]["targets"] == ["events-tickets"]
    assert contracts.HOOKS["billing.usage"]["targets"] == ["phone-agent"]
    assert contracts.HOOKS["platform"]["targets"][-1] == "phone-agent"
    assert {"booqy.events-tickets", "booqy.phone-agent"} <= set(contracts.EVENT_SOURCES)
    assert len(contracts.EVENTS["booqy.events-tickets"]) == 6 and len(contracts.EVENTS["booqy.phone-agent"]) == 8
    assert contracts.EVENTS["booqy.phone-agent"]["PhoneCall.Ended"]["optional"] == {
        "reservationId": "string", "callbackId": "string",
        # ADR-L counter-offers (DECISIONS.md 2026-11-04)
        "counterOffer": "enum[none,other_time,next_day,nearby_table]"}
    assert contracts.CounterOffer.__args__ == ("none", "other_time", "next_day", "nearby_table")
    assert {k: contracts.CLIENTS[k]["env"] for k in ("voice", "telephony", "agent_model")} == {
        "voice": "VOICE_CLIENT", "telephony": "TELEPHONY_CLIENT", "agent_model": "AGENT_MODEL_CLIENT"}
    ops = gen.load(RAW)["consoleForwardOps"]
    assert [op for op in ops if op.startswith("phone-agent.")] == [
        "phone-agent.tenant.get", "phone-agent.tenant.pause", "phone-agent.settings.get", "phone-agent.settings.put"]
    slots = DATA["slots"]
    assert {s for s, v in slots["admin"].items() if v["feature"] in ("events", "phoneAgent")} == {
        "bokningar.panel.callbacks", "bokningar.reservation.call", "statistik.events"}
    assert {s for s, v in slots["console"].items() if v["feature"] == "phoneAgent"} == {
        "tenant.integrations.phoneAgent"}
    # the console Settings page is platform-wide: no tenant feature, like settings.metaAds (DECISIONS.md 2026-11-02)
    assert slots["console"]["settings.phoneAgent"]["feature"] is None
    assert slots["console"]["settings.metaAds"]["feature"] is None
    # staff see the Statistik events slot with money removed: gate is on or ever on, never owner-only
    assert DATA["hooks"]["insights.events"]["gate"] == "events on or ever on"


def test_storage_registries():
    storage = DATA["storage"]
    assert set(storage) == {"siteKit", "admin"}
    assert storage["siteKit"]["TICKET_PURCHASE_KEY"]["key"] == "booqy-ticket-purchase"
    assert storage["admin"]["EVENT_SCAN_KEY_PREFIX"] == {
        **storage["admin"]["EVENT_SCAN_KEY_PREFIX"], "key": "booqy-event-scan-", "kind": "IndexedDB",
        "feature": "events", "category": "nödvändig"}
    assert "export const STORAGE_KEYS = {\n  \"EVENT_SCAN_KEY_PREFIX\": \"booqy-event-scan-\"\n} as const;" \
        in TEXTS["admin"]
    assert "export const TICKET_PURCHASE_KEY = 'booqy-ticket-purchase';" in TEXTS["site-kit"]
    assert "EVENT_SCAN_KEY_PREFIX" not in TEXTS["site-kit"] and "TICKET_PURCHASE_KEY" not in TEXTS["admin"]


@pytest.mark.parametrize("detail_type, ok", [
    ("GiftCard.Sold", True), ("Waitlist.Left", True), ("DishMargin.CostSet", True),
    ("Personalliggare.CheckedOut", True), ("MetaSpend.CapReached", True), ("ReviewRequest.Sent", True),
    ("Ticket.Sold", True), ("Ticket.CheckedIn", True), ("PhoneMinutes.CapReached", True),
    ("PhoneNumber.Provisioned", True), ("EventOccurrence.Cancelled", True),
    ("GiftCard.sold", False), ("GiftCard.Sell", False), ("giftCard.Sold", False), ("GiftCardSold", False),
])
def test_detail_type_rule(detail_type, ok):
    assert bool(gen.DETAIL_TYPE_RE.match(detail_type)) is ok


def _broken(edit):
    data = copy.deepcopy(DATA)
    edit(data)
    return gen.validate(data)


def test_validate_catches_bad_contracts():
    assert _broken(lambda d: d["events"]["sources"].__setitem__("booqy.nope", {}))
    assert _broken(lambda d: d["events"]["sources"]["booqy.waitlist"].__setitem__("Waitlist.join", {}))
    assert _broken(lambda d: d["events"]["sources"]["booqy.waitlist"]["Waitlist.Left"].__setitem__("x", "float"))
    assert _broken(lambda d: d["hooks"]["stripe.event"]["targets"].append("giftcards"))
    assert _broken(lambda d: d["slots"]["admin"]["meny.dish.cost"].__setitem__("feature", "dishMargins"))
    assert _broken(lambda d: d["features"].append(dict(d["features"][0])))
    assert _broken(lambda d: d["siteConfig"]["tenantFeatures"].append("kassa"))
    assert _broken(lambda d: d["limits"]["siteBuilderAiEditsPerMonth"].__setitem__("feature", "x"))
    assert _broken(lambda d: d["storage"]["admin"]["EVENT_SCAN_KEY_PREFIX"].__setitem__("feature", "event"))
    assert _broken(lambda d: d["storage"].__setitem__("console", {}))
    assert _broken(lambda d: d["storage"]["admin"].__setitem__(
        "TICKET_PURCHASE_KEY", d["storage"]["siteKit"]["TICKET_PURCHASE_KEY"]))
    assert _broken(lambda d: d["hooks"]["tenant.purge"]["targets"].append("phone-agents"))
    assert _broken(lambda d: d["features"].pop())


def test_duplicate_yaml_keys_are_refused():
    with pytest.raises(ValueError, match="duplicate key"):
        gen.load(b"a: 1\na: 2\n")


def test_check_mode_reports_missing_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gen, "APP_ROOT", tmp_path)
    assert gen.main(["--check", "--only", "application"]) == 1
    assert "missing" in capsys.readouterr().err
    assert not (tmp_path / "shared").exists()
