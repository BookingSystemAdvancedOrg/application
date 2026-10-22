# Orchestrator status

Single source of truth for the restaurant-features work (brief: `docs/ORCHESTRATOR.md`).
On restart, read this file first and resume.

Updated: 2026-10-22

## Issues

| # | Issue | Priority | Phase | Branch | Agent | Status | Blockers |
|---|-------|----------|-------|--------|-------|--------|----------|
| 5 | Catering | 1 | 2 Implementing (M1 done, in review) | dev | review agent | in progress | provider adapter waits for Scrive/SEB decision |
| 3 | Booking time limits | 2 | 1 Approved | dev | none | queued after catering | none |
| 2 | Kitchen printing | 3 | 1 Approved | dev | none | queued after catering | none (Star mC-Print3, Ethernet, 80 mm) |
| 4 | Cookies and email marketing | 4 | 1 Approved | dev | none | queued after catering | none |
| 1 | Scheduled dishes | 5 | 1 Approved | dev | none | queued after catering | none |

Phase 0 (recon, `CONVENTIONS.md`): done.
Phase 1 (`PLAN.md`): done 2026-10-22, waiting for the owner's answers (PLAN.md section 8). Approved 2026-10-22.

## Decisions (dated)

- 2026-10-22: Docs live in `application/docs/ORCHESTRATOR.md` and `application/docs/orchestrator/`.
- 2026-10-22: booqy-console has uncommitted work from another session (billing restart fix, 6 files). Left untouched; this work does not touch booqy-console.
- 2026-10-22: No AWS calls at all (including `terraform plan`) until the owner switches the shell to a dev profile. The active credentials are an IAM user in account 388343452097, which is neither dev (877653167825) nor prod (343695380960).
- 2026-10-22: Extra branches deleted on the owner's request: infrastructure `backup` (local only, 664f959, catering snapshot from 2026-09-26 superseded by dev), `feat/lambda-blue-green` (local and remote, merged), remote `dev-sync-test` (merged); application `feature/multi-tenancy` (local and remote, merged), remote `15-task-15-implement-lambda-create-pending-reservation` (merged). Only `dev` and `main` remain in every repo.

- 2026-10-22: Owner approved PLAN.md with every recommended answer in section 8, except C1.
- 2026-10-22: C1 changed: the signing provider is chosen after meetings with Scrive and SEB (direct BankID RP); Idura is not on the shortlist. Build everything provider-neutral now with the simulator in dev; the adapter starts on the owner's trigger, see `CATERING-SIGNING-HANDOFF.md`.
- 2026-10-22: Printer for issue 2: Star mC-Print3, Ethernet, 80 mm.
- 2026-10-22: Owner: build all catering parts first (issue 5 M1-M5 minus the provider adapter), then the other issues. Bug fixes outside catering wait.
- 2026-10-22: Execution: one implementation agent at a time (shared checkout, `git add .`), milestone by milestone; infra pushed and its dev CI finished before application is pushed; pushes to dev deploy to the dev account only.

- 2026-10-23: Issue 5 M1 shipped to dev: infrastructure 77a1695, b27272f (CI 39854058365 ok); application b2d6e1a (CI 39861092948 ok); admin 78ee3c2 (CI 39862567469 ok). Migration script `scripts/migrate_catering_menu.py` written, not yet run.

## Rule changes

- 2026-10-22: Git. Overrides CLAUDE.md "git read only, owner commits" and the brief's per-issue branches, worktrees and `integration/restaurant-features`. Work only on `dev` in every repo; Claude commits and pushes directly to `origin/dev`. No other branches. Implementation agents run without worktree isolation, one writer at a time per repo/file set. Merging to `main` stays the owner's call. A push to `dev` runs the dev deploy pipeline (dev account only).
- 2026-10-22: Terraform `plan` and `apply` run only in the CI/CD pipeline, never locally (overrides X1). Locally only `fmt` and `validate`.
- 2026-10-22: Orchestrator md files are deleted once every step in them is done (long-term content moved to normal docs first).
- All other CLAUDE.md rules still hold: no em or en dashes anywhere, Swedish app copy, light and dark mode, never change OIDC/deploy roles, call out IAM changes before making them, no secrets.

## Open questions

All batched in `PLAN.md` section 8 (owner), 9 (end customer), 10 (legal advisor).
- M1-Q1: default for `defaultServiceIncluded` (false = 6 % food VAT from M2, true = keep 12 %). Rec: true until the accountant confirms.
- M1-Q2: running `migrate_catering_menu.py --apply` needs credentials for dev; how should it run (owner locally, or a one-off CI job)?
