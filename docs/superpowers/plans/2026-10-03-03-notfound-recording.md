# Sustainable 404 recording implementation plan

> **For agentic workers:** Use `superpowers:executing-plans` task by task. Track completed checks below and record deviations with their evidence.

**Tech stack:** Flask, Click, SQLAlchemy, migrated SQLite, pytest.

## Global constraints

- Worktree: `bragi-topics`, baseline `ffa182f`. No dependencies or schema changes unless a demonstrated requirement makes them necessary.
- User authorized planning and implementation of all five topics on 2026-10-03. The user authorized five topic commits with explanatory bodies at 22:37 CEST. Push, PR creation, issue closure and deployment remain separate actions.
- Markdown remains canonical. Preserve contrib boundaries and one supplied session for lifecycle writes. Content and hooks commit once.
- Observe meaningful regression failures before implementation. Use migrated file-backed SQLite and independent connections for transaction behavior.
- Keep disputed issue claims as explicit outcomes, rather than implementing them automatically. #520 and importer/block-tree work stay deferred.
- Run Ruff lint, Ruff formatting, mypy and the full pytest suite before final completion. Update CHANGELOG and affected operator documentation.

**Goal:** Let operators reclaim full 404 storage deliberately, while preserving suppression by default and making recording failures observable.

**Spec:** GitHub `sgaduuw/bragi` #533, #535 section 1, #538 sections 1/2/6, and the recorder binding suggestion in #537 section 4.

**Architecture:** Extend the existing site-scoped prune command with optional ignored-record cleanup. Keep eligibility predicates inside the DELETE and share the exact-redirect predicate within the notfound plugin. Deduplicate the existing fixed warnings by reason.

## Review focus

- A site already full of ignored records must recover through the documented CLI.
- Ordinary cleanup must preserve deliberate suppression and existing automation.
- Dry runs and missing ignored-cleanup confirmation must leave every row unchanged.
- Cleanup must preserve another site's rows and unresolved open records.
- Database failures and rate suppression must each remain observable and bounded.

### Task 1: Deliberate capacity recovery (#533, #538 section 2)

**Files:** `src/bragi/contrib/notfound/cli.py`, `src/bragi/contrib/notfound/plugin.py`, `src/bragi/contrib/notfound/templates/admin/_notfound_list_table.html`, `tests/integration/test_notfound_limits.py`, `docs/operations.md`, `CONTEXT.md`, `CHANGELOG.md`.

**Interface:** Extend the Click callback to `prune(site_slug: str, dry_run: bool, include_ignored: bool, yes: bool) -> None`. Both new flags default to false. Require `--yes` only when `include_ignored and not dry_run`; ordinary prune keeps its existing invocation and behavior.

- [x] Add `test_ignored_capacity_recovery_requires_explicit_cleanup` using the migrated file-backed fixture. Seed site A with three ignored rows and cap 3; seed an ignored row on site B. Assert a new A path is blocked and re-recording an ignored A path leaves its status/count unchanged.
- [x] In that test, assert ordinary dry-run reports exactly `Would prune 0 records.` and ordinary prune reports exactly `Pruned 0 records.`. Both preserve every ignored row.
- [x] Assert `--include-ignored --dry-run` reports exactly `Would prune 3 records.` and changes nothing. Assert destructive `--include-ignored` without `--yes` exits with usage error and changes nothing. A dry run never requires `--yes`.
- [x] Run the new test against baseline and observe the missing-flag failure. Command: `uv run pytest tests/integration/test_notfound_limits.py::test_ignored_capacity_recovery_requires_explicit_cleanup`.
- [x] Add the two flags. Help text and missing-confirmation error must explain that deleting ignored rows removes suppression and hit history, so those paths can be recorded again. Preserve default protection for ignored rows.
- [x] Default eligibility remains this site, status not ignored, and dismissed or covered by an active exact redirect. When explicitly including ignored rows, add ignored status to the eligible set. Evaluate status and redirect membership in the DELETE itself; do not delete a previously selected ID list.
- [x] Complete the recovery test with `--include-ignored --yes`: assert exactly three A rows removed, B unchanged, and a new A path can now be recorded. Record a previously ignored A path and assert it becomes open, proving the documented loss of suppression is deliberate.
- [x] Extend the existing mixed-status prune test to exercise optional ignored cleanup. Assert unresolved open rows, inactive/prefix redirect cases and another site's rows remain. Preserve existing missing/unknown-site checks and ordinary prune automation coverage.
- [x] Update the full-capacity AdminNotice and list banner to name optional ignored cleanup and warn that removed ignored paths can appear again. Update existing full/partial/boosted list assertions to check the recovery guidance reaches each response shape.
- [x] Update operations and CONTEXT examples: ordinary `prune --site blog --dry-run`, ordinary `prune --site blog`, optional `prune --site blog --include-ignored --dry-run`, and confirmed `prune --site blog --include-ignored --yes`. Explain that cleanup is site-scoped, never automatic, loses removed history, and keeps unresolved open rows. Existing ignored records remain deliberately ignored until explicitly removed.
- [x] Add an Unreleased entry for optional ignored-record recovery and its confirmation requirement. Remove the stale statement that this command cannot reclaim ignored rows.
- [x] Run `uv run pytest tests/integration/test_notfound_limits.py tests/integration/test_notfound.py`. Expected: all pass, with exact output-line assertions and persisted recovery checks.

