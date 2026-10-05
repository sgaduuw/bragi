# Bragi database dependency upgrade implementation plan

> **For agentic workers:** Use `superpowers:executing-plans` for sequential implementation, with an independent reviewer before integration. Steps use checkboxes for tracking.

**Goal:** Adopt SQLAlchemy 2.1 and the current Alembic release while preserving Bragi's explicit-flush, single-transaction behavior and recovery guarantees.

**Architecture:** Upgrade the ORM and migration libraries together, retaining the existing synchronous session architecture. Add a behavior check around the production session factory before upgrading; make only compatibility fixes demonstrated by tests.

**Tech stack:** SQLAlchemy, Alembic, SQLite, pytest, uv.

**Spec:** The 2026-10-04 conversation's database batch and [SQLAlchemy 2.1 migration guide](https://docs.sqlalchemy.org/en/21/changelog/migration_21.html).

**Tracking:** [#552](https://github.com/sgaduuw/bragi/issues/552), branch `chore/552-database-dependencies`.

## Targets and ownership

- SQLAlchemy 2.0.50 -> 2.1.3.
- Alembic 1.18.4 -> 1.20.0.
- Mako 1.3.12 -> 1.4.3.
- Accept removal of greenlet only after confirming Bragi's built-in database paths use synchronous sessions and do not depend on SQLAlchemy's asyncio extra.

Existing `sqlalchemy>=2.0,<3.0` and `alembic>=1.13,<2.0` ranges already allow these targets. Keep them unless an actual compatibility requirement demands otherwise. Preserve the completed runtime batch and the original development-tool versions.

## Shared execution rules

- Planning baseline: develop `5dd04fd6ae11f3c9a3aeeef7ca3ccfa75caaa11e`, after Dependabot PRs #546-#549. Its exact tree passed 2,523 tests, Ruff, formatting, mypy, and real guarded HTTPS checks. The optional browser module was tested separately during the release.
- Execute sequentially: runtime/security, database, tooling. The user requested this batch before runtime integration, so its isolated worktree copies the tested batch-one changes over develop `5dd04fd`. The original runtime worktree is preserved, and an incremental patch records the database delta for separate commits/PRs. Preserve the shared checkout.
- Before implementation, create or link the proposed tracking issue. No existing open issue covers this batch. Planning does not create an issue.
- Use targeted `uv lock --upgrade-package 'name==version'` arguments for every package assigned below. Avoid an unrestricted `--upgrade`. Inspect the complete lockfile delta; a required dependency outside this batch needs an explicit explanation.
- Keep Python `>=3.14,<4`, the existing plugin interfaces, and production behavior. Application version changes belong to the later release.
- Preserve baseline evidence when the base tree is unchanged. If it changed, establish a fresh baseline. For an application fix, retain a regression and observe its intended failure before fixing it. Dependency-only changes use the existing behavior checks; do not add tests that merely repeat version pins.
- After the final edit, run each gate once, checking its exit code:
  `uv run --frozen ruff check src/ tests/ alembic/`;
  `uv run --frozen ruff format --check src/ tests/ alembic/`;
  `uv run --frozen mypy src/`;
  `uv run --frozen pytest`.
  Record the optional-browser skip explicitly and run browser checks when their affected behavior is in scope.
- Finish with an independent correctness review and Ponytail ultra diff review. Fix findings within this batch, inspect for secrets, and prepare one PR to develop. Commit/push/PR actions follow the user's authorization; propose the commit message below.
- Run PR CI on the final head and resolve all actionable feedback before merge. Record the tested tree and verify that the merge contains it.
- These plans end at integration into develop. A new release and deployment are separate actions.

## Review focus

1. ORM and raw SQL reads with pending changes must honor production `autoflush=False`.
2. Rejected edits, failed publication, and exceptions must roll back without holding SQLite's writer lock through rendering or network work.
3. Successful publication must commit content and lifecycle effects once; concurrent workers must not duplicate them.
4. Fresh migrations, populated upgrades, and rollback must preserve schema state, public content, media and URLs.
5. Session APIs, row access and type inference changed by SQLAlchemy must not be accommodated by weakening validation or suppressing checks.

## Task 1: Pin the production session behavior

**Files:** Create `tests/integration/test_session_autoflush_contract.py`. Read `src/bragi/core/db.py` and `tests/integration/conftest.py`; modify production code only if a demonstrated compatibility issue requires it.

**Interfaces:** The test consumes `migrated_db_url` and runs a fresh subprocess with `BRAGI_DATABASE_URL` set to that disposable database. It imports the real `SessionLocal` factory. Do not substitute the test factory, which independently sets `autoflush=False` and could hide a production regression.

- [x] Add `test_production_session_preserves_explicit_flush_boundaries`. In the child process, add a uniquely named User and execute both an ORM SELECT and raw `text()` SELECT. Assert the pending User remains unflushed and is not visible to another connection.
- [x] Assert an independent connection can acquire and roll back `BEGIN IMMEDIATE` after those reads, demonstrating that they have not implicitly acquired the writer lock.
- [x] Explicitly flush; assert the row is visible inside that transaction but not to the independent connection. Roll back and assert the row remains absent. Repeat with a new User and commit, then assert persistence from a fresh connection.
- [x] Run `uv run --frozen pytest -q tests/integration/test_session_autoflush_contract.py` on the old ORM. Temporarily mutate only the production factory to `autoflush=True`, observe failure on the intended unflushed/read assertion, restore it, and verify passing. Retain the test, not the mutation.

## Task 2: Upgrade and verify the database stack

**Files:** Modify `uv.lock` and `CHANGELOG.md`; narrowly scoped compatibility fixes and their regressions only if needed. No new Alembic revision is planned.

**Interfaces:** Consumes the completed runtime graph and the production-session regression above. Produces the same application database contract under the new ORM and migration tools.

- [x] Review SQLAlchemy 2.1 and Alembic 1.19/1.20 compatibility notes against actual calls. Bragi's explicit `autoflush=False` remains required; the upstream default change does not justify enabling automatic flushes.
- [x] Apply targeted upgrades for SQLAlchemy 2.1.3, Alembic 1.20.0 and Mako 1.4.3; run `uv sync --frozen`. Inspect lockfile drift and greenlet removal.
- [x] Run the new test and existing transaction/recovery checks:

```sh
uv run --frozen pytest -q tests/integration tests/test_db_hardening.py tests/test_recovery.py
```

This covers working-copy isolation/promotion, lifecycle commits, scheduler races, media deletion locking and backup recovery. Any regression gets a retained failing check before the smallest fix.

- [x] Run migration upgrade/downgrade/upgrade using a new absolute temporary database path for every command:

```sh
bragi_migration_dir="$(mktemp -d)"
BRAGI_DATABASE_URL="sqlite:///$bragi_migration_dir/bragi.db" uv run --frozen alembic upgrade head
BRAGI_DATABASE_URL="sqlite:///$bragi_migration_dir/bragi.db" uv run --frozen alembic downgrade base
BRAGI_DATABASE_URL="sqlite:///$bragi_migration_dir/bragi.db" uv run --frozen alembic upgrade head
```

Expected head remains `f515c0ffee01`. Never run downgrade against the default database.

- [x] Create an isolated Python 3.14 environment containing released `bragi-cms==1.54.0`. Set `BRAGI_RECOVERY_PREVIOUS_PYTHON` to that environment's interpreter and run `uv run --frozen pytest -q tests/test_recovery.py`. This must prove populated upgrade and failed-upgrade rollback, including existing content/media/public URLs.
- [x] Run `uv run --with playwright pytest -q tests/browser/test_recovery_receipts.py` and `uv run --with playwright python tests/browser/check_editor_recovery.py` against the candidate. Verify the run did not change the lockfile.
- [x] Add the Unreleased database compatibility entry, run the shared gates and reviews, and report the retained session test's mutation result.

**Done when:** Explicit flush and single-commit contracts pass; the populated recovery rehearsal and fresh migration round trip pass; the migration head and schema intent are unchanged; no unrelated runtime/tool upgrades landed.

**Proposed commits:** `test(db): guard production session flush boundaries`, then `chore(deps): upgrade SQLAlchemy and Alembic`. Keep one database PR; the two commits preserve the independently useful guard.

**Rollback:** Revert the library lock and any compatibility changes together. No schema rollback is required by a dependency-only update; investigate any unexpected schema requirement before proceeding.

## Execution record

Worktree: `/Users/ewesemann/Projects/.worktrees/bragi-database-dependencies`.
Branch: `chore/552-database-dependencies`. Implementation and local verification completed before publication. The branch is stacked on runtime commit `22cac3f`.

- The production-factory guard passed with SQLAlchemy 2.0.50. An
  `autoflush=True` mutation failed at the intended pending-row assertion.
  The source was restored byte-for-byte and the test passed again.
- The database delta contains only SQLAlchemy 2.1.3, Alembic 1.20.0 and
  Mako 1.4.3, plus removal of greenlet. Built-in sessions are synchronous.
  Runtime and tool versions remain unchanged.
- SQLAlchemy's new result typing exposed 15 mypy errors in seven files.
  Precise annotations and an explicit SQLite date result type resolve them.
  No checks were suppressed or disabled; no session behavior was changed.
- Transaction/integration/recovery suite: 412 passed. Full suite:
  2,547 passed and one optional browser-module skip. Ruff passed, formatting
  passed for 572 files, and mypy passed for 333 source files.
- Browser checks ran separately: 12 receipt tests passed, and the editor
  recovery script passed. The project lockfile was unchanged.
- Published `bragi-cms==1.54.0` was installed in an isolated environment
  with SQLAlchemy 2.0.50, Alembic 1.18.4 and Mako 1.3.12. Both populated
  upgrade/failed-upgrade rollback recovery checks passed.
- Fresh upgrade/downgrade/upgrade passed with integrity and foreign-key checks.
  The final head remains `f515c0ffee01`. Column order and schema definitions
  agree; only named-constraint emission order differed between processes.
  No migration was added.
- Both CPython 3.14 Linux amd64/arm64 SQLAlchemy wheels exist. Runtime
  execution was tested on macOS, not Linux.
- The verified SQLAlchemy database-path decoding change is documented in
  the changelog and recovery guidance. Operators using percent escapes must
  compare the resolved path before startup or migration.

### Upstream result-cleanup limitation

The [unreleased SQLAlchemy 2.1.4 notes](https://docs.sqlalchemy.org/en/21/changelog/changelog_21.html#change-2.1.4)
describe delayed collection of Result/cursor objects introduced in 2.1.
A bounded probe with cyclic GC disabled confirmed that Bragi's normal fully
consumed ORM/Core results close their actual cursors and allow another writer.
Objects retained after full direct iteration were released by cyclic GC.
The `scalars().all()` probe retained none after session close.

A deliberately partial Core result retained an open cursor until GC.
Writer acquisition alone did not detect it under WAL, so the probe also
checked actual cursor closure. Retain the planned 2.1.3 without a workaround,
while recording that interrupted/partial Core consumption remains exposed
to this upstream issue. This is not a long-running memory benchmark or a
claim about third-party plugins.

Independent correctness/security review found no actionable findings. Ponytail ultra found no unnecessary complexity. All 11 incremental file hashes matched the tested state. Commit, push and PR creation were authorized on 2026-10-05. PR CI and merge remain delivery checks.
Release and deployment are outside this batch.
