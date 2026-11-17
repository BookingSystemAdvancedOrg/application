"""Pay-later QR table orders outside the order Lambdas: the kitchen print
(INSERT as confirmed prints once, "BETALAS VID BORDET"), the guest e-mails
(bill receipt, unpaid cancel), Statistik (paid outside Booqy on its own),
the accounting export (paid day, paid outside left out) and retention."""

from datetime import date
from zoneinfo import ZoneInfo

from boto3.dynamodb.types import TypeSerializer

from shared import accounting, insights as ins, order_messages, retention
from shared import print_ticket as t
from shared import printing as p
from test_printing import LOC_A, ORDER_ID, add_printer, env, order  # noqa: F401

_ser = TypeSerializer()
TZ = ZoneInfo("Europe/Stockholm")


def insert(env, new):
    record = {"eventName": "INSERT", "dynamodb": {
        "SequenceNumber": "1", "NewImage": {k: _ser.serialize(v) for k, v in new.items()}}}
    return env["jobs"].handler({"Records": [record]}, None)


# --- printing ---------------------------------------------------------------------------------

def test_pay_later_order_prints_once_when_written(env):
    add_printer(env, autoPrint=True)
    new = order(mode="table", tableLabel="7", payLater=True, paymentStatus="unpaid")
    assert insert(env, new) == {"batchItemFailures": []}
    insert(env, new)  # redelivered
    jobs = p.order_jobs(LOC_A, ORDER_ID)
    assert [j["kind"] for j in jobs] == ["station"]


def test_insert_without_pay_later_prints_nothing(env):
    add_printer(env, autoPrint=True)
    insert(env, order(mode="table", tableLabel="7", status="pending_payment"))
    insert(env, order(mode="table", tableLabel="7"))  # confirmed but not pay later (gift card in full: MODIFY)
    assert p.order_jobs(LOC_A, ORDER_ID) == []


def test_ticket_says_it_is_paid_at_the_table():
    text = t.order_ticket(order(mode="table", tableLabel="7", payLater=True), {}).decode("cp1252")
    assert "BORD 7" in text and "BETALAS VID BORDET" in text
    assert "BETALAS VID BORDET" not in t.order_ticket(order(mode="table", tableLabel="7"), {}).decode("cp1252")
    header = p.order_header(order(mode="table", tableLabel="7", payLater=True))
    assert header["payLater"] is True and "payLater" not in p.order_header(order())
    station = t.station_ticket({**header, "stationName": "Kök", "entries": []}, order(), {}).decode("cp1252")
    assert "BETALAS VID BORDET" in station


# --- guest e-mails ----------------------------------------------------------------------------

BILL = {"billId": "b" * 32, "tableLabel": "7", "amount": 37550, "paidAt": "2026-09-08T12:00:00Z",
        "timezone": "Europe/Stockholm", "payerEmail": "payer@example.se",
        "vat": [{"rate": 12, "gross": 28050, "net": 25045, "vat": 3005},
                {"rate": 25, "gross": 9500, "net": 7600, "vat": 1900}],
        "orders": [{"number": 1, "lines": [{"name": "Margherita", "quantity": 2, "total": 25000, "vatRate": 12},
                                           {"name": "Cola", "quantity": 1, "total": 3050, "vatRate": 12}]},
                   {"number": 2, "lines": [{"name": "Husets röda", "quantity": 1, "total": 9500, "vatRate": 25}]}]}
TENANT = {"tenantId": "t", "name": "Roma", "legalName": "Roma AB", "orgNumber": "556000-0000"}
LOCATION = {"name": "Södermalm", "address": "Götgatan 1, Stockholm"}


def test_bill_receipt_lists_every_order_and_the_vat():
    subject, text, html = order_messages.bill_email(BILL, TENANT, LOCATION)
    assert subject.startswith("Kvitto för notan vid bord 7 - ")
    assert "Tack! Notan vid bord 7 är betald." in text and "Kvitto - Nota" in text
    assert "2 x #1 Margherita" in text and "1 x #2 Husets röda" in text
    assert "Moms 25 %" in text and "Referens: " + "b" * 32 in text and "Roma AB" in text
    assert "Nota</h2>" in html


def test_unpaid_order_cancel_says_nothing_was_paid():
    item = {"orderId": "o" * 32, "number": 4, "customerName": "Anna", "paymentStatus": "void", "mode": "table",
            "tableLabel": "7", "totalAmount": 12500, "lines": []}
    _, text, _ = order_messages.email("order_cancelled", item, TENANT, LOCATION, None)
    assert "Du har inte betalat något för den." in text and "tillbaka" not in text


