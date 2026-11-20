# Live replay results (dev)

Status: NOT COMPLETE. The live replay could not be (re)run in this session, so there is no verdict and no
metric below that is measured.

## Why

The AWS SSO token for `booqy-dev` had expired and could not be refreshed. The device-code login opens the AWS
access portal sign-in page (username and password), and entering credentials is not something this session may
do. Nothing was called in AWS (no Transcribe, Polly, Bedrock, DynamoDB or Lambda) during this pass.

Earlier runs left live recordings in `tests/out/` (`*-live.wav`, 02:21 to 02:36 on 2026-11-20) for every scenario
voice-A..F and lang-A..K, but their timelines and metrics were not saved, so none are quoted here.

## Scenario table

| Scenario | Result |
|---|---|
| voice-A hesitation | NOT RUN (no AWS access) |
| voice-B backchannel during info | NOT RUN |
| voice-C correction to five | NOT RUN |
| voice-D interruption with Friday | NOT RUN |
| voice-E normal booking | NOT RUN |
| voice-F unavailable time, real alternatives | NOT RUN |
| lang-A..H (Swedish, English after Swedish greeting, Persian, Arabic, mid-call switch, uncertain utterance, switch before confirm, interruption keeps language) | NOT RUN |
| lang-I German, lang-J Turkish, lang-K Polish | NOT RUN |

## Metrics

Time to first backchannel, end of speech to first assistant audio (p50, p95), barge-in stop latency, language
detection time and accuracy: not measured.

## Changes in the gateway since the last commit (found by the earlier live runs)

- `brain/streaming.py`: a note the model writes to itself beside a lookup (ISO date, a clock time in digits, a
  quoted phrase) is no longer spoken from the live stream.
- Harness: `VG_WORLD=dev` runs against the real dev restaurant (`tests/vg_harness/devworld.py`, notifications
  recorded, never sent), `VG_STT=fake` for languages no AWS voice speaks, scenarios lang-I, lang-J, lang-K.
- Offline suite: 511 passed, 12 skipped.

## To finish

1. `aws sso login --sso-session booqy --use-device-code --no-browser`, approve in the portal.
2. `VG_WORLD=dev python -m voice_gateway.replay all --mode live` from `services/voice-gateway`.
3. Delete what the run created in dev (reservation `PENDING#`, `RESERVATION#`, `RID#` rows, `LINK#` rows, `CALL#`
   and `CALL#...#T#` rows in `dev-phone-agent`), compare with a before snapshot, and replace this file with the
   measured table.

Dev cleanup is UNVERIFIED: whether the earlier live runs wrote rows in dev (the dev world was added for them)
could not be checked without AWS access.
