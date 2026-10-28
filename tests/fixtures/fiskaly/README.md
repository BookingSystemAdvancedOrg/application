# fiskaly SIGN SE API spec

`fiskaly.uapi.sign-se.2026-06-01.yaml` is fiskaly's published OpenAPI spec
for SIGN SE (Unified API, version 2026-06-01), downloaded 2026-10-25 from
https://workspace.fiskaly.com/specs/fiskaly.uapi.sign-se.2026-06-01.yaml.

`tests/fiskaly_fake.py` checks every request the adapter
(`shared/control_system_fiskaly.py`) sends against its schemas.

One change: the em and en dashes in descriptions are replaced by hyphens
(the repository allows neither). No schema, pattern or enum changed.
SHA-256 of the file as downloaded: `f3265ec696ee9817e6f18bbfeeb4a5e411b48d703c065078189efb1d35ac0b0b`.
