# Food orders

Guests order food in two ways:

| Mode | Where the guest is | How they get the food |
|---|---|---|
| `pickup` | anywhere - the restaurant's site, `/bestall` | collects at the counter when the "ready" SMS arrives |
| `table` | at a table - scans its QR code, `/bestall?bord=<code>` | staff bring it to that table |

Payment is always online with Stripe Checkout on the restaurant's own
connected account (direct charge, like the card guarantee). Each dish can be
ordered 1-20 times; an order has at most 30 lines.

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

   Order SMS are short and plain, always one SMS part (max 160
   characters, no link - the link is in the receipt e-mail):

       Roma: Beställning #12 bekräftad. Klar ca 18:40. Adress: Götgatan 1, Stockholm
       Roma: Beställning #12 bekräftad. Serveras till bord 5.
       Roma: Beställning #12 är klar att hämta. Adress: Götgatan 1, Stockholm
5. **Kitchen:** the admin's Beställningar screen lists the day's paid
   orders. Two clicks: "Påbörja - skicka sms" (`preparing`, the
   confirmation SMS) and "Klar - skicka sms" (`ready`, pickup) / "Klar att
   servera" (table), then `completed`; or `cancelled` (refunds the whole
   order with Stripe).

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

## Record

`PK LOCATION#<locationId>`:

| SK | What |
|---|---|
| `ORDER#<date>#<orderId>` | the order |
| `OID#<orderId>` | pointer to the date (status page, webhook) |
| `COUNTER#<date>` | `lastNumber` - the day's order numbers |

Order attributes: `orderId, tenantId, locationId, date, timezone, mode,
tableId, tableLabel, lines[{menuItemId, name, unitPrice, quantity, total,
vatRate}], totalAmount, vat[{rate, gross, net, vat}], currency,
customerName, customerPhone, customerEmail, notes, language, prepMinutes,
status, paymentStatus, number, stripeAccountId, checkoutSessionId,
paymentIntentId, refund, readyEstimate, confirmedAt, readyAt, completedAt,
cancelledAt, notice, history, version, ttl (pending only)`. Amounts are öre.

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

`POST /locations/{id}/orders/table-codes` signs `{tableId, label}` with the
platform's link key (purpose `table-qr`), so a guest can't invent a table and
the label prints on the order without a layout lookup. Renaming a table
means printing a new code.

## Security

- Public routes: prices never from the client; Turnstile + per-IP and
  per-contact limits on `POST /order`; the status page needs the HMAC token.
- Webhook: Stripe signature; metadata + connected account + session id +
  amount must all match the order.
- Staff routes: tenant from the JWT, staff limited to their location, status
  writes conditional on the status the screen showed.
