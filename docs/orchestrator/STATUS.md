# Orchestrator status

Single source of truth for the restaurant-features work (brief: `docs/ORCHESTRATOR.md`).
On restart, read this file first and resume.

Updated: 2026-10-22

## Issues

| # | Issue | Priority | Phase | Branch | Agent | Status | Blockers |
|---|-------|----------|-------|--------|-------|--------|----------|
| 5 | Catering | 1 | 1 Planned | dev | none | waiting for owner | C1-C8 |
| 3 | Booking time limits | 2 | 1 Planned | dev | none | waiting for owner | none |
| 2 | Kitchen printing | 3 | 1 Planned | dev | none | waiting for owner | printer hardware (owner) |
| 4 | Cookies and email marketing | 4 | 1 Planned | dev | none | waiting for owner | none |
| 1 | Scheduled dishes | 5 | 1 Planned | dev | none | waiting for owner | none |

Phase 0 (recon, `CONVENTIONS.md`): done.
Phase 1 (`PLAN.md`): done 2026-10-22, waiting for the owner's answers (PLAN.md section 8). No implementation started.

## Decisions (dated)

- 2026-10-22: Docs live in `application/docs/ORCHESTRATOR.md` and `application/docs/orchestrator/`.
- 2026-10-22: booqy-console has uncommitted work from another session (billing restart fix, 6 files). Left untouched; this work does not touch booqy-console.
- 2026-10-22: No AWS calls at all (including `terraform plan`) until the owner switches the shell to a dev profile. The active credentials are an IAM user in account 388343452097, which is neither dev (877653167825) nor prod (343695380960).
- 2026-10-22: Extra branches deleted on the owner's request: infrastructure `backup` (local only, 664f959, catering snapshot from 2026-09-26 superseded by dev), `feat/lambda-blue-green` (local and remote, merged), remote `dev-sync-test` (merged); application `feature/multi-tenancy` (local and remote, merged), remote `15-task-15-implement-lambda-create-pending-reservation` (merged). Only `dev` and `main` remain in every repo.

## Rule changes

- 2026-10-22: Git. Overrides CLAUDE.md "git read only, owner commits" and the brief's per-issue branches, worktrees and `integration/restaurant-features`. Work only on `dev` in every repo; Claude commits and pushes directly to `origin/dev`. No other branches. Implementation agents run without worktree isolation, one writer at a time per repo/file set. Merging to `main` stays the owner's call. A push to `dev` runs the dev deploy pipeline (dev account only).
- All other CLAUDE.md rules still hold: no em or en dashes anywhere, Swedish app copy, light and dark mode, never change OIDC/deploy roles, call out IAM changes before making them, no secrets.

## Open questions

All batched in `PLAN.md` section 8 (owner), 9 (end customer), 10 (legal advisor).
