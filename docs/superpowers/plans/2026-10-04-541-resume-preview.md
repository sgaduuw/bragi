# Issue #541: Preserve incomplete resume input during preview

**Goal:** Preserve incomplete resume input during preview.

**Architecture:** Render submitted resume form data without requiring it to satisfy the persisted ResumeData schema. Keep strict validation at Save. Use an explicit form-only representation with defaults for missing sections and shape checks for malformed data; do not bypass validation on model persistence. Preserve row order, ids, text, blanks, links, and all sections.

**Tech stack:** Flask, SQLAlchemy, Jinja, browser JavaScript, pytest and Playwright.

**Spec:** https://github.com/sgaduuw/bragi/issues/541


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

1. Empty required fields, partial rows and row ordering: integration regression across new/live/working-copy editors.
2. Save with invalid data: response preserves input and database remains unchanged.
3. Malformed JSON and malformed nested shapes: rendering cannot raise; ordinary malformed input remains rejected by Save.
4. Template collisions (notably skill `items`) and escaping: exercise real fieldset output.
5. Existing GET and successful Save contracts: existing resume tests, plus focused checks as necessary.

## Task 1: Implement and prove #541

**Files:** `src/bragi/contrib/page/admin.py`, resume admin templates only if needed; `tests/integration/test_resume_preview.py`; existing resume tests as appropriate; `CHANGELOG.md`.

**Interfaces:** consumes raw `resume_data` JSON from `_form_from_request`; produces display-only data for `_resume_fieldset.html`. `_validate_resume_data` remains the persistence gate. Shared `_render_page_form` must remain compatible with #542 and #543.

- [x] Copy the review regression to `tests/integration/test_resume_preview.py`; add database non-mutation and subsequent rejected Save assertions. Expand section coverage to the form structures that the representation touches.
- [x] Run `uv run pytest -q tests/integration/test_resume_preview.py`. Expected before fix: incomplete submitted company/text replaced in new/live/copy cases.
- [x] Implement the smallest form-only rendering change. Keep persisted ResumeData validation strict and avoid generic recursive schema machinery.
- [x] Run the regression plus existing resume tests located with `rg --files tests | rg resume`. Expected: all pass; no mutation during preview or rejected Save.
- [x] Add the #541 changelog entry, run Ruff/format on touched Python files, inspect diff and record commands/results in the task report.
- [x] Independent task review must approve issue compliance and code quality; then proceed to #542.

Proposed commit: `fix(editor): preserve incomplete resume input during preview`.

## Completion evidence

Implemented and independently reviewed. Combined gates: 2523 pytest tests passed; 12 browser cases passed separately; Ruff, formatting and mypy passed. The fix, tests and changelog entry belong to this issue's commit on `fix/541-543-editor-previews`, included in the shared PR against `develop`. Detailed commands, per-issue red/green evidence and final results are retained in `.superpowers/sdd/` for this worktree.
