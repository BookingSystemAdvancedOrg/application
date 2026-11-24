# Food orders

Guests order food online for pickup:

| Mode | Where the guest is | How they get the food |
|---|---|---|
| `pickup` | not at the restaurant - the restaurant's site, `/bestall` | collects at the counter when the "ready" SMS arrives |

Payment is always online with Stripe Checkout on the restaurant's own
connected account (direct charge, like the card guarantee): the guest pays
at once, and only a paid order reaches the kitchen, the printer and the
kitchen display. Each dish can be ordered 1-20 times; an order has at most
30 lines.

**No ordering at the table (owner decision 2026-11-17).** Ordering and
paying with a QR code at the table needs a certified register, so it is
off. A guest at the restaurant orders and pays at the counter in the
restaurant's own till; a guest with a booking arrives and pays there too.
- `POST /order` with `mode: "table"` or a `tableCode`, and
  `GET /locations/{id}/ordering?table=...`, answer **410
  `table_ordering_off`** with `message` "Beställ och betala i kassan i
  restaurangen." Guest sites send `/bestall?bord=<code>` (an old printed
  sticker) to a page that says the same, with links to the menu and booking.
- The table-codes routes (`.../orders/table-codes[/...]`) answer 410 too;
  `shared/table_qr.py` is gone. The `TABLEQR#...` items stay in the order
  table untouched, so the feature can return through a certified register.
- The ordering policy has no `table` option: a stored or sent `table` flag
  is accepted and ignored.
