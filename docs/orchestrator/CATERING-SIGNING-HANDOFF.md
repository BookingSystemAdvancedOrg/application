# Catering signing: hand-off for the provider decision

## Trigger
When the owner says something like **"start on the BankID signing for catering, we chose Scrive"** (or **SEB**), this file is the brief. Read it, `STATUS.md`, `CONVENTIONS.md` and `PLAN.md` (issue 5), then build the chosen adapter as described below. Do not ask again which provider to use. Ask only for what is listed under "Owner provides" if it is missing.

Background: on 2026-10-22 the owner approved the catering plan, but the provider is chosen after meetings with Scrive and SEB. Idura (the plan's first recommendation) is not on the shortlist. Until then, everything provider-neutral is built and the dev environment signs with the BankID simulator.

## What exists when the trigger comes (built in issue 5 M1-M5)
Check `STATUS.md` for the actual state; this is the target.
- `application/shared/signing.py`: the provider interface and the factory. The provider is chosen by the `provider` field in the secret at `SIGNING_PROVIDER_SECRET_ARN`: `simulator` (dev), `bankid_direct`, later `scrive`.
- Signing order: the restaurant signs first (owner in admin), then the customer. Statuses: `offer_sent` means "waiting for signatures"; per-party signature state lives on the offer version (`signatures[{role, method, signedAt, evidenceKey}]`). A new offer version cancels the open signing order.
- Offer PDF: `functions/catering-document` (WeasyPrint) renders `OFFER#vNNNN` to `<tenant>/offers/<id>/vNNNN.pdf` with its SHA-256 on the offer.
- Webhook endpoint `POST /webhooks/signing/catering` (`functions/catering-signing-webhook`): dedupe on `AUDIT#SIGNING#<eventId>`, provider-specific verification delegated to the adapter, always re-fetch status from the provider before changing state.
- Locked storage: `signed/` and `invoices/` get per-object COMPLIANCE retention (7 years in prod, 1 day GOVERNANCE in dev); `offers/` expire after 13 months.
- Admin and customer UIs handle both start kinds the interface can return: `redirect` (open the provider's signing page) and `bankid` (QR or app on same device, polled through `collect`).
- Fallback without BankID (simple acceptance) is provider-independent and already works.

### Interface (keep it; one implementation per provider, no extra layers)
```
create_order(request, offer, pdf_bytes, parties) -> order_ref         # parties: [{role: "restaurant"|"customer", name, email, personalNumber?}] in signing order
start(order_ref, role, end_user_ip) -> {"kind": "redirect", "url"} | {"kind": "bankid", "orderRef", "autoStartToken", "qr"}
collect(order_ref, role) -> {"state": "pending"|"signed"|"failed", "hint"?}   # only for kind "bankid"
cancel(order_ref) -> None
status(order_ref) -> {"complete": bool, "parties": [{role, signedAt?, method}]}
fetch_evidence(order_ref) -> {"<filename>": bytes}                  # stored under signed/<requestId>/vNNNN/ with lock
verify_webhook(headers, raw_body) -> {"eventId", "orderRef"} | None   # None means reject (401)
```

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
