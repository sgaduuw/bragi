# Reliable editing and publication implementation plan

> **For agentic workers:** Use `superpowers:executing-plans` task by task. Track completed checks below and record deviations with their evidence.

**Tech stack:** Flask, SQLAlchemy, migrated SQLite, Jinja, pytest; existing browser checks for JavaScript.

## Global constraints

- Worktree: `bragi-topics`, baseline `ffa182f`. No dependencies or schema changes unless a demonstrated requirement makes them necessary.
- User authorized planning and implementation of all five topics on 2026-10-03. The user authorized five topic commits with explanatory bodies at 22:37 CEST. Push, PR creation, issue closure and deployment remain separate actions.
- Markdown remains canonical. Preserve contrib boundaries and one supplied session for lifecycle writes. Content and hooks commit once.
- Observe meaningful regression failures before implementation. Use migrated file-backed SQLite and independent connections for transaction behavior.
- Keep disputed issue claims as explicit outcomes, rather than implementing them automatically. #520 and importer/block-tree work stay deferred.
- Run Ruff lint, Ruff formatting, mypy and the full pytest suite before final completion. Update CHANGELOG and affected operator documentation.

**Goal:** Publication, editing conflicts and invalid submissions preserve content while keeping SQLite write transactions short.

**Spec:** GitHub `sgaduuw/bragi` #529, #531, #532; publication parts of #535, #536, #537 and #538.

**Architecture:** Fix repeated lifecycle indexing in its shared reconciler. Release rejected editor writes in the existing form rendering boundary, where every post/page error response converges. Keep authorization and editor-token checks under the writer lock for accepted writes.

## Review focus

- Repeated hook calls and changing link sets within one uncommitted transaction.
- Hook failure after reconciliation must roll back content and edges together.
- Conflict forms must keep submitted fields and their original version token.
- Independent writers must proceed during Jinja error rendering.
- Authentication choice must preserve scopes, CSRF boundaries and revocation checks.

### Task 1: Repeated internal-link reconciliation (#529)

**Files:** `src/bragi/contrib/internal_links/index.py`, `tests/contrib/test_internal_links.py`, `tests/integration/test_lifecycle_commit.py`.
**Interfaces:** Keep `reindex_source(item, session)` and `drop_for_deleted(item, session)` unchanged; hooks still own no commits.

- [x] Add a real inline draft-to-published route regression with a resolved internal link and assert one persisted edge plus published content.
- [x] Add repeated-reconcile coverage without an intermediate commit, including changed/removed targets and rollback.
- [x] Run the new tests against baseline. Expected: uniqueness failure or stale pending edge.
- [x] Flush pending changes before deleting/reconciling edges so SQL sees prior hook additions under `autoflush=False`; trace every caller including delete/rebuild.
- [x] Run `uv run pytest tests/contrib/test_internal_links.py tests/integration/test_lifecycle_commit.py`. Expected: all pass.

### Task 2: Release rejected editor writes (#532, #538 section 3)

**Files:** `src/bragi/contrib/post/admin.py`, `src/bragi/contrib/page/admin.py`, `tests/integration/test_editor_conflicts.py`, `tests/integration/test_scheduling_editor_lock.py`.
**Interfaces:** Existing `_render_post_form` and `_render_page_form` remain the shared error/GET rendering boundary. No helper that commits rejected work.

- [x] Parameterize independent-writer probes at actual Jinja rendering for edit/stage/save/promote/discard conflicts and validation failures, posts and pages. Assert execution count, unchanged persisted rows and original form/token.
- [x] Add invalid/scheduled inline status probes. Expected: current code prevents an independent writer while rendering errors.
- [x] Roll back before form rendering; ensure evaluated ORM arguments remain readable and rejected mutations cannot persist. Do not acquire BEGIN IMMEDIATE for input known to be invalid.
- [x] Run `uv run pytest tests/integration/test_editor_conflicts.py tests/integration/test_scheduling_editor_lock.py`. Expected: all pass.

### Task 3: Authentication contract (#531)

**Files:** `src/bragi/contrib/api_tokens/auth.py`, related plugin registration, `src/bragi/core/editor_state.py` only if required, `tests/contrib/test_api_tokens.py`, authentication docs.
**Interfaces:** User selected API-only. Bearer credentials are restricted to the registered `api_tokens_api` blueprint.

- [x] Receive explicit selection before changing that security-sensitive contract. Continue independent tasks meanwhile.
- [x] For API-only: restrict token authentication/CSRF exemption to registered `/admin/api/*` routes, reject bearer credentials outside that surface, and document supported API scopes. Verify read-only token cannot mutate HTML even when a browser cookie is present, and API token writes still work.
- [x] Run focused auth tests red before green, then existing cookie editor and API auth tests.

