# Food orders

Guests order food in two ways:

| Mode | Where the guest is | How they get the food |
|---|---|---|
| `pickup` | anywhere - the restaurant's site, `/bestall` | collects at the counter when the "ready" SMS arrives |
| `table` | at a table - scans its QR code, `/bestall?bord=<code>` | staff bring it to that table; paid before the guests leave (pay later) |

Pickup orders are paid first, online with Stripe Checkout on the
restaurant's own connected account (direct charge, like the card
guarantee). Table orders from the QR code go to the kitchen at once and are
paid with the table's bill before the guests leave ("Pay later at the
table" below). Each dish can be ordered 1-20 times; an order has at most 30
lines.

## Flow

1. **Open?** `GET /locations/{id}/ordering` - the owner's policy is on, the
   tenant's Stripe account can take charges, ordering isn't paused, and the
   location is open (business hours / special days) with more than
   `lastOrderMinutes` left before closing.
2. **Order:** `POST /order` (payment-intent). Prices, names and VAT come from
   the menu table; the request only names dishes and quantities. The order is
   written `pending_payment` with a TTL backstop, then the Checkout Session
   is created. The site sends `embedded: true`: the session is `ui_mode:
   elements` and the answer carries `clientSecret` + `stripeAccount`; the
   site's own "Betala" step shows Stripe's Payment Element (card data stays
   in Stripe's iframes, PCI SAQ A) styled with the site's tokens, and
   confirms it there. Without `embedded` (older sites) the browser goes to
   hosted Checkout at `checkoutUrl`. Same session, events and settling.
3. **Paid:** the site (or Stripe, after a redirect method such as Swish,
   Klarna or a 3-D Secure redirect) opens `/bestallning/<locationId>/<orderId>`. The
   page polls `GET /locations/{id}/order-status/{orderId}` with the status
   token; while pending, get-order asks Stripe itself. The webhook
   (`checkout.session.completed`, `payment_status=paid`) does the same - the
   first one wins (conditional write). Confirming assigns the day's number
   (`COUNTER#<date>`), sets `readyEstimate = now + prepMinutes` and the
   `order_confirmed` notice.
4. **Messages:** the Order table's stream (filtered on a notice) runs the
   notification function:
   - `order_confirmed` - receipt e-mail only (paid)
   - `order_started` - the confirmation SMS when staff press "Påbörja"
     (e-mail when no SMS can be sent). `readyEstimate` is set again from
     that click.
   - `order_ready` - SMS "ready to collect" (pickup only; e-mail when no
     SMS can be sent)
   - `order_cancelled` - e-mail only, with the refund amount. Never an
     SMS: cancellation SMS are for table bookings.
   - `order_refunded` - e-mail only: the owner refunded (part of) the
     order without cancelling it, with the amount and the owner's message.

   Order SMS are short and plain, always one SMS part (max 160
   characters, no link - the link is in the receipt e-mail):

       Roma: Beställning #12 bekräftad. Klar ca 18:40. Adress: Götgatan 1, Stockholm
       Roma: Beställning #12 bekräftad. Serveras till bord 5.
       Roma: Beställning #12 är klar att hämta. Adress: Götgatan 1, Stockholm
5. **Kitchen:** the admin's Beställningar screen lists the day's paid
   orders. Two clicks: "Påbörja - skicka sms" (`preparing`, the
   confirmation SMS) and "Klar - skicka sms" (`ready`, pickup) / "Klar att
   servera" (table), then `completed`; or `cancelled` (refunds what is left
   of the order with Stripe). The owner can also refund part or all of the
   card payment without cancelling ("Återbetala", POST
   `.../orders/{orderId}/refund`, also on completed orders): paymentStatus
   `partly_refunded` / `refunded`, one Stripe key per refund, rules in
   `shared/refunds.py`.

