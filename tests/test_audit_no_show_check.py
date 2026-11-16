"""Audit #59: no-show-check is retired; a stray schedule or a dlq-replay
redelivery must not fail."""
import importlib.util
from pathlib import Path

APP_PATH = Path(__file__).parents[1] / "functions" / "no-show-check" / "app.py"


def load():
    spec = importlib.util.spec_from_file_location("no_show_check_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retired_handler_is_a_no_op(monkeypatch):
    for name in ("ENVIRONMENT", "LOCATION_TABLE_NAME", "SLOT_OCCUPANCY_TABLE_NAME",
                 "RESERVATION_TABLE_NAME", "PAYMENT_DELINQUENCY_TABLE_NAME"):
        monkeypatch.delenv(name, raising=False)
    app = load()
    assert app.handler({"reservation_id": "r-1"}, None) == {"ok": True, "result": "retired"}
    assert app.handler({}, None)["ok"] is True
    assert app.handler(None, None)["ok"] is True
