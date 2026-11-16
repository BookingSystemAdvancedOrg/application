"""Audit #32: the Logs Insights parse must split loc from kind, so the
per-location request counts match the real location id."""

import importlib.util
import logging
import re
from pathlib import Path

from shared import tenant

APP = Path(__file__).parents[1] / "functions" / "finance-rollup" / "app.py"


def _app():
    spec = importlib.util.spec_from_file_location("finance_rollup_audit_query", APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insights_parse(query, message):
    """Logs Insights glob parse: every * is lazy except the last, which takes the rest."""
    pattern = re.search(r'parse @message "([^"]+)" as ([\w, ]+?) \|', query)
    glob, names = pattern.group(1), [n.strip() for n in pattern.group(2).split(",")]
    parts = glob.split("*")
    regex = "".join(re.escape(p) + ("(.*?)" if i < len(parts) - 2 else "(.*)" if i == len(parts) - 2 else "")
                    for i, p in enumerate(parts))
    match = re.search(regex, message)
    return dict(zip(names, match.groups())) if match else {}


def test_requests_query_parses_the_location_id_alone(caplog):
    caplog.set_level(logging.INFO, logger="shared.tenant.requests")
    tenant._log_request("01tenant", "jwt", "loc-a")
    line = caplog.records[-1].getMessage()
    parsed = _insights_parse(_app().REQUESTS_QUERY, line)
    assert parsed["tenant"] == "01tenant"
    assert parsed["loc"] == "loc-a"
    assert "stats count(*) as n by tenant, loc" in _app().REQUESTS_QUERY
