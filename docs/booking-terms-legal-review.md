# Booking terms: legal review

No wording makes terms "non-sueable". What we can do is show clear,
reasonable fees before the guest books, get an active acceptance and keep
the record. This note lists what a lawyer (Swedish consumer law) should
review before the card guarantee is sold widely.

## What the product does

- The guest site's "Bokningsvillkor" page (customer-front-end
  `src/features/juridik/TermsPage.tsx`) is generated from the location's live
  policy (`shared/guarantee.py`: `minPartySize`, `noShowFeePerPerson`,
  `lateCancelFeePerPerson`, `cancelCutoffHours`) and its grace period
  (`gracePeriodHours`, 15 to 60 min). Restaurants without a guarantee get a
  short version.
- The wording lives in one function, `src/shared/bookingTerms.ts`; the email
  uses the same wording in `shared/messages.py` (`guarantee_lines`). Both are
  unit-tested.
- Before booking: the fees for the exact party size are shown next to an
  unticked "Jag godkänner bokningsvillkoren" checkbox that links to the terms;
  the booking cannot be sent without it. The card step repeats them.
- At booking a card is saved (Stripe SetupIntent), 0 kr is charged. The
  amounts and the cutoff are snapshotted on the reservation; later policy
  changes never apply to an existing booking.
- The confirmation page and email repeat the fees and the free-cancellation
  deadline as an exact date and time (restaurant's time zone).
- No-show fee: only when staff mark the booking as a no-show. Late-cancel fee:
  only when the guest cancels inside the cutoff (the cancel page shows the fee
  and asks for a second confirmation). Late arrival is never charged. Staff
  can waive or refund. The guest gets a receipt by email.

## For the lawyer

1. **Reasonableness of the fees** (avtalsvillkorslagen 1994:1512, 36 §
   avtalslagen). Fees are capped at 5 000 kr per person and the cutoff at 168
   hours, but each restaurant sets its own. Is a per-person no-show fee a fair
   estimate of the restaurant's loss? Should Booqy cap fees lower, or guide
   owners towards a level (for example, a share of an average bill)?
2. **Penalty vs. damages.** The fee is a fixed sum, not actual loss. Is it
   enforceable against a consumer as a vite/schablonersättning, and must the
   restaurant reduce it when the table was resold?
3. **Distance-contract rules** (lagen 2005:59 om distansavtal). Restaurant
   bookings for a specific date are normally exempt from the right of
   withdrawal (2 kap. 11 § 12). Confirm, and whether anything must still be
   stated.
4. **Clarity and timing.** Fees are shown before booking, accepted with an
   unticked checkbox and confirmed in writing. Is this enough "particular
   notice" (särskild upplysning) for a burdensome term, or should the fees be
   in the checkbox label itself?
5. **Illness and emergencies.** The terms say the restaurant "kan efterskänka
   avgiften vid sjukdom eller liknande", at its discretion. Is a discretionary
   waiver acceptable, or does consumer law (force majeure, the general
   reasonableness rule) require a right to a waiver in some cases?
6. **Late arrival.** The terms promise that arriving late is never charged.
   Staff must therefore never mark a guest who came late as a no-show. Is the
   admin's no-show flow (it asks whether to charge) enough, or should it warn?
7. **Who the contract is with.** The terms name the restaurant's legal entity;
   fees go to the restaurant's own Stripe account. Booqy is only the platform.
   Check that this is clear and that Booqy carries no liability for the fee.
8. **Disputes.** The terms send questions to the restaurant. Should they name
   Allmänna reklamationsnämnden (ARN) and the EU ODR platform, as many
   consumer terms do?
9. **Card scheme rules.** Stripe/Visa/Mastercard rules for no-show charges on
   a saved card (merchant-initiated transactions, disclosure). Chargebacks
   are won with the snapshot, the accepted terms and the confirmation email.
10. **Record of acceptance.** We store the fee snapshot and when the terms
    were accepted (`termsAcceptedAt`), not the text version shown. Should the
    terms text (or a version id) be stored on the reservation as well?
