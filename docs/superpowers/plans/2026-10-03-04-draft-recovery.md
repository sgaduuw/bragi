# Draft recovery and upgrades implementation plan

> **For agentic workers:** Use `superpowers:executing-plans` task by task. Track completed checks below and record deviations with their evidence.

**Tech stack:** Flask, SQLAlchemy, migrated SQLite, Jinja, pytest; existing browser checks for JavaScript.

## Global constraints

- Worktree: `bragi-topics`, baseline `ffa182f`. No dependencies or schema changes unless a demonstrated requirement makes them necessary.
- User authorized planning and implementation of all five topics on 2026-10-03. The user authorized five topic commits with explanatory bodies at 22:37 CEST. Push, PR creation, issue closure and deployment remain separate actions.
- Markdown remains canonical. Preserve contrib boundaries and one supplied session for lifecycle writes. Content and hooks commit once.
- Observe meaningful regression failures before implementation. Use migrated file-backed SQLite and independent connections for transaction behavior.
- Keep disputed issue claims as explicit outcomes, rather than implementing them automatically. #520 and importer/block-tree work stay deferred.
- Run Ruff lint, Ruff formatting, mypy and the full pytest suite before final completion. Update CHANGELOG and affected operator documentation.

**Goal:** Operators can recover legacy working copies without losing content, and browser save receipts acknowledge only valid draft identifiers.

**Spec:** Recovery sections of #536, #537 and #538.

**Architecture:** Keep the baseline fingerprint refusal. Explain and test a copy-out, restage, restore, promote journey using the existing editor. Strengthen the existing receipt and browser checks without adding storage infrastructure.

## Review focus

- Legacy copies with valuable text and no baseline fingerprint.
- A live row changed after staging, and a deleted working copy.
- Malformed and noncanonical UUID receipt identifiers.
- Resume controls on non-resume or unspecified page kinds.
- Existing browser drafts retained after failure and removed only after matching successful save.

### Task 1: Legacy working-copy recovery

**Files:** `docs/content.md`, `CHANGELOG.md`, post/page working-copy banners if guidance is needed there, `tests/integration/test_editor_conflicts.py`.
**Interfaces:** Existing stage/save/promote routes and `_edit_token` remain the recovery path.

- [x] Start with a legacy copy whose body differs from live and whose `base_fingerprint` is NULL. Assert promotion refuses and preserves it.
- [x] Exercise the documented copy-out, fresh live GET, deliberate restage, restore copied fields, save, promote journey. Assert the recovered text survives and current live metadata is preserved where not edited.
- [x] Add explicit copy-out-before-restaging guidance in the editor and docs. Explain that restaging replaces the working copy. Keep the refusal rather than inventing a legacy baseline.
- [x] Run `uv run pytest tests/integration/test_editor_conflicts.py`. Expected: new guidance/check fails before change and all tests pass afterward.

### Task 2: Receipts and recovery controls

**Files:** `src/bragi/core/editor_state.py` if behavior needs correction, `src/bragi/contrib/page/templates/admin/_resume_fieldset.html`, `src/bragi/static/admin/resume-fieldset.js` only for demonstrated failure, existing recovery integration/browser checks.

- [x] Check malformed UUID cannot emit a save receipt, and canonicalization behavior matches JavaScript record identity. Add only missing behavioral guards.
- [x] Check all resume-fieldset callers before changing its default. Make unspecified/non-resume kinds disabled if an actual supported render path can otherwise submit resume data.
- [x] Preserve existing successful-save identity and conflict recovery behavior; run `tests/browser/check_editor_recovery.py` using its documented invocation if browser tooling is available.
- [x] Record outcomes: privacy guidance already explains seven-day persistence after logout; do not duplicate it or silently erase drafts. UUID validation is input validation, not the whole identity guarantee.
- [x] Run the focused recovery checks; the final combined repository gates are recorded below.

## Progress and rulings

- Implemented recovery guidance in both legacy working-copy banners and docs. The expanded journey failed twice against baseline because guidance was absent; after the change both editors copied out, refused unsafe promotion, restaged, restored and promoted successfully while retaining newer live metadata.
- Added six malformed/empty/noncanonical receipt cases. Existing behavior passed; a temporary mutation replacing UUID parsing with the raw submitted value made all six fail. Restored the original implementation before the green run.
- `uv run pytest tests/integration/test_editor_conflicts.py -q --tb=short --show-capture=no`: 108 passed in 36.20s. `uv run --with playwright python tests/browser/check_editor_recovery.py`: Browser recovery checks passed. Ruff check/format passed for the changed tests.
- #537 resume default/null-guard suggestions declined: the sole production caller defines `current_kind` before including the fieldset, and the guarded wrapper always contains that fieldset. No supported path demonstrated the claimed failure. Corrected the inaccurate include comment without changing behavior.
- Privacy guidance already covers seven-day browser persistence after logout. Receipt UUID parsing is retained as input validation; the browser check covers matching record identity and success/failure retention.
- Topic 5 may proceed while independent review/final combined gates finish. No recovery production changes remain in flight.

- Independent Granny spec/quality review passed with no actionable findings. Final combined repository gates passed, as recorded below.

## Final verification, 2026-10-03

All five topic implementations were verified together on `fix/529-cms-reliability` in `bragi-topics` before splitting them into the five authorized topic commits.

- `uv run ruff check src/ tests/ alembic/`: passed.
- `uv run ruff format --check src/ tests/ alembic/`: 566 files formatted.
- `uv run mypy src/`: passed, 332 source files.
- `uv run pytest`: **2516 passed in 228.24s**. Baseline was 2324 passing tests.
- `uv run --with playwright python tests/browser/check_editor_recovery.py`: passed, including native and boosted metadata preview submissions, JavaScript-disabled submission, keyboard Save ordering, current Markdown and retained metadata recovery.
- `git diff --check`: passed. Final independent Granny review passed specification, correctness, security and simplicity checks with no unresolved findings or exposed secrets.

The first combined pytest run had 2515 passes and one old CSRF status expectation. API-only bearer rejection now returns 401 before the HTML CSRF guard. The revised check verifies that rejection preserves the authenticated session, cookie-only writes still require CSRF, and valid-CSRF logout succeeds. All 52 focused auth/CSRF tests and the final full run passed. No production change was needed for that test correction.

Documentation and Unreleased entries are updated. At verification time, no dependencies or schema changes, commits, pushes, PRs, issue closures or deployments had been made. Deferred suggestions remain documented in the rulings above; #520 and the previously deferred importer/block-tree work remain outside this batch.
