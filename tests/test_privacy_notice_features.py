"""Privacy notice template: the guest register (guestCrm) and queue
(waitlist) paragraphs appear only when the feature is on."""

import pytest

from shared import privacy_notice
from tenant_support import TENANT_A, features_on, tenant_row

PERIODS = {"bookingMonths": 12, "guestMonths": 24, "cancelledMonths": 6, "accountingYears": 7}


def _text(features):
    return privacy_notice.template(tenant_row(TENANT_A, features=features), PERIODS)


@pytest.mark.parametrize("features", [None, features_on(), features_on("giftCards", "siteBuilder")])
def test_paragraphs_absent_when_off(features):
    text = _text(features)
    assert "Gästregister" not in text and "- Kö:" not in text


def test_paragraphs_present_when_on():
    assert "- Gästregister:" in _text(features_on("guestCrm")) and "- Kö:" not in _text(features_on("guestCrm"))
    assert "- Kö:" in _text(features_on("waitlist")) and "Gästregister" not in _text(features_on("waitlist"))


def test_template_unchanged_without_new_features():
    legacy = _text(None)
    assert _text(features_on()) == legacy
    assert "—" not in _text(features_on("guestCrm", "waitlist"))
    assert "–" not in _text(features_on("guestCrm", "waitlist"))