6. **Kitchen printer** (optional, Star CloudPRNT, e.g. mC-Print3 on
   Ethernet): the order table's stream runs `print-jobs`, which puts one
   auto job per paid order in the `print` table (conditional put, so a
   retried record never prints twice) when the location has auto print on.
   The printer polls `POST /print/cloudprnt/{deviceId}` every few seconds,
   fetches the ticket (StarPRNT, rendered from the order at that moment:
   "Köksnota, ej kvitto", no prices, phone on pickup only, notes in bold,
   local time) and confirms it. Errors retry after 15/30/60/120 s, then the
   job is FAILED; a ticket printed on a second attempt says "KAN VARA
   DUBBLETT". A printed order that is cancelled prints "MAKULERAD #N". The
   admin's Beställningar screen shows "Skrivaren svarar inte" / "N
   outskrivna" and "Skriv ut igen"; settings are under Inställningar,
   Skrivare. Printing never runs in the order request path.

Abandoned checkouts: `checkout.session.expired` (30 min) marks the order
`failed`; the TTL (60 min) removes an order whose webhook never came.

## Pay later at the table

`POST /order` with `mode: table` and `payLater: true` (the site since
2026-11-17). A table order without `payLater` (an old cached site) is still
paid first, as above; it logs `legacy_table_pay_first` so we see when that
path can go.

1. **Placing.** Name and mobile required, e-mail optional, terms accepted;
   discount codes and gift cards are refused (400 `codes_not_for_table`:
   their holds are per order and a bill covers several orders). Turnstile,
   the IP and contact limits as for every order, plus 8 table orders per
   phone and day. The order is written `confirmed` with paymentStatus
   `unpaid`, its day number at once, `readyEstimate`, `payLater: true` and
   `serviceDate`; no TTL, no notice (no receipt: nothing is paid). In the
   same transaction it is added to the table's bill; a bill with 8 open
   orders or 5 000 kr refuses it (409 `table_bill_full`, "ask the staff").
   The kitchen, the printer (print-jobs reacts to the INSERT, the ticket says
   `BETALAS VID BORDET`), the kitchen display and the status page treat it
   like a paid order. Payment and kitchen progress are independent: an order
   can be `completed` and still `unpaid`.
2. **The service day.** `orders.service_date`: the local date whose opening
   hours contain now, so a bar open to 01:00 keeps one bill across midnight.
   The order's `date` (and number) stay the calendar date.
3. **The bill** (`TABLETAB#<serviceDate>#<tableId>`): every unpaid order of
   the table that day (`open_<orderId>` = {date, number, total, at}),
   `openCount`, `openTotal`. Staff see it in Beställningar (Notor), on the
   Layout floor view and as "Obetald" on the kitchen display. A bill from an
   earlier service day, or after closing, is `stale` (amber) until it is
   paid, cancelled or written off. Nothing is deleted.
4. **Paying with the phone.** Staff show a pay link
   (`POST .../orders/tabs/{serviceDate}/{tableId}/pay-link`, a QR code,
   signed, valid 12 h) or the guest uses "Betala notan" on their own status
   page. `GET /locations/{id}/table-bill` shows the bill (no names or
   contacts); `POST .../table-bill/pay {expectedAmount}` locks it
   (`paying` = {billId, orderIds, amount, at}) to the amount shown, writes
   `BILL#<billId>` (a snapshot of the orders) and creates one Checkout
   Session (Payment Element, key `bill-checkout-<billId>`, one line per
   order, metadata `kind: table_bill`). An order placed meanwhile is added
   to the table but not to the locked set: it stays for the next bill. A
   second guest gets 409 `bill_being_paid` for 120 s; after that the old
   session is expired at Stripe first (if Stripe says it was paid, it is
   settled instead), so nobody is charged twice. The same device can resume
   its session (`resumeBillId`, `resumeToken`).
