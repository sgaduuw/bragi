# Upgrade recovery implementation plan

> Execute inline with superpowers:executing-plans. User authorized planning and execution for #523. Leave changes uncommitted.

**Goal:** Prove that a quiesced SQLite/media backup restores a usable Bragi site after a failed upgrade.

**Architecture:** Reuse the backup CLI and manual extraction into an empty destination. Add one subprocess-based pytest rehearsal with actual Alembic migrations and real app requests, plus an operator runbook. No restore CLI or deployment changes.

**Tech stack:** Python 3.14, pytest, subprocess, SQLite, Alembic, Flask test clients.

**Spec:** https://github.com/sgaduuw/bragi/issues/523

## Constraints and review focus

- Synthetic data only; subprocess settings and working directories isolate the rehearsal from operator data and credentials.
- Stop all writers for the documented backup, including delivery, task workers, external jobs and upload/rendition workers. DB snapshot and media archive are not atomic together.
- Restore to a fresh destination; retain the failed deployment for diagnosis. Never copy a DB over a live connection or stale WAL files.
- Check actual authenticated access, content, redirects, original image and rendition bytes. Health checks alone do not prove recovery.
- Exercise a failure after persistent migration DDL, and restore the matching prior app and data, without assuming downgrade reversibility.
- Preserve configuration, secrets, plugin versions and external storage separately. No secrets in the evidence record.

## Task 1: Runnable rehearsal

Files: create `tests/test_recovery.py`.

- [x] Run baseline Ruff lint/format, mypy and pytest.
- [x] Implement one test that runs seed/check phases in fresh subprocesses against file-backed, Alembic-created DBs. Seed local credentials, a site, posts/pages, a redirect, original image and a rendition. Generate a real backup with the CLI.
- [x] Support an optional `BRAGI_RECOVERY_PREVIOUS_PYTHON` executable to seed, back up and restore using an independently installed previous release. Default to the current interpreter so ordinary CI needs no historical checkout or network access.
- [x] Copy the current migration tree into the temporary test directory, append a migration that creates a table and then raises, and assert both the intended failure and the persistent partial change.
- [x] Extract the backup into an empty directory, verify its revision/integrity and matching prior app, then upgrade the restored populated DB with current migrations and verify current app behaviour.
- [x] Prove the media check fails when restored media is missing, then succeeds with complete restoration. No production changes unless a reproduced recovery defect requires them.

## Task 2: Operator procedure and evidence

Files: modify `docs/operations.md`; add `docs/recovery.md`; link from `README.md`; add an Unreleased CHANGELOG entry.

- [x] Document retained artifacts, quiesced backup, explicit migration execution, fresh restore, app/version matching, and verification before traffic/tasks resume. Explain media races and missing attachment-root limitations.
- [x] Run the rehearsal using v1.53.1 in its own environment and current develop as the upgrade target. Record the exact source revision, schema revisions, tested runtime and limits, including no live deployment/container rehearsal.
- [x] Run all four repository gates, review the diff for secrets, and obtain an independent review. Fix any substantive findings and rerun affected checks.
- [x] Record results and a proposed Conventional Commits message in the shared work log; leave the issue open pending integration.

## Execution record

Base: `17095df` (origin/develop on 2026-10-02). Existing backup requires paused writes for a coherent DB/media pair. Baseline lint, formatting and mypy passed; baseline pytest: 2,246 passed.

Ruling: Proceed without another approval checkpoint because the user explicitly requested both plan and execution. No commits, deployment, or release are authorized by this plan.


Completed: recovery rehearsal passes with current application and independently installed v1.53.1 (`2ebc687`), upgrading populated schema `78d0fa9367df` to `f515c0ffee01`. Persistent partial-DDL failure and missing-media negative controls behaved as intended. Temporarily omitting the attachment archive made the test fail on missing media entries; production code was restored before final gates.

Final verification: 2,247 tests passed; Ruff lint and formatting passed; mypy passed on 328 source files; 16 local documentation links resolve; diff and credential checks passed. Granny Weatherwax found no actionable correctness or complexity findings. No production code changes, commits, deployment or release. Live/container recovery remains outside the evidence.

Proposed commit: `test(recovery): rehearse backup restore after failed upgrades (#523)`.


Adversarial review follow-up: fixed both findings in the rehearsal. The original and rendition now use valid, distinct 2x2 and 1x1 PNGs, with CRC/dimension preconditions and per-asset response checks. Bragi/Flask environment prefixes are filtered case-insensitively, with synthetic lowercase/mixed-case settings checked absent in child apps. The original PNG-validation and credential-isolation failures were observed before their fixes. The previously false-green original-for-rendition substitution now fails on byte identity. The independent v1.53.1 recovery run passes, and Granny verified both corrections without further findings.
