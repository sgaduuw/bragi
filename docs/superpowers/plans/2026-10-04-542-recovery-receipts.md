# Issue #542: Retain recovery ancestry through unsaved editor responses

**Goal:** Retain recovery ancestry through unsaved editor responses.

**Architecture:** Carry the identity of the submitted recovery snapshot through server preview/validation/conflict re-renders, then attach it as ancestry to the freshly initialized browser record. Keep a fresh record id per editor instance. A successful receipt can clear only the acknowledged snapshot and unchanged ancestors; later typing and other tabs remain recoverable. Retain the existing timestamp guards and do not delete records just because a preview returned.

**Tech stack:** Flask, SQLAlchemy, Jinja, browser JavaScript, pytest and Playwright.

**Spec:** https://github.com/sgaduuw/bragi/issues/542


## Global constraints

- Work only in `fix/541-543-editor-previews`, based on develop `dfbb04241dac83cf5e52b79064b56bab0c3da695`.
- The issue is the specification. Keep production changes bounded to its acceptance criteria.
- Preserve validation, optimistic edit tokens, account/site/entity isolation and transaction rollback on unsaved responses.
- Keep actual-code regression tests. Observe the intended failure before implementation and retain the passing test.
- Add a user-facing Fixed changelog entry. No version changes, merges or release actions. The user authorized a separate commit per issue and one shared PR against `develop`.
- Baseline at the identical develop revision: 2516 pytest tests passed, Ruff/format/mypy and browser recovery checks passed. Fresh worktree Ruff baseline also passes.
- Each issue has one implementation task with its own red/green evidence and independent task review. Execute sequentially because the editor rendering paths overlap.
- Final combined validation: `uv run ruff check src/ tests/ alembic/`, `uv run ruff format --check src/ tests/ alembic/`, `uv run mypy src/`, `uv run pytest`, and both existing and added browser checks. Expected: exit 0 for each.

## Review focus

1. Preview then Save, including repeated previews: real Flask route plus Chromium regression for posts and pages.
2. Validation or conflict re-render: submitted ancestry survives but rejected requests never acknowledge Save.
3. Further typing during an in-flight submission: existing browser race regression and any added targeted case.
4. Another tab modifies a source snapshot: version/scope checks prevent deletion or adoption of newer/unrelated writing.
5. Unchanged form after preview, restored sources, working copies and storage unavailable: verify lineage is persisted even without a new edit; exercise existing browser guards.

## Task 1: Implement and prove #542

**Files:** `src/bragi/static/admin/editor-recovery.js`; post/page editor templates; `src/bragi/core/editor_state.py` or render helpers only if a shared validated response value is needed; `tests/browser/test_recovery_receipts.py`; `tests/browser/check_editor_recovery.py`; relevant server tests; `CHANGELOG.md`.

**Interfaces:** `_recovery_id` identifies an exact submitted local snapshot. Server responses without a save must preserve enough snapshot identity to link the replacement editor safely. `_edit_token` remains unchanged on unsaved responses. Receipt cleanup must still honor ancestor version guards.

- [x] Integrate the review receipt test under `tests/browser/test_recovery_receipts.py`; use explicit optional Playwright collection so default pytest needs no new dependency. Document the browser command and run it explicitly with Playwright.
- [x] Run `uv run --with playwright pytest -q tests/browser/test_recovery_receipts.py`. Expected before fix: posts and pages commit the edit but leave the original recovery record.
- [x] Add focused cases for repeated previews, rejected Save followed by correction, unchanged-after-preview Save, and a newer source/other tab where practical. Use actual routes and browser storage.
- [x] Implement response ancestry propagation, fresh instance ids, and guarded source cleanup. Avoid overwriting an existing snapshot on form initialization.
- [x] Run the new browser pytest file and `uv run --with playwright python tests/browser/check_editor_recovery.py`; run relevant editor-state integration tests. Expected: all pass, unrelated/newer edits remain.
- [x] Add #542 changelog entry; lint/format touched Python; inspect diff and record exact red/green results.
- [x] Independent task review must approve issue compliance and code quality; then proceed to #543.

Proposed commit: `fix(editor): retain recovery receipts across unsaved responses`.

## Completion evidence

Implemented and independently reviewed. Combined gates: 2523 pytest tests passed; 12 browser cases passed separately; Ruff, formatting and mypy passed. The fix, tests and changelog entry belong to this issue's commit on `fix/541-543-editor-previews`, included in the shared PR against `develop`. Detailed commands, per-issue red/green evidence and final results are retained in `.superpowers/sdd/` for this worktree.
