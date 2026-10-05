# Bragi development tooling refresh implementation plan

> **For agentic workers:** Use `superpowers:executing-plans` for sequential implementation, with an independent reviewer before integration. Steps use checkboxes for tracking.

**Goal:** Update pytest, mypy and Ruff while retaining the strength and consistency of the development and CI checks.

**Architecture:** Refresh the dev group after runtime and database changes are verified. Keep mechanical formatting separate from type or test compatibility fixes. Validate the final wheel independently of the editable development environment.

**Tech stack:** pytest, mypy, Ruff, uv, GitHub Actions, Python 3.14.

**Spec:** The 2026-10-04 conversation's tooling batch and [Ruff 0.16 release notes](https://github.com/astral-sh/ruff/releases/tag/0.16.0).

**Tracking:** [#555](https://github.com/sgaduuw/bragi/issues/555). Branch: `chore/555-development-tooling`. Implementation authorized before the batch 1 and 2 publication interlude; resumed 2026-10-05.

## Targets and ownership

- pytest 9.0.3 -> 9.1.1; existing `>=9.0.3,<10` range can remain.
- mypy 2.1.0 -> 2.4.0; existing `>=2.1,<3` range can remain.
- Ruff 0.15.16 -> 0.16.10; change the requirement to `ruff>=0.16.10,<0.17`.
- Tool transitive dependencies: ast-serialize 0.12.1 and librt 0.16.0, subject to mypy's constraints.
- Keep the completed runtime and database versions unchanged. Preserve current Ruff selectors, line length 100, target Python 3.14 and mypy strictness.

## Shared execution rules

- Planning baseline: develop `5dd04fd6ae11f3c9a3aeeef7ca3ccfa75caaa11e`, after Dependabot PRs #546-#549. Its exact tree passed 2,523 tests, Ruff, formatting, mypy, and real guarded HTTPS checks. The optional browser module was tested separately during the release.
- Execute sequentially: runtime/security, database, tooling. Base each branch on develop after the previous batch merges. Use an isolated worktree and preserve the shared checkout.
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

1. New lint/formatter findings must produce mechanical changes without altered application behavior.
2. New mypy errors must be resolved with correct types, not broad ignores or disabled checks.
3. pytest must still collect the existing suite and execute its assertions; new skips or lost tests require explanation.
4. CI, Makefile commands and local execution must use the same locked tool versions.
5. The final built wheel must work with both the locked runtime graph and fresh dependency resolution, since release containers install from PyPI rather than uv.lock.

## Task 1: Update tools and reconcile findings

**Files:** Modify `pyproject.toml`, `uv.lock`, and only Python files requiring new-tool compatibility. Inspect `Makefile`, `.github/workflows/ci.yml`, `AGENTS.md`, and `docs/development.md`; change them only if their commands become inaccurate.

**Interfaces:** Consumes the runtime/database branch results and unchanged production behavior. Produces one tool configuration used by the local checks and CI.

- [x] Record pytest collection count and the existing optional-browser skip on the completed database baseline. Read pytest 9.1, mypy 2.2-2.4 and Ruff 0.16 compatibility notes.
- [x] Widen the Ruff constraint as above and target only the five listed tool packages during lock resolution. Run `uv sync --frozen`; assert the runtime graph did not move.
- [x] Run Ruff check/format-check and mypy before applying fixes; retain the output distinguishing formatting, typing, and real behavior findings.
- [x] Apply safe Ruff fixes and formatting. Inspect every transformation and keep formatting-only changes in a separate commit if broad. Do not apply unsafe fixes automatically or reduce enabled rules.
- [x] Fix demonstrated type/test compatibility issues narrowly. A real application behavior change requires a retained regression observed failing first; a formatting-only change does not need new tests.
- [x] Run `uv run --frozen pytest --collect-only -q`; compare with the baseline and explain all count changes. Run the shared gates and confirm no additional unexplained skips.
- [x] Run the optional browser tests separately with Playwright, since the tooling upgrade changes their test runner too. Record results independently from the default suite.

## Task 2: Verify the complete dependency refresh as an installed package

**Files:** Build artifacts and checks in disposable scratch directories only. Add a concise Unreleased summary for the completed dependency refresh if the earlier batch entries do not already cover the user-visible changes.

**Interfaces:** Consumes the final wheel built from all three batches. Produces installation/startup evidence for the packaging model used by release containers.

- [x] Run `uv build`. Export the exact runtime graph with `uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements.txt` into a scratch file.
- [x] Create one clean Python 3.14 environment with the exported runtime requirements and install the actual built wheel using `--no-deps`. Run `uv pip check` against that interpreter and verify imports come from site-packages.
- [x] Create a second clean environment and install the actual local wheel with ordinary dependency resolution. Compare resolved runtime versions against the validated graph. Investigate any differences rather than assuming the lock controls this installation.
- [x] In both environments, verify package version and Alembic head, perform migration/startup smoke checks on disposable databases, and load both WSGI factories through Gunicorn. Confirm development tools are not required to run the package.
- [x] Verify current Linux amd64 and arm64 wheel availability for the selected native runtime dependencies. If performing image smoke checks, install the candidate wheel into disposable images; the existing Dockerfiles with `BRAGI_VERSION=1.54.0` would install the old published package and cannot verify this candidate.
- [x] Finish the independent review, inspect the final diff for unintended behavior changes and secrets, and prepare the tooling PR draft.
- [ ] Run PR CI and integrate; then re-query outdated packages and security alerts and record any intentional exclusions.

**Done when:** The same strict checks pass locally and in CI, collection is intact, runtime dependencies stay at their tested targets, and the built wheel passes locked and fresh-install smoke checks.

**Proposed commits:** `chore(dev): update pytest, mypy and Ruff`; a separate `style: apply Ruff 0.16 formatting` only if formatting churn warrants it. One tooling PR.

**Rollback:** Revert the tool constraints, lock changes and associated compatibility/format changes together. No production data change is involved.

## Execution evidence (2026-10-05)

Base: `f9430432a297c9c7b0af2d253ca8734350af7de2`. Its tracked tree exactly matches tested database head `2cba9da`; the prior 2,547-test baseline remains applicable. Worktree: `.worktrees/bragi-tooling-dependencies`.

Only the five planned tool packages changed. Runtime requirements, production code, lint selectors, line length, Python target, pytest settings, and mypy strictness are unchanged. No formatting or compatibility edits were needed. Makefile, CI, AGENTS.md, and development instructions still invoke the same locked tools.

Compatibility notes reviewed: [pytest 9.1](https://docs.pytest.org/en/stable/changelog.html#pytest-9-1-0-2026-06-13), [mypy 2.2 through 2.4](https://mypy.readthedocs.io/en/stable/changelog.html), and [Ruff 0.16](https://github.com/astral-sh/ruff/releases/tag/0.16.0). Ruff's expanded defaults do not replace our explicit selectors. Its formatter additionally checks the existing Markdown fixture README, explaining 573 files versus 572. No loop or context-manager type comments rely on behavior omitted by mypy's new default native parser. Pytest's fixture and parametrization changes produced no new warnings, lost tests, or skips.

Validation:
- All 2,547 collected test IDs and their order match the baseline.
- Ruff lint and formatting passed; strict mypy passed all 333 source files.
- Full pytest: 2,547 passed in 232.14 seconds; one optional Playwright module skip.
- Optional browser tests: 12 passed. Documented editor recovery script passed.
- `uv build` produced the wheel and sdist. Two clean Python 3.14.4 environments installed the actual candidate wheel: one with the exported runtime graph and `--no-deps`, one with ordinary dependency resolution. Both contain the same 43 runtime dependencies plus Bragi and pass `uv pip check`.
- Both installed wheels import from site-packages without pytest, mypy or Ruff, complete fresh migration upgrade/downgrade/upgrade, report head `f515c0ffee01`, and pass SQLite integrity and foreign-key checks.
- Both environments serve `/healthz` through the admin and delivery Gunicorn factories with the container worker counts and timeout flags.
- Binary-only resolution of the exact 43-package runtime graph passes for Python 3.14 on Linux amd64 and arm64. Runtime execution was on macOS; Linux runtime execution is not claimed.

Evidence logs and disposable smoke scripts are in `.superpowers/sdd/2026-10-04-tooling-dependency-refresh/`. Independent Granny review found no actionable correctness or security findings; Ponytail ultra found no unnecessary complexity. The reviewer verified the diff, artifact hashes, unchanged graph/settings, exact collection, and saved validation evidence without rerunning the suite. Final documentation-only rebuild produced the identical tested wheel (SHA-256 `b41231285796853b9e039a5eecc548e23e47648cd4957c3fad01e3f5ae8189e4`). Commit, push, and PR creation were authorized on 2026-10-05 after implementation and review. PR CI, integration, and post-integration dependency/security readback remain delivery steps. Release and deployment remain outside this batch.
