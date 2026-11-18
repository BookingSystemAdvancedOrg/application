# Kom igång: when is a restaurant's setup finished?

One rule, computed only in the backend (`shared/setup_checklist.py`, served
by `functions/tenant-setup`). Booqy Admin shows the list it returns; Booqy
Console shows what it records on the tenant.

## The rule

A tenant's setup is **complete** when every applicable step is **finished**
across all of the tenant's locations.

- A step is **finished** when it is **done** or **skipped**.
- **Tenant steps** count once for the tenant. **Location steps** must be
  finished at **every** location of the tenant. A tenant with no locations
  is never complete.
- A step **applies** when the plan has its feature; steps without a feature
  always apply.
- **Skipped** means the owner pressed "Hoppa över" on Översikt (a
  `SETUPSKIP#` row), or used the step's own skip (printers: "Inga skrivare
  än"; Google: "Vi har ingen Google-profil"). Every step except `dpa` can be
  skipped.
- A step whose data can't be read counts as **open** (fail closed).

| Step | Scope | Applies when | Done when |
|---|---|---|---|
| `dpa` Godkänn personuppgiftsbiträdesavtalet | tenant | always | the current DPA version is accepted (cannot be skipped) |
| `hours` Lägg in öppettider | location | always | at least one day has opening hours |
| `menu` Lägg upp menyn | location | always | at least one active menu item |
| `printers` Kvittoskrivare | location | `ordering` | at least one printer |
| `layout` Publicera och aktivera en bordslayout | location | always | a layout version is active |
| `stripe` Slutför Stripe för kortbetalningar | tenant | always | the connected account can take charges |
| `staff` Bjud in personal | tenant | always | at least one staff user (system accounts excluded) |
| `google` Google-profil (valfritt) | location | `googleBusiness` | Google connected and the location linked |
| `phone` Telefonassistent | location | `phoneAgent` | all five sub-steps: namn, tider, nummer, koppla, prova |

Progress ("7/9" in the console) counts steps: finished steps / applicable
steps, a location step counting as finished only when it is finished at
every location.

## Recording and notifying

Every evaluation (an owner opening Översikt, a skip, the 15-minute sweep)
stores `PROFILE.setupState` when it changed. The first time the rule is true:

1. one transaction sets `PROFILE.setupCompletedAt` (only if not set),
   appends the audit row `setup_completed` and a `NOTICES` row;
2. then `Tenant.SetupCompleted` goes on the event bus (source
   `booqy.tenant-setup`) and one email to the operators (platform alerts
   topic);
3. Booqy Console shows the notice under the bell ("Setup finished:
   <restaurant>"), the progress in the customer list and the step states on
   the customer page.

A step reopened and finished again later only updates `setupState`; there is
never a second notice.

## Adding a step

Add a `Step` to `STEPS` in `shared/setup_checklist.py` (with the facts it
needs in `Facts`/`LocationFacts` and the loader in `functions/tenant-setup`).
It is then part of the rule, the admin list (the server sends the label) and
the console count. `tests/test_setup_checklist.py` fails for a step that
gives no state or that a fully set-up restaurant can't finish. In admin, give
it a link in `SETUP_LINKS` (`src/features/oversikt/overview.ts`) and a help
text (`setupHelp.test.tsx` requires one).
