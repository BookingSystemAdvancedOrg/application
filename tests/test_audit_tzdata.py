"""Every function whose app.py calls ZoneInfo ships the tzdata package: the
Lambda base image may have no system time zone database, and a missing zone
raises ZoneInfoNotFoundError (a KeyError) that handlers tend to swallow."""

from pathlib import Path

import pytest

FUNCTIONS = Path(__file__).parents[1] / "functions"


def _uses_zoneinfo():
    return sorted(d.name for d in FUNCTIONS.iterdir()
                  if (d / "app.py").is_file() and "ZoneInfo(" in (d / "app.py").read_text())


@pytest.mark.parametrize("name", _uses_zoneinfo())
def test_function_using_zoneinfo_ships_tzdata(name):
    req = FUNCTIONS / name / "requirements.txt"
    lines = [line.split("#")[0].strip().lower() for line in req.read_text().splitlines()]
    assert any(line == "tzdata" or line.startswith(("tzdata=", "tzdata>", "tzdata<", "tzdata~"))
               for line in lines), f"{name}/requirements.txt needs tzdata"


def test_tenant_site_config_is_covered():
    assert "tenant-site-config" in _uses_zoneinfo()
