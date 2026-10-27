# Per-customer websites: plan

Status 2026-10-27: Phase 1 done (site-kit in customer-front-end); Phases
3 to 6 built (see "Build design" below), waiting for the owner's GitHub App
registration before the end-to-end test; Phase 2 (Figma) waits for the
owner. Owner-approved direction:
every restaurant gets its own front-end app in its own repo, designed in
Figma and coded with Claude, with all booking/ordering/payment logic in one
shared package. Guests only ever see the restaurant's own domain in the
address bar (emails, SMS sender and the API stay on Booqy).

## Goals

1. A new customer in Booqy Console gets a repo, a working site and a dev
   preview automatically, with no manual GitHub or AWS work.
2. Each site can look completely different (Figma per customer).
3. Booking, ordering, catering, payments, GDPR and analytics are written
   and tested once (`@booqy/site-kit`) and every site gets fixes through
   update PRs.
4. The browser address is always the restaurant's domain in production.
5. Customers are isolated: one site's repo can only deploy that site.

Not goals: a drag-and-drop site builder, per-restaurant email domains, the
API on the restaurant's domain.

## How it fits together

```
Figma (Booqy design system + one file per customer)
   │  Code Connect: Figma component  ->  <BookingForm />, <Menu />, ...
   ▼
GitHub org BookingSystemAdvancedOrg (private repos, prefix site-)
   ├── site-kit        shared package (@bookingsystemadvancedorg/site-kit, GitHub Packages)
   ├── site-template   starting point, today's customer site on site-kit
   └── site-<slug>     one per customer, created from the template
          dev  -> builds -> dev account  S3 sites/<tenantId>/  -> <slug>.dev.booqy.se
          main -> builds -> prod account S3 sites/<tenantId>/  -> restaurant's domain
   ▼
CloudFront multi-tenant distribution (already exists)
   one distribution tenant per restaurant domain, origin path sites/<tenantId>/
```

## Phases

Each phase ends with something working in dev. Order matters: 1 before 3,
2 can run in parallel with 1.

### Phase 0: owner setup (one time, owner)
1. GitHub: decided 2026-10-27, no new organisation. Customer sites live
   in the existing `BookingSystemAdvancedOrg` as **private** repos named
   `site-<slug>` (plus `site-kit` and `site-template`), on the **Free**
   plan (unlimited private repos, 2,000 Actions minutes a month). Upgrade
   to Team only for enforced reviews/protected branches or more minutes.
   They can move to their own organisation later (repo transfer keeps
   history).
2. Approve the GitHub App (Phase 3) once it's registered: name "Booqy
   Sites", installed on **selected repositories** (the `site-*` repos)
   only, never on the platform repos.
3. Figma: a Professional (or higher) team for Code Connect and the Booqy
   design system library; authorise the Figma connector in Claude Code
   (`/mcp`).
4. D1 and D2 are decided (below).

### Phase 1: `@booqy/site-kit` (code)
Extract everything that isn't "look" from customer-front-end into a package.
- **Contents:** the API client and tenant resolution (today's `api.ts`,
  `site.ts`, `SiteProvider`), the flows as components (BookingFlow, Menu,
  OrderFlow/Cart, Checkout with Stripe Elements, Catering request,
  OpeningHours, AddToCalendar, MarketingConsent, GuestDataNotice, cookie
  notice, legal pages), hooks for headless use, analytics, the theme
  tokens (CSS variables: colours, fonts, radius, spacing).
- **Styling:** components ship unstyled-but-sensible defaults driven by
  CSS variables, so a site restyles them without forking.
- **Quality:** the existing customer-front-end tests move with the code;
  semver releases to GitHub Packages from CI; a changelog.
- **Proof:** customer-front-end itself is rebuilt on site-kit and keeps
  working. This becomes `site-template`.
- Done when: today's test restaurant runs on site-kit in dev with no
  visible change and all tests green.

**Done 2026-10-27** (customer-front-end `ed54324` on dev). What moved where:
- **Package:** `customer-front-end/packages/site-kit` as an npm workspace,
  `@booqy/site-kit` 0.1.0, own package.json, README and CHANGELOG. `npm run
  build -w @booqy/site-kit` type checks and writes ESM, types and
  `site-kit.css` to `dist/`; `publishConfig` points at GitHub Packages but
  nothing is published. Exports: `.`, `./testing` (WithSite, siteConfig,
  json) and `./styles.css`. Moves to its own private repo `BookingSystemAdvancedOrg/site-kit` with Phase 3.
