"""shared/dpa.py: the go-live gate must never take an existing restaurant offline."""

from shared import dpa


def test_only_flagged_tenants_without_any_acceptance_are_blocked():
    assert dpa.blocks_public({"dpaRequired": True}) is True
    assert dpa.blocks_public({}) is False
    assert dpa.blocks_public({"dpaRequired": "yes"}) is False
    assert dpa.blocks_public(None) is False


def test_an_older_accepted_version_keeps_the_site_live():
    row = {"dpaRequired": True, "dpa": {"version": "2025-01-v0", "acceptedAt": "2025-01-01T00:00:00Z"}}
    assert dpa.blocks_public(row) is False
    status = dpa.status(row)
    assert status["upToDate"] is False and status["acceptedVersion"] == "2025-01-v0"
    assert status["currentVersion"] == dpa.CURRENT_VERSION and status["required"] is True


def test_status_without_acceptance():
    assert dpa.status({"dpa": "garbage"}) == {
        "currentVersion": dpa.CURRENT_VERSION, "acceptedVersion": None, "acceptedAt": None,
        "required": False, "upToDate": False,
    }
