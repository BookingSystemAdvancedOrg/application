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
    assert len(contracts.FEATURE_KEYS) == 16 and len(contracts.NEW_FEATURE_KEYS) == 10
    assert set(contracts.SITE_CONFIG_FEATURES) <= set(contracts.FEATURE_KEYS)
    assert "kassa" not in contracts.SITE_CONFIG_FEATURES


@pytest.mark.parametrize("detail_type, ok", [
    ("GiftCard.Sold", True), ("Waitlist.Left", True), ("DishMargin.CostSet", True),
    ("Personalliggare.CheckedOut", True), ("MetaSpend.CapReached", True), ("ReviewRequest.Sent", True),
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


def test_duplicate_yaml_keys_are_refused():
    with pytest.raises(ValueError, match="duplicate key"):
        gen.load(b"a: 1\na: 2\n")


def test_check_mode_reports_missing_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gen, "APP_ROOT", tmp_path)
    assert gen.main(["--check", "--only", "application"]) == 1
    assert "missing" in capsys.readouterr().err
    assert not (tmp_path / "shared").exists()