- **Into site-kit** (same folder layout as before, `src/shared` and
  `src/features`): `api`, `site`, `SiteProvider`/`SiteContext`,
  `locationApi` (openStatus), `layoutApi`, `bookingTerms`, `phone`,
  `calendar`, `storageRegistry`, `theme` + `ThemeToggle`, `visits`
  (analytics), `useLocationInfo`, `useStickyCart`, `payment/*` (Stripe),
  AddToCalendar, MarketingConsent, GuestDataNotice, DateInput, PhoneInput,
  FlowSteps, Celebrate, Plate, OpeningHoursModal; features `boka`,
  `bokning`, `meny` (now `Menu`, without the hero), `bestall`, `catering`,
  `erbjudande`, `juridik`; the test fixtures (`src/test`). Flows are
  exported as `BookingFlow`, `ManageBooking`, `OrderFlow`, `OrderStatus`,
  `CateringFlow`, `CateringRequest`, `PrivacyPolicy`, `CookiePolicy`,
  `BookingTermsPage`; API clients as namespaces (`bookingApi`, `orderApi`,
  `cateringApi`, `menuApi`, `offerApi`, `layoutApi`).
- **Settings:** `configureSiteKit({ apiBaseUrl, devTenantSlug,
  menuImageBaseUrl, paymentFonts })` replaces `import.meta.env` inside the
  kit; the site calls it in `main.tsx` from its `VITE_` variables.
- **Styles:** `styles.css` = the old cascade in the same order (offer,
  components (was customer.css), world, privacy, payment, catering, date
  input, theme (tokens and base from index.css, light/dark)). Rules that
  only style the shell (header, mobile menu, hero, footer, contact page)
  moved to the site's `src/site.css`; the fonts (`@font-face`, woff2) stay
  with the site.
- **Stays in the app (future site-template):** `App.tsx` routes,
  `CustomerLayout`, `BooqyWordmark`, `MenyPage` (hero + `<Menu />`),
  `KontaktPage`, `OmOssPage`, `index.css` (fonts), `site.css`, assets.
- **Tests:** 343 in the package (all moved tests plus the public API and
  SiteProvider), 5 in the app (App on SiteProvider, CSP vs. storage
  registry). Root `npm test`, `npm run lint`, `npm run build` cover both, so
  the CI workflow is unchanged.
- **Proof:** production builds before and after against the dev test
  restaurant, start, menu, boka, beställ, checkout, catering, kontakt,
  integritetspolicy, cookies, bokningsvillkor plus mobile menu, opening
  hours and theme toggle, at 375/768/1280 px in light and dark: pixel
  identical.

### Phase 2: Booqy design system in Figma (design + code)
- A Figma library with the same components as site-kit, with variants and
  variables matching the theme tokens.
- **Code Connect** maps each Figma component to its site-kit component and
  props, so Claude reads a customer's Figma and places real components.
- A starter file "Booqy site - start" that a new customer's design is
  duplicated from.
- Done when: Claude turns a sample Figma page into site-kit code with the
  real components, no hand-written booking logic.

### Phase 3: automatic repo per customer (infrastructure + application + console)
- **GitHub App** "Booqy Sites" in `BookingSystemAdvancedOrg`, installed
  on selected repositories only. It creates `site-<slug>` repos and
  manages only repos named `site-*`; the `site-repo` Lambda refuses any
  other name. Its private key lives in Secrets Manager
  (`booqy/<env>/github-app-sites`, in both accounts), never shown.
- **Onboarding step:** after the tenant is active, the onboarding workflow
  calls a new Lambda `site-repo`:
  1. create `site-<slug>` from `site-template`;
  2. write `booqy.json` (tenantId, slug, API URL per environment, public
     site keys);
  3. create branches `dev` and `main` (only these two);
  4. set the repo variables for deploy (role ARNs, bucket, distribution);
  5. trigger the first deploy, so the site works at once.
  Idempotent and resumable like the other onboarding steps; a failure
  shows on the tenant page with Retry.
- **Offboarding:** archive the repo (never delete), stop deploys, remove
  the site folder after the retention period.