- Orders placed at a table before that keep `mode: "table"`, `tableId` and
  `tableLabel`: the order card, kitchen display, printed tickets, guest
  status page, SMS ("Serveras till bord 5"), refunds and Statistik ("Vid
  bordet") still show and handle them.

## Payment methods

Stripe's Payment Element with dynamic payment methods on the restaurant's
connected account offers what that account has activated, for SEK, on
that device and for that amount (shared/payment_methods.py):

| Method | How it shows | Settles | Refund |
|---|---|---|---|
| Card (Visa, Mastercard) | card form | at once | back to the card, 5-10 bank days |
| Apple Pay, Google Pay | wallet button on a device that has one (the site's host must be registered on the account: shared/payment_domains.py) | at once | as card |
| Swish | redirect to the app on a phone, QR code on a computer; 3 kr - 150 000 kr | at once | asynchronous, usually minutes; can fail (below) |
| Klarna | redirect to Klarna; pay later, pay now or installments, up to 150 000 kr | when Klarna approves | via Klarna, up to 180 days |

Stripe Link is off (`wallet_options.link.display = never`: its SMS code
goes to the Link account's phone, not the guest's). PayPal is not offered:
Stripe does not support it for direct charges on Connect.

The restaurant turns Swish and Klarna on in its own Stripe Dashboard
(Settings, Payment methods; accounts with the full Dashboard manage their
own methods). Admin's Betalningar card lists each method's status from the
account's capabilities (`GET /tenant` `stripe.paymentMethods`, kept current
by `account.updated` in platform-stripe-webhook).

**How it was paid:** when the order is settled, the PaymentIntent is read
on the account (`expand latest_charge`) and `paidWith` stored: `{method:
card | apple_pay | google_pay | swish | klarna | other, brand?, last4?}`
(no payer details). Best effort: without an answer the order still
confirms, without `paidWith` (receipt then says Kort (Stripe)). The
receipt, the refund e-mails ("via Swish, oftast inom några minuter"), the
guest's status page and admin's order card name the method.

**Only paid confirms:** an order is confirmed on `payment_status = paid`
only (checkout.session.completed, or async_payment_succeeded for a method
that settles later); `unpaid` on completed waits.

**A refund that fails** (`refund.failed`, Swish can refuse one after
accepting it): Stripe puts the money back on the restaurant's balance. The
refund stays recorded (the guest is still owed it), the failure is listed
once per refund id in `refundFailed` with a `refund_failed` history entry,
admin tells staff to pay the guest back another way, and the
`order_refund_failed` notice e-mails the restaurant (location e-mail, also
with staff e-mails off: the order may be from another day than the one on
the screen). The guest's status page gets `refundIssue: true` (no amounts)
and says the restaurant pays back another way instead of naming the
method. Stripe drops a failed refund from the charge's `amount_refunded`
while Booqy keeps it (the guest is owed it), so `charge.refunded` adds the
failed amounts back before comparing. The accounting
export books the balance coming back (`refund_failure`) to the OBS account
with the same advice. A Dashboard refund has no order metadata: the
webhook reads the PaymentIntent's.

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
       Roma: Beställning #12 bekräftad. Serveras till bord 5.   (past table orders)
       Roma: Beställning #12 är klar att hämta. Adress: Götgatan 1, Stockholm
5. **Kitchen:** the admin's Beställningar screen lists the day's paid
   orders. Two clicks: "Påbörja - skicka sms" (`preparing`, the
   confirmation SMS) and "Klar - skicka sms" (`ready`, pickup) / "Klar att
   servera" (past table orders), then `completed`; or `cancelled` (refunds what is left
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
   Skrivare. Printing never runs in the order request path. Who may do
   what: staff with Beställningar add printers, rename them, change their
   kind (Kök, Dryck, Annat) and test print; removing a printer, a new
   secret, the print settings and "Inga skrivare än" are the owner's.
   Booqy operators see each location's printers in Booqy Console
   (customer, Locations) and can add, rename, test print and remove one
   there (audited). A menu category with no "Skickas till" choice goes to
   Dryck when it is the drinks category, its name sounds like drinks
   ("Öl", "Te & kaffe", never "Salladsbar") or the dish is alcohol, else
   to Kök (shared/routing.py; each order line keeps its `categoryName`).

Abandoned checkouts: `checkout.session.expired` (30 min) marks the order
`failed`; the TTL (60 min) removes an order whose webhook never came.

## Record

`PK LOCATION#<locationId>`:

| SK | What |
|---|---|
| `ORDER#<date>#<orderId>` | the order |
| `OID#<orderId>` | pointer to the date (status page, webhook) |
| `COUNTER#<date>` | `lastNumber` - the day's order numbers |

Order attributes: `orderId, tenantId, locationId, date, timezone, mode,
tableId, tableLabel, lines[{menuItemId, name, unitPrice, quantity, total,
vatRate, category, categoryName}], totalAmount, vat[{rate, gross, net, vat}], currency,
customerName, customerPhone, customerEmail, notes, language, prepMinutes,
status, paymentStatus, number, stripeAccountId, checkoutSessionId,
paymentIntentId, refund, readyEstimate, confirmedAt, readyAt, completedAt,
cancelledAt, notice, history, version, ttl (pending only)`. Amounts are öre.

## VAT

| What | Rate |
|---|---|
| Alcohol (menu item `alcohol: true`) - past table orders only | 25 % |
| Food and soft drinks served at the table (past table orders) | 12 % |
| Takeaway food and soft drinks, 2026-04-01 - 2027-12-31 | 6 % |
| Takeaway food and soft drinks, otherwise | 12 % |

Alcohol can't be ordered for pickup (a restaurant may serve alcohol, not
sell it to take away), so with table ordering off it can't be ordered
online at all (`alcohol_not_for_pickup`). Whether a restaurant needs a certified cash register
(kassaregister) for these sales is the restaurant's matter with its
accountant; the receipt shows seller, org.nr, lines, VAT per rate and total.

## Security

- Public routes: prices never from the client; bot check (AWS WAF) + per-IP and
  per-contact limits on `POST /order` (10 per IP per 10 minutes); the status page
  needs the HMAC token.
- Webhook: Stripe signature; metadata + connected account + session id +
  amount must all match the order.
- Staff routes: tenant from the JWT, staff limited to their location, status
  writes conditional on the status the screen showed.
