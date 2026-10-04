# Issue #543: Make unsaved metadata descriptions match Save

**Goal:** Make unsaved metadata descriptions match Save.

**Architecture:** Use the saved excerpt when rendering a saved GET preview. For a submitted editor form, compute the excerpt from submitted Markdown using the same rule as Save even if the body text is unchanged. Keep the public saved description and explicit metadata overrides unchanged until Save.

**Tech stack:** Flask, SQLAlchemy, Jinja, browser JavaScript, pytest and Playwright.

**Spec:** https://github.com/sgaduuw/bragi/issues/543


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

1. Imported custom excerpts with unchanged Markdown: real preview, Save and delivery comparison for posts/pages.
2. GET before Save still shows the existing stored custom excerpt.
3. Explicit metadata descriptions override the generated excerpt on all surfaces.
4. New/live/working-copy form variants: shared helper behavior and existing metadata matrix; add coverage where needed.
5. Preview non-mutation and rollback: read the database before and after the preview; no saved receipt.

## Task 1: Implement and prove #543

**Files:** `src/bragi/contrib/post/admin.py`, `src/bragi/contrib/page/admin.py`, `tests/integration/test_metadata_excerpt_parity.py`, `tests/contrib/test_metadata_editor.py` if needed, `CHANGELOG.md`.

**Interfaces:** consumes `request.method`, the submitted Markdown and stored content. Produces the candidate `body_excerpt` passed to `effective_metadata`. Shared page renderer already contains #541/#542 changes, which must be preserved.

- [x] Copy the review regression to `tests/integration/test_metadata_excerpt_parity.py`. Assert saved GET uses the custom excerpt and preview leaves the database unchanged.
- [x] Run `uv run pytest -q tests/integration/test_metadata_excerpt_parity.py`. Expected before fix: both post/page preview descriptions differ from delivery after Save.
- [x] Make the minimal distinction between saved GET and unsaved submitted-form excerpt selection in both render helpers.
- [x] Run `uv run pytest -q tests/integration/test_metadata_excerpt_parity.py tests/contrib/test_metadata_editor.py`. Expected: all pass, explicit overrides and existing new/live/copy cases remain valid.
- [x] Add #543 changelog entry; lint/format touched Python; inspect diff and record red/green results.
- [x] Independent task review must approve issue compliance and code quality. Run combined final gates and request whole-diff Granny review.

Proposed commit: `fix(metadata): preview the excerpt that saving will produce`.

## Completion evidence

Implemented and independently reviewed. Combined gates: 2523 pytest tests passed; 12 browser cases passed separately; Ruff, formatting and mypy passed. The fix, tests and changelog entry belong to this issue's commit on `fix/541-543-editor-previews`, included in the shared PR against `develop`. Detailed commands, per-issue red/green evidence and final results are retained in `.superpowers/sdd/` for this worktree.