- Done when: "New customer" in the dev console creates a repo whose first
  deploy is live within a few minutes.

### Phase 4: deploy per customer (infrastructure)
- **One bucket per environment** for sites (or a `sites/` prefix in the
  existing customer bucket), one folder per tenant: `sites/<tenantId>/`.
- **Distribution tenants** route to their own folder through the origin
  path (`/sites/{{tenantId}}`, a tenant parameter). Verify first that
  multi-tenant distributions accept parameters in the origin path; the
  fallback is a CloudFront Function that rewrites the path from the
  tenant parameter.
- **GitHub OIDC deploy role per customer**, trusted only for
  `repo:BookingSystemAdvancedOrg/site-<slug>`, allowed to write only
  `sites/<tenantId>/*`. Branch `dev` may assume only the dev account's
  role; branch `main` only the prod account's. This is an IAM change (new
  roles whose trust is limited to `site-<slug>` repos), owner-approved
  before it's built; the existing OIDC/deploy roles stay untouched.
- **Caching:** index.html no-cache, hashed assets immutable, as today, so
  no invalidations are needed.
- **Rollback:** redeploy the previous commit from the repo's Actions tab
  (per customer, seconds).
- Done when: two customers deploy independently and neither can write the
  other's folder (tested).

### Phase 5: restaurant domain only (infrastructure + console)
- Prod has no `<slug>.booqy.se` (already `tenant_subdomains_enabled =
  false` in prod).
- **Go-live gate:** the production deploy job itself asks Booqy whether
  the restaurant has an attached, verified domain and refuses to deploy
  otherwise (works on GitHub Free, where private repos have no branch
  protection). No domain, no prod site.
- **Dev preview** `<slug>.dev.booqy.se`: `noindex`, canonical to the
  restaurant's domain, only for the restaurant and us.
- Links in emails and SMS that open the site use the restaurant's domain
  (already the behaviour once `primaryDomain` is set).
- Done when: a prod site is only reachable on its own domain.

### Phase 6: console Website tab (console)
On the customer's page, tab **Website**:
- repo link and status (created, last deploy dev/prod, version, who
  pushed);
- "Kom igång lokalt": `git clone …`, `npm install`, `npm run dev`;
- the `.env.local` values (API URL, tenant ID, Stripe and Turnstile site
  keys). None are secret, but they're hidden behind **Visa** with a copy
  button;
- "Ge åtkomst": invite a GitHub user to this repo only (through the App);
  no tokens are ever shown or stored;
- buttons: open repo, open dev preview, redeploy, archive.
- Done when: an operator can get from "New customer" to a running local
  dev server using only this tab.

### Phase 7: pilot and runbook
- One pilot customer end to end: Figma -> approval -> Claude -> dev
  preview -> approval -> main -> own domain.
- Write `docs/CUSTOMER-SITE-RUNBOOK.md` (the 18 steps agreed with the
  owner) and update `TENANT-ONBOARDING.md`.
- Dependabot in every site repo for `@booqy/site-kit`, with auto-merge of
  patch updates once CI is green.

## Build design (Phases 3 to 6, built 2026-10-27)

Operating it: infrastructure `docs/CUSTOMER-SITE-RUNBOOK.md`.
- **Repos:** `site-kit` (package `@bookingsystemadvancedorg/site-kit`:
  GitHub Packages only accepts the owner's name as npm scope, so not
  `@booqy`), `site-template` (customer-front-end's shell on the published
  kit), `site-<slug>` generated from it. Private, branches `dev` and `main`.
- **booqy.json** (in each site repo, public values only):
  `{slug, name, environments: {dev|prod: {tenantId, apiBaseUrl,
  menuImageBaseUrl, stripePublishableKey, turnstileSiteKey, previewUrl?,
  domain?}}}`. Each environment's `site-repo` writes only its own section,
  on `dev`. The build picks the section with `BOOQY_ENV`; outside prod it
  adds `noindex` and a canonical link to `environments.prod.domain`.
- **Repo variables:** `DEV_|PROD_` + `AWS_ROLE_ARN`, `SITE_BUCKET`,
  `TENANT_ID`, `API_BASE_URL`. No secrets anywhere in a site repo.
