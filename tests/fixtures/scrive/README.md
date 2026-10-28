Scrive Document API v2 responses for `tests/test_signing_scrive.py`, shaped
after Scrive's v2 documentation (documents/new, /start, /get, error bodies).
Ids and links are made up. Once the testbed credentials are in the dev
secret, replace them with real testbed responses (strip names, e-mails and
personal numbers first); the tests read only the fields the adapter uses.
