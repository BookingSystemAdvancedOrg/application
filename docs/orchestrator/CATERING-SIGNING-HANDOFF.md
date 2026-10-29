# Catering signing: hand-off for the provider decision

**Implemented: Scrive, 2026-10-28** (`shared/signing_scrive.py`, tests in `tests/test_signing_scrive.py`). Still open from "Done when": dev end-to-end on api-testbed.scrive.com once the owner has put the credentials in `dev-catering/signing-provider` (steps in `infrastructure/docs/PLATFORM-SETUP.md` section 5), and replacing the modelled responses in `tests/fixtures/scrive/` with real testbed ones. Delete this file when both are done. 2026-10-29: credentials are in; Scrive has not yet switched on se_bankid for the testbed account (start answers `authentication_to_sign_method_disabled`, asked via their support). Meanwhile dev may sign with `"signMethod": "standard"` in the secret; the end-to-end test counts only with BankID.

## Trigger
When the owner says something like **"start on the BankID signing for catering, we chose Scrive"** (or **SEB**), this file is the brief. Read it, `STATUS.md`, `CONVENTIONS.md` and `PLAN.md` (issue 5), then build the chosen adapter as described below. Do not ask again which provider to use. Ask only for what is listed under "Owner provides" if it is missing.

Background: on 2026-10-22 the owner approved the catering plan, but the provider is chosen after meetings with Scrive and SEB. Idura (the plan's first recommendation) is not on the shortlist. Until then, everything provider-neutral is built and the dev environment signs with the BankID simulator.

## What exists (built in issue 5 M4, 2026-10-23)
- `application/shared/signing.py`: the provider interface and the factory. The provider is the `provider` field in the secret at `SIGNING_PROVIDER_SECRET_ARN`: `simulator` (dev; `{"startKind": "redirect"}` makes it behave like a provider with its own signing page), `bankid_direct` (older name `bankid`, wraps `shared/bankid.py`). Add `scrive` (or the SEB variant) as one more branch in the same functions.
- `application/shared/catering_signing.py`: the flow on top of it (restaurant first, then the customer; simple acceptance; evidence package; webhook re-fetch). Provider code never touches DynamoDB or S3.
- Signing order: the restaurant signs first (owner in admin, `POST …/restaurant-sign` + `…/status`), then the customer (`…/customer/sign` + `…/status`). `offer_sent` means "waiting for signatures"; `restaurantSignedAt` marks the restaurant's signature and only then is the customer notified. Per-party state lives on the **head item** `signing.signatures[role] = {name, personalNumberMasked?, role?, method, signedAt, evidenceKey}` (the history table is append-only, so not on `OFFER#vNNNN`). A new offer version cancels the open order and opens a new one.
- Offer PDF: `functions/catering-document` (WeasyPrint, PDF/A-3b) renders the version to `<tenant>/offers/<id>/vNNNN.pdf` (tagged `retention=offer`); its SHA-256 is `signing.pdfSha256` and what both parties sign (BankID `userNonVisibleData`).
- Webhook `POST /webhooks/signing/catering?l=&r=&t=` (`functions/catering-signing-webhook`): verification delegated to the adapter, dedupe on `AUDIT#SIGNING#<eventId>` (checked first, written after the change), always re-fetch with `status()` before recording.
- Locked storage: `signed/<id>/vNNNN/` holds each party's evidence, `offer.pdf` (the pinned S3 version `signing.pdfVersionId`, hash-checked) and `manifest.json` with per-object COMPLIANCE (7 years prod, 1 day GOVERNANCE dev), all written only when the customer's signature makes the contract binding. Until then the restaurant's evidence sits unlocked next to the PDF in `offers/` (tagged, expires after 13 months).
- Admin and customer UIs handle both start kinds: `redirect` (open the provider's page, back with `?signering=klar`, then one status call) and `bankid` (QR or app on the same device, polled every second).
- Fallback without BankID (simple acceptance) is provider-independent and works (off by default per restaurant).

### Interface as built (differences from the first draft)
The "order_ref" is the **order dict** the adapter returns from `create_order`; the caller stores it as `signing.order` and passes it back, and the functions return the changed dict. Keep provider ids, per-party URLs and tokens inside it.
```
create_order(request, offer, pdf_bytes, parties, callback_url) -> order    # parties: [{role, name, email}] in signing order
start(order, role, end_user_ip, return_url) -> (result, order)            # result {"kind": "redirect", "url"} | {"kind": "bankid", "autoStartToken", "qrData"}
collect(order, role) -> (result, order)                                   # {"state": "pending"|"signed"|"failed", "hint"?, "signer"?, "evidence"? {filename: bytes}}
qr(order, role) -> str | None
cancel(order) -> None
status(order) -> {"complete": bool, "parties": [{role, signedAt?, method}]}
fetch_evidence(order) -> {"<filename>": bytes}                            # whole-order files (Scrive: the sealed PDF), stored with the package
verify_webhook(order, headers, raw_body, query) -> {"eventId"} | None     # None = 401
```
`callback_url` already carries `?l=<locationId>&r=<requestId>`; `create_order` adds the per-order random `t` token. For a redirect provider, `collect` after the return reads the party's state from the provider. `return_url` comes from the caller (admin page for the restaurant, the guest's page for the customer). An offer that expires (catering-lifecycle) does not cancel the provider's order: lifecycle has no access to the signing secret; let the provider's own expiry (days to sign) match `offerValidDays`.

## If the choice is Scrive
Owner provides (never paste secrets into chat): a Scrive API account (testbed first: api-testbed.scrive.com), OAuth or personal access credentials put into the existing signing secret in Secrets Manager by the owner, the contract's price model, and the user-group setup per restaurant if Enterprise.
Build:
1. `shared/signing_scrive.py` (Document API v2): `create_order` uploads the PDF, sets parties with `sign_order` 1 (restaurant) and 2 (customer), `delivery_method` api, `authentication_method_to_sign` `se_bankid`; `start` returns the party's `api_delivery_url` as `redirect`; `cancel` calls cancel; `status` maps parties; `fetch_evidence` downloads the sealed PDF (with evidence attachments).
2. Callbacks are not signed by Scrive: put a per-order random token in the callback URL (`?t=`), compare in constant time, then re-fetch the document. Never trust the callback body.
3. Secret fields: `provider: "scrive"`, base URL, credentials. No new IAM if the secret ARN is unchanged.
4. Tests with recorded responses (no network): create, start, callback token mismatch, out-of-order and duplicate callbacks, sealed PDF stored with lock, cancel on new version.
5. Dev on the testbed, then the owner switches prod's secret.
Ask Scrive in the meeting: API price per document and per BankID signature, monthly minimum, whether one Booqy account can serve many restaurants (user groups, Enterprise?), whose name the BankID app shows, callback authentication options, data location, sealed PDF format (PAdES?) and verification without Scrive (KSI lock-in), sandbox access, onboarding time, DPA.

## If the choice is SEB (direct BankID RP)
Owner provides: a signed BankID RP agreement with SEB, the RP certificate (.p12 and password) put into the signing secret by the owner, written confirmation from SEB that Booqy may sign on behalf of the restaurants on its platform (BankID forbids a "BankID-växel": one RP certificate signing or identifying for other companies). **Do not go live without that confirmation.**
Build:
1. Adapter around the existing `shared/bankid.py` (RP API v6, mTLS): `create_order` stores the PDF hash; `start` calls `/sign` with `userVisibleData` (short Swedish summary: restaurant, date, guests, total) and `userNonVisibleData` = PDF SHA-256, returns `bankid`; `collect` polls; `status` from our own records; `cancel` calls `/cancel`; `verify_webhook` returns None (no webhooks; polling only).
2. Evidence package per signature under `signed/<requestId>/vNNNN/`: the PDF, `completionData` (signature, OCSP response, user, device), our audit entries, a manifest with hashes. Locked with COMPLIANCE.
3. Decide with the owner whether to add a PAdES seal (needs an organisation seal certificate; extra cost) or keep the evidence package only. Default: evidence package only.
4. Certificate rotation: the p12 lives in the secret; alarm 30 days before expiry (CloudWatch scheduled check). Test environment uses BankID's free test certificate.
5. Tests: sign, collect states, cancel, hash mismatch, rotation check, evidence manifest.
Ask SEB in the meeting: price (connection, monthly per certificate, per signature), the växel question above in writing, display name shown in the BankID app (Booqy or the restaurant), whether each restaurant needs its own agreement, onboarding time, certificate renewal routine, test environment.

## Done when
- The chosen adapter passes its tests; dev signs end to end with the real provider's test environment (restaurant first, then customer); the sealed or evidence files are in the locked bucket.
- `STATUS.md` records the provider decision with date; `PLAN.md` issue 5 updated; this file says "Implemented: <provider>, <date>".
- The simulator stays for dev and tests.