- **Serving:** a new sites bucket per account (the shared customer bucket
  is synced with `--delete`), origin `tenant-site` with origin path
  `/sites/{{tenantId}}` on the multi-tenant distribution (verified: AWS
  supports tenant parameters in origin paths; no CloudFront Function).
  `per_tenant_sites` switches the default behaviour per environment, so
  restaurants on the shared build keep working until they have a site.
- **Go-live gate:** `GET /site-config?tenantId=` returns `tenant.domain`
  (own domain, kind custom, certificate issued); the main-branch deploy
  refuses without it.
- **OIDC:** `site-repo` sets each site repo's OIDC subject to
  `repository_owner_id, repository_id, ref`, and the deploy role trusts
  exactly that (dev branch in dev, main in prod). A site repo's token can
  then never match a platform role's `repo:...` pattern, which matters
  because outside developers get write access to site repos.
- **Onboarding/offboarding:** website step after ActivateTenant (failure:
  alert, tenant stays active, Retry on the Website tab); archive step in
  offboarding. In prod the step creates the repo and then, through dev's
  `booqy-sites-prod-link` role (lambda:InvokeFunction on dev
  `platform-tenants` only), a `siteTask` that creates or adopts the empty
  dev tenant; that tenant's own dev onboarding runs the same step, which in
  dev only sets up a repo prod already created (else a dry run, status
  `simulated`; `test-` slugs excepted).

## Decisions for the owner

- **D1, environments and data. Decided 2026-10-27 (owner correction; replaces
  the "test copy" idea and the first dev-first version): the PROD console is
  the only place a site repo is born; nothing is ever copied between
  environments.**
  1. Creating a customer in the **prod** console creates ONE private repo
     `site-<slug>` (from `site-template`, branches `dev` and `main`) set up
     for both environments: a prod deploy role (prod account, `main` only)
     and, through the dev account, a matching **empty** dev tenant (same
     slug, no data copied; the restaurant and developers enter test data in
     the dev admin), its `<slug>.dev.booqy.se` and a dev deploy role (dev
     account, `dev` only). Both roles under `/booqy-sites/` with the
     permissions boundary, each limited to `sites/<tenantId>/*` in its own
     account.
  2. Prod reaches dev through one narrow role in the DEV account
     (`booqy-sites-prod-link`), assumable only by prod's `site-repo`, that
     may only invoke dev `platform-tenants` with a site task (create or
     adopt the dev tenant by slug, archive its dev site). Dev never gets
     rights in prod. Owner-approved 2026-10-27.
  3. The **dev** console only simulates: the website step is a dry run that
     shows what prod would create, and creates nothing; except slugs
     starting with `test-` (repos `site-test-<slug>`) for testing the
     automation. The dev console shows a prod-born repo read-only.
  4. Push to `dev` deploys to the dev account; merge `dev` -> `main` deploys
     to the prod account, only with the restaurant's own domain attached and
     verified (the deploy job asks the Booqy API).
  5. GitHub plan Free: no environment approvals on private repos, so the
     workflow picks repo-level `DEV_*` / `PROD_*` variables by branch, and
     each role's OIDC trust holds the branch condition. No approval button
     before prod; the domain gate is the prod check.
- **D2, who designs. Decided 2026-10-27: Booqy designs every customer's
  site in Figma.** Restaurants review and approve through a Figma view
  link; no outside designers, so the design system stays internal.

## Risks
- **Template drift:** sites are copies; only site-kit updates reach them
  automatically. Keep the template thin and put anything shared in
  site-kit.
- **A bad site-kit release** reaches every site through update PRs. CI in
  each repo must pass before merge; patch-only auto-merge.
- **GitHub App key leak:** the app lives in the same organisation as the
  platform, so it is installed on selected `site-*` repos only and never
  gets "all repositories". Key stored in Secrets Manager, read only by
  `site-repo`, rotated yearly. Moving sites to their own organisation
  later removes this risk entirely.
- **CloudFront parameter in origin path:** verify in Phase 4 before
  relying on it (fallback described).
- **Cost:** GitHub Free to start (Team per seat later if needed), Actions about 1-2 minutes per deploy,
  S3 and CloudFront per site negligible.

## Rough effort
Phase 1: 3-5 days. Phase 2: 2-4 days (most is design). Phase 3: 2-3 days.
Phase 4: 2-3 days. Phase 5: 1 day. Phase 6: 1-2 days. Phase 7: 1-2 days.