5. **Settling.** The webhook (`checkout.session.completed` / async
   succeeded, paid) and the guest's pay page polling
   `GET .../table-bill/{billId}` both try; one transaction marks the bill
   `paid`, takes the orders off the table's bill and sets every order
   paymentStatus `paid`, `paymentMethod online`, `paymentIntentId` (the
   bill's), `paidAt`, `billId`. The first wins. Expired / failed sessions
   release the lock; the orders stay unpaid. The bill's `bill_paid` notice
   sends one receipt e-mail (every order's lines, VAT per rate, total,
   reference = billId) to the payer's optional e-mail; none per order.
6. **Paid outside Booqy.** `POST .../tabs/{serviceDate}/{tableId}/paid-outside
   {method, note?, orderIds?, expectedAmount}`: staff with Beställningar
   record that the guest paid with the restaurant's own card terminal,
   cash, Swish or other. paymentStatus `paid_outside`, `paidOutside`
   {method, note, by, at}, `paidAt`. Booqy issues no receipt for it (the
   restaurant's own register does).
7. **Write-off.** `POST .../write-off {reason, orderIds?, expectedAmount}`,
   owner only: `written_off` {reason, by, at}.
8. **Cancel.** Cancelling an unpaid order (staff, the usual status route)
   refunds nothing: paymentStatus `void`, it leaves the bill, and the guest
   (if they gave an e-mail) is told they paid nothing. An order paid outside
   Booqy or written off can't be cancelled (`settled_outside_booqy`).
9. **Refunds** stay owner only and per order: a bill-paid order is refunded
   on the bill's PaymentIntent, capped at the order's total, key
   `refund-<orderId>-<n>`. `charge.refunded` on a bill payment is a
   backstop: it completes a lost owner refund when exactly one order waits
   for one; anything else (a Stripe Dashboard refund) is recorded on the
   bill as `externalRefund` and logged.

Every staff action on a bill (pay link, paid outside, write-off, cancel
unpaid) writes `AUDIT#ORDER#<orderId>#<at>#<rand>` {action, by, at, amount,
method?, reason?} and an order history entry. `expectedAmount` must match
what the screen showed (else 409 `bill_changed`); a guest paying those
orders right now is stopped first (409 `bill_paid` / `bill_processing` /
`bill_being_paid` when that is not possible).

**Mismatch runbook.** If a bill payment arrives for an order that is no
longer unpaid (the lock should make this impossible), the bill is still
marked paid, the other orders are settled, the bill gets `mismatch:
[orderIds]` and the table bill a `mismatch` flag (owner sees "Kontrollera
notan"), and an ERROR log `{"bill":"mismatch"}` raises the error alarm.
Refund that order's amount on the bill's payment in the Stripe Dashboard,
then press "Klar" (`POST .../tabs/{serviceDate}/{tableId}/mismatch-checked`).

**Money reporting.** `unpaid` is not revenue anywhere. A bill paid online
counts as a normal paid order (Statistik on the order's day; the
accounting export on the day it was paid, `paidAt`). `paid_outside` is its
own figure in Statistik ("Betalt utanför Booqy", `paidOutsideRevenue`,
dishes counted) and is left out of the accounting export with a note per
day (the restaurant books it from its own register). `written_off` and
`void` count nowhere. Retention keeps `paid_outside` and `written_off`
like paid orders, never deletes an `unpaid` one, and keeps paid `BILL#`
records for the accounting years (the payer's e-mail removed after the
guest months); unpaid bills expire by their TTL.

## Record

`PK LOCATION#<locationId>`:

| SK | What |
|---|---|
| `ORDER#<date>#<orderId>` | the order |
| `OID#<orderId>` | pointer to the date (status page, webhook) |
| `COUNTER#<date>` | `lastNumber` - the day's order numbers |
| `TABLETAB#<serviceDate>#<tableId>` | a table's open bill (pay later): `open_<orderId>`, `openCount`, `openTotal`, `paying`, `lastPaidAt`, `mismatch?`, `ttl` when empty |
| `BILL#<billId>` | one payment of a table's bill: orders snapshot, `amount`, `vat`, `stripeAccountId`, `checkoutSessionId`, `paymentIntentId`, `payerEmail?`, `status` paying / paid / expired / failed, `notice` bill_paid, `ttl` until paid |
| `AUDIT#ORDER#<orderId>#<at>#<rand>` | a staff action on an unpaid order |

Order attributes: `orderId, tenantId, locationId, date, timezone, mode,
tableId, tableLabel, lines[{menuItemId, name, unitPrice, quantity, total,
vatRate}], totalAmount, vat[{rate, gross, net, vat}], currency,
customerName, customerPhone, customerEmail, notes, language, prepMinutes,
status, paymentStatus, number, stripeAccountId, checkoutSessionId,
paymentIntentId, refund, readyEstimate, confirmedAt, readyAt, completedAt,
cancelledAt, notice, history, version, ttl (pending only)`; pay later adds
`payLater, serviceDate, billId, paidAt, paidOutside {method, note, by, at},
writtenOff {reason, by, at}` and paymentStatus `unpaid`, `paid_outside`,
`written_off`, `void`. Amounts are öre.

## VAT

| What | Rate |
|---|---|
| Alcohol (menu item `alcohol: true`) - table orders only | 25 % |
| Food and soft drinks served at the table | 12 % |
| Takeaway food and soft drinks, 2026-04-01 - 2027-12-31 | 6 % |
| Takeaway food and soft drinks, otherwise | 12 % |

Alcohol can't be ordered for pickup (a restaurant may serve alcohol, not
sell it to take away). Whether a restaurant needs a certified cash register
(kassaregister) for these sales is the restaurant's matter with its
accountant; the receipt shows seller, org.nr, lines, VAT per rate and total.

## QR codes

One code per table of the ACTIVE published floor plan (`shared/table_qr.py`).
The code is a random 128-bit key (22 characters) stored in the location's
partition of the order table; it carries no table id, so it can't be guessed
or edited into another table, and a code of another tenant or location is
simply unknown there.

- `GET /locations/{id}/orders/table-codes` (staff/owner): every published
  table with its code, current name and the URL to print
  (`/bestall?bord=<code>`); codes are created on first use. The admin shows
  them in Layout (select a table, or "QR-koder" for all) and under
  Inställningar -> Beställning, prints A6 cards or an A4 sheet (four per
  page) and downloads PDF, PNG or SVG.
- `POST .../table-codes/{tableId}/regenerate`: new code for a stolen sticker;
  the old key is kept with `revokedAt`.
- The guest's `GET /locations/{id}/ordering?table=<code>` and `POST /order`
  resolve the code against the active floor plan: the name follows renames
  and moves; 400 `table code is invalid` (unknown), `table code revoked`
  (regenerated), `table removed` (no longer in the plan; a new table that
  reuses the number is a new element with a new code). A draft layout never
  counts. Opening hours, pauses and "ordering at the table" off still close
  ordering as before.
- The order stores `tableId` and the table's name at order time
  (`tableLabel`); staff screens, the kitchen ticket and the guest messages
  show it ("Bord 7" for a number, a named table as it is).
- Codes printed before 2026-11-13 (`payload.signature`, name inside) still
  work, with the name from the floor plan, until the table's code is
  regenerated once or the table is removed.

## Security

- Public routes: prices never from the client; Turnstile + per-IP and
  per-contact limits on `POST /order`; the status page needs the HMAC token.
- Webhook: Stripe signature; metadata + connected account + session id +
  amount must all match the order (or the bill).
- Table bills: the guest routes need the staff pay link (signed, 12 h) or a
  pay-later order's status token; the bill view carries no names or
  contacts; 20 pay attempts per IP per 10 minutes.
- Staff routes: tenant from the JWT, staff limited to their location, status
  writes conditional on the status the screen showed.