### Task 2: One redirect-coverage definition (#535 section 1)

**Files:** create `src/bragi/contrib/notfound/queries.py`; modify `src/bragi/contrib/notfound/admin.py` and `src/bragi/contrib/notfound/cli.py`. Existing checks live in `tests/integration/test_notfound.py` and `tests/integration/test_notfound_limits.py`.

**Interface:** `covered_by_exact_redirect(site_id: int) -> Exists`, importing `Exists` from `sqlalchemy.sql.selectable`.

- [x] Move the identical correlated EXISTS expression into the helper. Keep site ID, source path, EXACT match and active=True predicates unchanged; correlate against NotFound.
- [x] Use that expression in both list filtering and prune eligibility. Keep the DELETE's direct predicate evaluation and comment explaining concurrent status revalidation.
- [x] Run the existing list and prune tests. They already guard the definition; no duplicate helper-only test is needed for this behavior-preserving extraction.

### Task 3: Independent bounded warnings (#538 sections 1/6)

**Files:** `src/bragi/contrib/notfound/plugin.py`, `tests/integration/test_notfound_limits.py`, `src/bragi/settings.py`, `docs/operations.md`, `CONTEXT.md`, `CHANGELOG.md`.

**Interface:** Replace `_Window.warned: bool` with `warned_reasons: set[str] = field(default_factory=set)`. Keep `warn_once(site_id, budget, reason)` and both fixed reason strings.

- [x] Update `test_failures_use_attempt_budget_and_log_once_per_window` to assert both exact warning reasons once in each of two windows, with four admitted calls total. Keep assertions excluding attacker input and tracebacks. Rename the test to make per-reason behavior explicit.
- [x] Run the changed test against baseline and observe failure: the shared flag emits only one reason per window.
- [x] Check and add each fixed reason under the existing lock. Keep keys independent of paths, referrers and exception messages; no new logger or extra recording attempt.
- [x] Run the focused warning test, existing threaded-budget test and busy-recorder timeout test. Expected: both reasons remain visible, each bounded to once per site/worker/window, while 404 responses and attempt counts remain unchanged.
- [x] Update operations and CONTEXT to say once per reason/site/worker/window, at most two warnings for the current reasons.
- [x] Add a comment beside `notfound_max_rows` explaining that its bounded OFFSET index walk grows linearly with the configured cap. Recommend measuring before large increases; do not invent a performance threshold or redesign admission counters.
- [x] Add an Unreleased entry for independently reported recording failure and rate-limit warnings.

### Task 4: Topic verification and checklist outcomes

- [x] Run `uv run pytest tests/integration/test_notfound_limits.py tests/integration/test_notfound.py`.
- [x] Run all four repository gates: `uv run ruff check src/ tests/ alembic/`, `uv run ruff format --check src/ tests/ alembic/`, `uv run mypy src/`, `uv run pytest`. Inspect each exit status independently.
- [x] Sweep affected README/docs command references and review the diff for secrets, unrelated changes and inaccurate claims.
- [x] Record exact red/green regression commands and outcomes. Leave code uncommitted for review.

## Progress and rulings

- Planning inspected `ffa182f`. Tasks 1-3 are implemented and independently reviewed. Combined repository gates passed, as recorded below.
- Confirmation applies only to destructive `--include-ignored` cleanup. Ordinary prune already has narrow, site-scoped eligibility and an established noninteractive interface. Retrofitting `--yes` there would unnecessarily break existing automation. Every dry run remains available without confirmation.
- No ignored-to-dismissed migration: historical verification established that permanent ignore predates #519. Changing existing ignored status would discard deliberate operator decisions.
- All retained statuses still consume the cap. Excluding ignored rows would restore unbounded storage unless another limit were introduced. No extra table, schema change, automatic eviction or ignored-record UI is needed for the demonstrated blockage.
- Decline #537's recorder bind refactor. The short-lived Session accesses the configured SessionLocal proxy bind; the independently owned Core connection ensures busy-timeout restoration before pool return. Removing the Session would require another core API or private factory access without a demonstrated benefit. `typing.cast` itself cannot raise; a Connection-bound factory would fail at `.connect()`, but current production/test factories are Engine-bound.
- Correct #538's sequence claim: serial requests cannot first exhaust the budget and then attempt another write in the same window. Rate-first masking requires an already admitted concurrent request to fail later. The existing failure-first test can establish the shared dedupe defect directly by asserting both reasons.
- No new shared limiter or counter index. The accepted per-worker recording budget and bounded database admission remain unchanged.

- Ruling: begin this independent topic after topic2 implementation has started, while its worker handles the media files. No files or behavior interfaces overlap; this preserves priority and topic grouping while avoiding idle time. Reviews and evidence remain per topic.

- Tasks 1-3 implemented. Seven recovery/warning/banner regressions failed against baseline. First focused pass exposed missing site template context (three cases); fixed by passing the already-resolved Site. Ordinary prune keeps its interface; ignored cleanup requires explicit confirmation. No statuses migrated or excluded from the total cap.

- Focused verification: 43 tests passed in 11.16s. Ruff passed; mypy passed for 332 source files. Independent Granny spec/quality review found no actionable defects.

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
