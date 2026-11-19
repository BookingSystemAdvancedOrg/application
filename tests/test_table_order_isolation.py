"""Table orders live in their own table without a stream: accounting,
Statistik, finance-rollup, guest CRM, marketing, notification and
data-retention cannot see them (docs/TABLE_ORDERS.md). Only the table-order
function and shared/table_orders.py (plus the ticket facade
shared/tab_tickets.py) may read TABLE_ORDER_TABLE_NAME."""

from pathlib import Path

ROOT = Path(__file__).parents[1]
ALLOWED_SHARED = {"table_orders.py", "tab_tickets.py"}


def test_only_the_table_order_function_and_its_modules_read_the_table():
    readers = []
    for path in sorted((ROOT / "functions").rglob("*.py")):
        if path.relative_to(ROOT / "functions").parts[0] == "table-order":
            continue
        if "TABLE_ORDER_TABLE_NAME" in path.read_text(encoding="utf-8"):
            readers.append(str(path.relative_to(ROOT)))
    for path in sorted((ROOT / "shared").rglob("*.py")):
        if path.name not in ALLOWED_SHARED and "TABLE_ORDER_TABLE_NAME" in path.read_text(encoding="utf-8"):
            readers.append(str(path.relative_to(ROOT)))
    assert readers == []


def test_the_order_pipeline_does_not_import_table_orders():
    for name in ("accounting", "insights", "insights-rollup", "finance-rollup", "guest-crm", "guest-crm-sync",
                 "marketing", "marketing-sync", "notification", "data-retention", "manage-order", "get-order"):
        source = (ROOT / "functions" / name / "app.py").read_text(encoding="utf-8")
        assert "table_orders" not in source and "table-order" not in source, name


def test_the_table_order_function_never_touches_the_order_table_or_payments():
    source = (ROOT / "functions" / "table-order" / "app.py").read_text(encoding="utf-8")
    shared = (ROOT / "shared" / "table_orders.py").read_text(encoding="utf-8")
    for text in (source, shared):
        assert "ORDER_TABLE_NAME\"" not in text.replace("TABLE_ORDER_TABLE_NAME\"", "")
        assert "stripe" not in text.lower()
        assert "kvitto" not in text.lower()
