# Table orders (Bordsbeställning)

Staff take a table's order at the table on an iPad, phone or monitor. Food
goes to the kitchen and drinks to the bar as tickets, on the same stations,
kitchen display and printers as online orders. The guest pays afterwards in
the restaurant's own till; staff then close the tab with "Betald i kassan,
stäng".

Code: `functions/table-order/app.py` (API), `shared/table_orders.py` (model,
rows, views, table numbering, guest matching), `shared/tab_tickets.py`
(tickets, stations, menu view). Admin: Beställningar > Bord and "Ny
bordsbeställning".

## Product boundary

- Booqy takes no payment for table orders, prints nothing that looks like a
  bill and is not a cash register. Closing only records that the tab was
  paid in the restaurant's own system (`closeReason: paid_elsewhere`).
- `referenceTotal` ("Att slå in") is what staff key into their own till. It
  is shown on screen only: never printed, never sent to the guest.
- Tickets carry no prices and no guest phone number, and keep the existing
  kitchen ticket footer.
- No guest QR ordering (off since 2026-11-17, `docs/parked/kassa-2027`), no
  Stripe, no `paymentStatus`, no SMS or e-mail. Online pickup orders are
  unchanged.

## Model

One open tab per table. A tab has items and rounds.

| Item state | Meaning |
|---|---|
| `held` | Taken, waits for its course ("Vänta"); on no ticket |
| `sent` | On a ticket (`round` = the round that sent it) |
| `void` | Voided: `before_send` (removed while held), `closed` (held at close), or `wrong` / `guest_changed` / `other` (Makulera, with a void ticket) |

Courses: `drinks`, `starters`, `mains`, `desserts`, `other` (Dryck, Förrätt,
Varmrätt, Efterrätt, Övrigt). A dish's course: the category's `course` field,
else a default category id, else `drinks` when it goes to the drinks station,
is alcohol or its category name sounds like drinks, else `other`. Staff can
override it per item.

Pacing defaults (server `default_hold`, admin `courses.ts`): drinks, starters
and other go now; mains wait while starters are in the same send or on the
tab and not ready on the kitchen display; desserts wait while the tab has
starters or mains. A request item may set `hold` explicitly.

Rounds (`order`, `fire`, `void`, `move`) each make tickets with source id
`<tabId>r<round>`. `dispatched` turns true once the tickets are stored.

Status: `open -> closed`; `closed -> open` ("Ångra") within 15 minutes and
only if the table is free. There is no cancelled status: a tab opened by
mistake is emptied and closed.

## API

Base `/locations/{locationId}/table-orders`, JWT, owner and staff (staff only
on their own location, staff area `bestallningar`), plan feature `ordering`.
Not dependent on online ordering being on, the pause flag or Stripe.

| Method and path | Purpose |
|---|---|
| `GET ?version=N` | Open tabs, closed in the last 12 h, tables with tab and guest; `{unchanged, version}` when nothing changed |
| `GET /menu` | Available dishes with course and station |
| `GET /closed?date=` | Tabs closed that local day |
| `POST` | Open a tab (`tableId`, items, covers, guest) |
| `GET /{tabId}` | One tab with ticket states per round |
| `PATCH /{tabId}` | Held items, covers, guest name, note (`expectedVersion`) |
| `POST /{tabId}/items` | Add items |
| `POST /{tabId}/send` | Fire held courses or items |
| `POST /{tabId}/void` | Void sent items, whole or part (`expectedVersion`) |
| `POST /{tabId}/move` | Another table (`expectedVersion`) |
| `POST /{tabId}/close` | Paid in the restaurant's till (`expectedVersion`, `dropHeld`) |
| `POST /{tabId}/reopen` | Undo a close within 15 minutes |
| `POST /{tabId}/rounds/{n}/dispatch` | Tickets for a round that was not dispatched |

Errors are `{"error": code, "message": Swedish text}`, plus `tab` when the
client should show the fresh tab. Codes: `invalid_request`,
`option_required`, `option_invalid`, `not_found`, `table_has_open_order`,
`table_not_found`, `no_tables`, `menu_item_unavailable`, `changed_retry`,
`held_items`, `tab_closed`, `reopen_expired`, `booking_not_found`,
`tab_full` (413), `internal` (500, "Booqys fel."). Print problems never fail
a request: they come back as `warnings` (`station_no_printer`,
`dispatch_failed`) with a text that says who acts.

## Safety

- Every write carries a client `requestId`. Its `REQ#` row is written in the
  same transaction as the tab. A repeated request returns the tab with
  `replayed: true` and dispatches the round again only when it was not
  dispatched (tickets are put once), so a double tap or a network retry
  never makes a second round or ticket.
- Add and fire are commutative: read, apply and write conditional on
  `version`, retried up to three times. Close, move, void and PATCH take the
  version the screen showed and answer `409 changed_retry` with the fresh
  tab.
- `OPEN#<tableId>` is the per-table lock, written in the same transaction as
  the open, move, close and reopen. A second waiter opening the same table
  gets `409 table_has_open_order` with the open tab and adds to it instead.
- Tickets are made after the write. If that fails the round stays "not
  dispatched"; "Skicka igen" or a replay of the request sends it.

## Tables and guests

Tables come from the active published layout and are numbered exactly like
admin `tablesFromLayout` and the booking site (floors by level, tables in
drawing order, numbered across floors, an own label wins;
`tests/test_table_order_numbering.py`). The guest of a table is today's
booking: arrived first (until two hours after its end), else reserved or
pending running now (from 30 minutes before its start), else the next one
within 90 minutes (`upcoming`). Opening a tab snapshots the guest's name and
party size so the right guest pays for the right order.

## Live updates

Every write bumps the location's `STATE.version`. Screens poll
`GET ?version=N` (one read when nothing changed): every 5 s on the Bord view,
table picker and an open tab, the shared live pulse elsewhere. The kitchen
display keeps its own 5 s ticket poll. A later option is EventBridge with
WebSocket or AppSync push; there is no websocket infrastructure today.

## Isolation and retention

Table orders live in their own DynamoDB table (`table-order`) without a
stream. Accounting (Fortnox, Visma), Statistik, finance-rollup, guest CRM,
marketing, notification, Google Business and data-retention never read it
(`tests/test_table_order_isolation.py`). Tickets in the print table expire
after 2 days and carry no prices.

Guest names on tabs are kept at most 14 days after close (`ttl`). Open tabs
and table locks expire 48 hours after their last write, request rows after
24 hours. tenant-purge does not touch the table: TTL clears a closed
restaurant's tabs within 14 days (accepted trade-off).

## Infrastructure

Lambda `table-order` (image, alias `live`) with its own role: table-order
table (item actions), menu (read), print table (read and write for tickets),
published layout snapshot (GetItem), reservation table (read), and the
tenant, location and user reads every staff-checking function has.