# --- Statistik ---------------------------------------------------------------------------------

def _order(pay, total, **extra):
    return {"paymentStatus": pay, "status": "completed", "mode": "table", "createdAt": "2026-10-20T16:05:00Z",
            "totalAmount": total, "lines": [{"menuItemId": "a", "name": "Pasta", "quantity": 1, "total": total}]} | extra


def test_paid_outside_is_its_own_figure_and_unpaid_counts_nowhere():
    location = {"locationId": "loc-1", "timezone": "Europe/Stockholm",
                "businessHours": {d: [{"opensAt": "11:00", "closesAt": "22:00"}] for d in ins.WEEKDAYS}}
    row = ins.build_day(location=location, day="2026-10-20", snapshots=[], built_at="x", orders=[
        _order("paid", 10000), _order("paid_outside", 20000), _order("unpaid", 40000),
        _order("written_off", 80000), _order("void", 160000, status="cancelled")])
    cell = row["o"]["18"]
    assert cell["n"] == 1 and cell["rev"] == 10000 and cell["out"] == 20000 and cell["outN"] == 1
    assert row["d"]["a"]["q"] == {"18": 2} and row["d"]["a"]["r"] == {"18": 30000}
    summary = ins.summarize([{**row, "date": "2026-10-20"}])
    assert summary["orderRevenue"] == 10000 and summary["orders"] == 1
    assert summary["paidOutsideRevenue"] == 20000 and summary["paidOutsideOrders"] == 1
    assert "paidOutsideRevenue" not in ins.without_money(summary)


# --- accounting --------------------------------------------------------------------------------

def test_bill_paid_next_day_is_a_sale_that_day_and_paid_outside_is_left_out():
    vat = [{"rate": 12, "gross": 11200, "net": 10000, "vat": 1200}]
    orders = [
        {"paymentStatus": "paid", "vat": vat, "totalAmount": 11200, "confirmedAt": "2026-09-08T21:50:00Z",
         "paidAt": "2026-09-08T22:30:00Z"},
        {"paymentStatus": "paid_outside", "vat": vat, "totalAmount": 11200, "confirmedAt": "2026-09-08T20:00:00Z",
         "paidAt": "2026-09-08T20:30:00Z"},
        {"paymentStatus": "unpaid", "vat": vat, "totalAmount": 11200, "confirmedAt": "2026-09-08T20:00:00Z"},
        {"paymentStatus": "written_off", "vat": vat, "totalAmount": 11200, "confirmedAt": "2026-09-08T20:00:00Z"},
    ]
    events = accounting.order_events(orders, "2026-09-08", "2026-09-09", TZ)
    sales = [e for e in events if e.postings]
    assert [e.day for e in sales] == ["2026-09-09"]  # 00:30 Stockholm
    notes = [e for e in events if not e.postings]
    assert [(e.day, e.warnings) for e in notes] == [
        ("2026-09-08", ["1 bordsbeställningar betalda utanför Booqy ingår inte (bokförs från ert eget system)."])]


# --- retention ----------------------------------------------------------------------------------

def test_retention_keeps_open_and_settled_table_orders():
    policy = retention.policy()
    today = date(2034, 1, 1)
    old = {"date": "2026-09-08"}
    assert retention.order_action({**old, "paymentStatus": "unpaid", "status": "completed"}, today, policy) is None
    assert retention.order_action({**old, "paymentStatus": "paid_outside"}, today, policy) == "delete"
    assert retention.order_action({**old, "paymentStatus": "written_off"}, date(2029, 1, 1), policy) == "anonymise"
    assert retention.order_action({**old, "paymentStatus": "void", "status": "cancelled"}, date(2028, 1, 1),
                                  policy) == "delete"
    bill = {"serviceDate": "2026-09-08", "status": "paid", "payerEmail": "p@example.se"}
    assert retention.bill_action(bill, date(2029, 1, 1), policy) == "anonymise"
    assert retention.bill_action(bill, today, policy) == "delete"
    assert retention.bill_action({**bill, "status": "expired", "ttl": 1}, today, policy) is None
    expr, names, _ = retention.anonymise_update("bill", bill, "2027-06-01T00:00:00Z")
    assert "payerEmail" in names.values() and "REMOVE" in expr
