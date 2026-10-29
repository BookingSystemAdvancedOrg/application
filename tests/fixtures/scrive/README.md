Scrive Document API v2 responses for `tests/test_signing_scrive.py`, shaped
after Scrive's v2 documentation (documents/new, /start, /get, error bodies).
Ids and links are made up. Once the testbed credentials are in the dev
secret, replace them with real testbed responses (strip names, e-mails and
personal numbers first); the tests read only the fields the adapter uses.

`error_sign_method_disabled.json` follows the testbed's real answer to
/start (2026-10-29) while se_bankid was not enabled on the account: the
`error_details.errors` list is real, the top-level fields are modelled.