### Task 4: Publication evidence and checklist outcomes

**Files:** `tests/integration/conftest.py`, `tests/integration/test_lifecycle_commit.py`, `tests/contrib/test_post_scheduling.py`, `tests/integration/test_scheduled_publish.py`, touched source comments, `CHANGELOG.md`.

- [x] Correct stale fixture docstrings: default tests explicitly create FTS tables but do not run Alembic.
- [x] Replace CLI count substring assertions with exact parsed lines.
- [x] Retain existing cancellation and scheduled revision tests; they already guard the behavior reported missing by #536.
- [x] Keep scheduler `scheduled_for` as historical publication context unless a real inconsistent consumer is demonstrated. Record this #535 outcome.
- [x] Decline speculative helper splits, future Page tags, and generalized authorization preambles from #537 unless required by a proven fix.
- [x] Run all four repository gates and review the topic diff. Expected: clean gates, no secrets or unrelated changes.

## Progress and rulings

- Baseline Ruff lint and formatting passed; mypy passed for 331 files. Baseline pytest: 2324 passed in 191.13s.
- Execution and five topic commits are approved; remote publication remains a separate user action.

- Task 1: #529 fixed with a flush before each internal-link SQL deletion; the supplied transaction still commits only in its caller. New checks cover unchanged, changed and removed pending edges, pending source/target deletion, real inline publication, and rollback after a later hook fails.
- Task 1 evidence: at baseline `ffa182f`, `uv run pytest tests/contrib/test_internal_links.py tests/integration/test_lifecycle_commit.py -k 'pending_edges or inline_publication' --tb=short` failed all 7 new cases with duplicate or stale edges. After the fix, all 39 tests in both files passed. Full combined gates passed, as recorded below.
- Task 1 ruling: include `drop_for_deleted` in the flush fix because its SQL DELETE also missed pending edges; both source and target deletion checks reproduced that failure. No interface or commit-boundary change.

- Task 2 complete: 18 new regression cases failed with an independent writer blocked during Jinja rendering on the unfixed code. After rollback at both form-render boundaries and a conditional inline status lock, `uv run pytest tests/integration/test_editor_conflicts.py tests/integration/test_scheduling_editor_lock.py -q` passed 107 tests in 35.77s.
- Ruling: split independent tasks within this topic between a worker (link reconciliation) and the main agent (transaction exits); ownership is disjoint. No commits, so progress lives in these plans and the preserved worktree.
- #535 schedule timestamp outcome: retain historical scheduled_for after automatic publication. The scheduler SQL requires SCHEDULED and a non-null due time; display returns early for other statuses. Manual cancellation intentionally clears it. No broken consumer found.
- #537 scheduler assertion outcome: retain the invariant after UPDATE RETURNING. It checks the just-claimed non-null time under the same writer transaction; it is not the eligibility gate. Audit payload remains the actual status transition plus source/scheduled_for, rather than inventing unchanged fields.
- #537 editor structure outcome: retain cohesive 110-line editor utility and explicit per-route authorization. Author-own and editor-wide permissions differ. No generic context manager, five-way split, future page-tag branch, or speculative GET refactor.
- #537 helper typing: defer the optional union/signature refactor because these fixes do not need it; no demonstrated wrong-type call exists. Metadata task may naturally replace site_id with the already-loaded Site when it needs site defaults.

- User selected API routes only at 20:09 CEST. Task 3 rejects bearer headers outside the registered api_tokens_api blueprint before verification, last-used/audit writes, principal assignment or CSRF exemption. Cookie-only admin requests continue unchanged. Five new GET/PATCH cases were observed failing before the guard.
- Tasks 1, 2 and 4 verification before the auth change: all four gates passed; pytest 2349 passed in 193.63s. Granny review found no actionable correctness, security or quality issues. CLI count mutation (12 instead of 2) was rejected by the exact summary assertion; original CLI restored.

- Task 3 focused verification: 140 API and editor tests passed in 39.26s. Granny found one stale API-token UI/docstring promise; corrected both to API-only. Security boundary otherwise passed review. Final combined gates passed after all five topics, as recorded below.
- Existing browser recovery baseline passed using `uv run --with playwright python tests/browser/check_editor_recovery.py`.

- #536 guard coverage is in `tests/contrib/test_post_scheduling.py`: inline cancellation checks status and NULL schedule; scheduled revision restoration checks Draft, NULL schedule/publication time and both warning phrases. The reported missing coverage came from running other files. The opt-in token fixture only supplies absent keys, preserving explicit stale or empty tokens; dedicated conflict tests use the unmodified client.

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
