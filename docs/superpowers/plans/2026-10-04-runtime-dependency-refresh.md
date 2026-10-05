# Bragi runtime and security dependencies implementation plan

> **For agentic workers:** Use `superpowers:executing-plans` for sequential implementation, with an independent reviewer before integration. Steps use checkboxes for tracking.

**Goal:** Refresh runtime dependencies and prevent installations from retaining versions covered by the known security fixes.

**Architecture:** Update dependency metadata and selected lock entries while preserving the current database and development-tool versions. Make application changes only when a demonstrated compatibility regression requires them.

**Tech stack:** Python 3.14, uv, Flask, Authlib, cryptography, Pydantic, Pillow, DuckDB, pytest.

**Spec:** The 2026-10-04 conversation's runtime/security batch and [Dependabot alert #4](https://github.com/sgaduuw/bragi/security/dependabot/4). This is a bounded dependency refresh, not a feature redesign.

**Tracking:** [#551](https://github.com/sgaduuw/bragi/issues/551), branch `chore/551-runtime-dependencies`.

## Targets and ownership

Direct upgrades:

| Package | Locked now | Target |
| --- | --- | --- |
| pydantic-settings | 2.14.1 | 2.15.0 |
| cryptography | 50.0.0 | 50.0.2 |
| Authlib | 1.7.2 | 1.8.0 |
| Gunicorn | 26.0.0 | 26.2.0 |
| DuckDB | 1.5.3 | 1.5.6 |
| Click | 8.4.1 | 8.5.0 |
| markdownify | 1.2.2 | 1.2.3 |
| pillow-avif-plugin | 1.5.5 | 1.6.0 |
| Pygments | 2.20.0 | 2.21.0 |

Runtime transitive targets: annotated-types 0.8.0, argon2-cffi-bindings 26.1.0, certifi 2026.7.22, cffi 2.1.1, charset-normalizer 3.5.2, idna 3.20, joserfc 1.7.5, MarkupSafe 3.0.4, packaging 26.3, Pydantic 2.13.5, pydantic-core 2.46.5, python-dotenv 1.2.4, SoupSieve 2.10, typing-extensions 4.16.0, typing-inspection 0.4.4, and Werkzeug 3.1.9.

Pydantic determines its compatible core version. Do not independently select the newer, incompatible pydantic-core 2.49.0 advertised by the package index.

Change only these published requirement floors, retaining the upper bounds:
- `pydantic-settings>=2.14.2,<3.0`
- `cryptography>=50.0.0,<51.0`
- `pillow>=12.3.0,<13.0.0`

Pillow's locked version already meets its floor. Execution also requires Flask >=3.1 for the per-request limit API used to preserve form-size protection under Werkzeug 3.1.9; the locked Flask version is unchanged. Leave remaining requirement ranges unchanged. Hold SQLAlchemy, Alembic, Mako, pytest, Ruff, mypy, ast-serialize, and librt at their starting versions.

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

1. Settings precedence, empty environment values, and startup validation: existing settings tests must retain their assertions.
2. Existing passwords, RSA signatures, and OAuth callbacks: exercise real hashing/signing and the existing OAuth state/error cases.
3. Native binary compatibility and image decoding: check supported Linux amd64/arm64 Python 3.14 wheels, AVIF/WebP, EXIF, and DuckDB behavior.
4. Real HTTPS and streamed decoding: exercise Bragi's guarded adapter, not only mocked requests.
5. Published dependency metadata: wheel requirements must enforce the three security floors, not just the lockfile.

## Task 1: Apply the runtime refresh

**Files:** Modify `pyproject.toml`, `uv.lock`, and `CHANGELOG.md`. Compatibility fixes, if demonstrated, belong only in the affected existing module with a regression in its existing test area.

**Interfaces:** Consumes the post-Dependabot develop baseline. Produces the refreshed runtime graph while retaining database and tool versions for the next plans.

- [x] Read the selected versions' upstream release notes and check removed APIs against Bragi's callers. Check the native distributions against the Python 3.14 Linux platforms used by the containers.
- [x] Apply the three requirement floors and targeted lock updates above. Run `uv sync --frozen`; compare all package versions before and after and account for every change.
- [x] Run the focused behavior checks:

```sh
uv run --frozen pytest -q tests/core/test_settings.py tests/unit/test_settings.py tests/contrib/test_auth_local.py tests/contrib/test_auth_github.py tests/contrib/test_activitypub.py tests/core/test_http_ssrf.py tests/core/test_renditions.py tests/unit/test_image_renditions.py tests/contrib/test_attachments.py tests/contrib/test_datasets_engine.py tests/contrib/test_datasets_render.py tests/contrib/test_import_ghost.py tests/contrib/test_import_wordpress.py tests/contrib/test_import_hugo.py tests/contrib/test_import_linkedin.py tests/contrib/test_markdown_extras.py
```

- [x] Repeat the real HTTPS GET/HEAD smoke with `bragi.core.http.safe_get` and `safe_head`. Fetch a public body larger than 8,192 bytes, then assert the capped response is exactly its first 8,192 bytes. Use no credentials. A network outage is inconclusive, not a dependency failure.
- [x] On a disposable migrated SQLite database, run Gunicorn's `--check-config` against `bragi.apps.admin:create_admin_app()` and `bragi.apps.delivery:create_delivery_app()`. Check the existing Docker command options still parse.
- [x] Run `uv build` and inspect wheel METADATA for the three minimum versions. A normal installation retaining pydantic-settings 2.14.1, cryptography 48.x, or Pillow 12.2 must no longer satisfy the wheel's requirements.
- [x] Add an Unreleased Security entry naming the fixed dependency floors and runtime refresh; run the shared gates and reviews.

**Done when:** Targeted runtime versions resolve and pass their checks; database/tool versions remain unchanged; published metadata enforces the security floors; alert #4's affected version is absent. After merge, read back the alert state and allow for GitHub's scan delay without dismissing it manually.

**Proposed commit:** `fix(deps): refresh runtime libraries and enforce security minimums`. Body explains the vulnerable-version exclusions, the bounded runtime scope, and validation.

**Rollback:** Revert this batch's code and lockfile together if needed. Any rollback of the security floors explicitly reintroduces the known exposure; prefer a targeted compatibility fix.

## Execution record

Implemented on `chore/551-runtime-dependencies` from develop
`5dd04fd6ae11f3c9a3aeeef7ca3ccfa75caaa11e`. Implementation and local verification completed before publication.

- Exactly 25 package versions changed. No packages were added or removed.
  Database and development-tool versions remain unchanged.
- [Werkzeug 3.1.9](https://werkzeug.palletsprojects.com/en/stable/changes/#version-3-1-9)
  removed the URL-encoded form memory limit. A small shared middleware guard
  preserves that limit in both apps, respects a smaller body cap, and rejects
  oversized streamed bodies without silently truncating form fields.
  Multipart uploads retain their existing allowance.
- This uses the public per-request limit setter introduced in
  [Flask 3.1](https://flask.palletsprojects.com/en/stable/api/#flask.Request.max_content_length),
  so the published minimum rises from 3.0 to 3.1. This excludes Flask 3.0
  installations instead of adding a compatibility shim.
- Before the guard, both 500,001-byte admin cases returned 400 after parsing
  instead of 413. After the fix, all 28 settings/form-limit cases passed,
  including both factories, exact boundaries, streams, smaller body caps,
  field round-trips, and a multipart upload.
- Focused runtime suite: 408 passed. Final full suite: 2,546 passed,
  one optional Playwright module skipped. Ruff passed; formatting passed
  for 571 files; mypy passed for 333 source files.
- Actual guarded HTTPS GET/HEAD passed. The capped response was exactly
  the first 8,192 bytes of a 78,807-byte public response.
- Grayscale and RGB AVIF images with orientations 1, 6, and 8 passed probing
  and AVIF/WebP/original rendition checks.
- Both Gunicorn factories accepted the existing Docker command options
  against a disposable, freshly migrated SQLite database.
- Built wheel metadata rejects the superseded security versions and Flask
  3.0. Flask 3.1.0 separately passed 12 boundary/streaming checks.
- Native wheels were verified in PyPI metadata for Linux amd64/arm64 and
  CPython 3.14. Linux execution was not performed.
- Independent correctness/security review found no actionable findings.
  Ponytail ultra found no unnecessary complexity. Diff and secret checks passed.

Commit, push, and PR creation were authorized on 2026-10-05. PR CI, merge
verification, and the post-merge alert readback remain delivery checks. A release and deployment are outside this batch.
