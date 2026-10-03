# Safe media deletion implementation plan

> **For agentic workers:** Use `superpowers:executing-plans` task by task. Track completed checks below and record deviations with their evidence.

**Tech stack:** Flask, SQLAlchemy, migrated SQLite, Jinja, markdown-it-py and pytest. Reuse existing dependencies.

## Global constraints

- Worktree: `bragi-topics`, baseline `ffa182f`. No dependencies or schema changes unless a demonstrated requirement makes them necessary.
- User authorized planning and implementation of all five topics on 2026-10-03. The user authorized five topic commits with explanatory bodies at 22:37 CEST. Push, PR creation, issue closure and deployment remain separate actions.
- Markdown remains canonical. Preserve contrib boundaries and one supplied session for lifecycle writes. Content and hooks commit once.
- Observe meaningful regression failures before implementation. Use migrated file-backed SQLite and independent connections for transaction behavior.
- Keep disputed issue claims as explicit outcomes, rather than implementing them automatically. #520 and importer/block-tree work stay deferred.
- Run Ruff lint, Ruff formatting, mypy and the full pytest suite before final completion. Update CHANGELOG and affected operator documentation.
- Execute after topic 1. Reuse its resolved authentication contract and lock helper; do not introduce separate authentication behavior in these routes.

**Goal:** Media deletion presents complete known usage without holding SQLite's writer lock during Markdown parsing or preview rendering, and refuses confirmation when its inputs change.

**Spec:** GitHub `sgaduuw/bragi` #530 and #534; media portions of #535 and #537. Read descriptions and comments as evidence, not as unquestioned implementation instructions.

**Architecture:** Load an immutable snapshot of the existing scanner's inputs, then parse it outside the writer lock. A valid confirmation acquires the lock, refreshes authorization, and compares fresh input values and selected attachment fingerprints before deletion. Keep reference checks and storage removal inside the existing deletion transaction.

## Review focus

- A post, page, working copy or revision gains a reference after scanning but before the lock.
- Site aliases, default images or rendition mappings change without modifying the selected attachment row.
- Invalid confirmation and conflict rendering leave independent writers free to proceed.
- Literal code and sentence punctuation must not hide media usage; dotted rendition filenames remain valid.
- Storage deletion retains the lock across flush, reference checks, unlink and commit; shared bytes survive.

## Algorithm and boundaries

Introduce frozen `MediaSource` and `MediaUsageInputs` records in `src/bragi/core/media_usage.py`. Their collections are deterministic tuples, ordered by source type and primary key, containing plain values rather than ORM instances.

`MediaSource` contains `source_type`, `source_id`, `content_id`, `title`, effective `status`, `body_markdown` and `featured_image_id`. Effective status remains `revision` or `working_copy` for those source types, matching current previews.

`MediaUsageInputs` contains:

- Site ID, hostname, canonical URL, title and default featured-image ID.
- This site's alias hostnames.
- This site's attachment ID to storage-key mapping.
- This site's rendition attachment IDs and storage keys, including legacy keys.
- All scoped Post, Page, PostWorkingCopy, PageWorkingCopy, PostRevision and PageRevision sources. Revision scope continues to follow the parent content's site.

Expose `media_usage_inputs(db: Session, site: Site) -> MediaUsageInputs`. Keep existing callers compatible by adding optional keyword `inputs: MediaUsageInputs | None = None` to `media_usage` and `attachment_usage`. Both parse the supplied snapshot or load one themselves. The loader does no Markdown parsing, template rendering, relationship-wide fingerprinting or storage access. Reading exact values, rather than timestamps alone, detects insertions, deletions, moved sources and relevant field changes.

In the shared attachment confirmation path:

1. Authorize, validate the selection and early bulk limit, load selected attachments, capture their existing fingerprints, load scanner inputs, and parse usage.
2. Compare the existing signed confirmation state, including user, site, selection, attachment fingerprints and computed references. Keep its 900-second expiry and acknowledgement requirement. Missing or invalid confirmation renders immediately without a writer lock.
3. For an accepted confirmation, call `lock_editor_write`, resolve the site and permissions again, reload selected attachment fingerprints and scanner inputs, and compare them with the values actually scanned.
4. On change, roll back before rescanning or rendering. Force a new preview even if the old token happens to match the recomputed reference list. Preserve submitted IDs and htmx response behavior.
5. On equality, delete through `_delete_one_attachment` and commit once. Reload/check the single-route target after lock acquisition so expired ORM state cannot turn disappearance into an accidental 500.

The freshness reread remains O(content bytes). It removes Markdown parsing and Jinja rendering from the lock; it is not an O(1) index. Keep a short `ponytail:` comment identifying that ceiling and a measured slow reread as the trigger for an index. Reject `PRAGMA data_version` here: it requires retaining the same physical connection and would invalidate deletion for unrelated session, analytics and 404 writes. Reject attachment-only fingerprints and `updated_at`-only checks because neither covers all scanner inputs.

### Task 1: Literal media reference coverage (#534)

**Files:** `src/bragi/core/media_usage.py`, `tests/test_media_usage.py`.
**Interfaces:** Preserve `MediaReference` and scanner return values; normalization changes only literal candidate handling.

- [x] Add parameterized `test_usage_includes_literal_media_urls` cases for inline code, sentence-final punctuation, fenced code and raw HTML. Assert exactly the expected local key and source identity. Include dotted rendition filenames and foreign-host rejection.
- [x] Run `uv run pytest tests/test_media_usage.py`. Expected before the fix: inline-code and sentence-final original URL cases fail on missing references.
- [x] Exclude backticks from literal URL matches. Consider the original literal candidate and a candidate with trailing sentence punctuation removed, then retain `_KEY.fullmatch` validation. Preserve parsed Markdown destinations and periods inside rendition filenames.
- [x] Correct the parser comment to describe deterministic base CommonMark parsing plus conservative literal scanning. Do not claim parity with delivery's full plugin renderer or add a new public renderer API.
- [x] Run `uv run pytest tests/test_media_usage.py`. Expected: all cases pass, including existing entity/encoded-path and alias coverage.

### Task 2: Snapshot freshness and short deletion transactions (#530)

**Files:** `src/bragi/core/media_usage.py`, `src/bragi/contrib/attachments/admin.py`, new `tests/integration/test_media_deletion_lock.py`, existing `tests/contrib/test_attachments.py` and `tests/contrib/test_attachments_admin_bulk.py`.
**Interfaces:** Add `MediaSource`, `MediaUsageInputs` and `media_usage_inputs` as specified above. Existing scanner callers remain valid. Keep `_delete_one_attachment(db, site, row)` and its single-commit caller contract.

- [x] Add `test_deletion_parses_and_renders_without_writer_lock`, parameterized for single and bulk routes, initial preview and invalid confirmation. At actual parser and Jinja execution, let an independent connection acquire `BEGIN IMMEDIATE` and roll it back. Assert probe counts and unchanged attachment rows and bytes.
- [x] Add `test_deletion_refuses_changed_scan_inputs`: commit a change through an independent connection after scanning but before the route claims the lock. Cover new/removed or changed content, working copies/revisions, aliases/default image, selected attachment metadata and rendition keys. Assert the old confirmation does not delete; a current preview appears and a subsequent fresh confirmation works.
- [x] Add `test_deletion_rechecks_role_before_mutation` with a committed role revocation at the same boundary. Assert rejection and retained bytes.
- [x] Add `test_deletion_holds_writer_lock_through_storage_removal`: at real backend removal, an independent writer with a short busy timeout cannot acquire the lock; after the response it can. Assert the probe ran. Retain the existing shared-attachment and dataset byte-preservation tests.
- [x] Run `uv run pytest tests/integration/test_media_deletion_lock.py`. Observe the expected baseline lock failure during parsing/rendering, and record any later race regression that requires the partially reordered implementation to expose its failure.
- [x] Implement the shared snapshot loader and scanner reuse, then reorder both routes according to the algorithm. On every rejected branch, release the writer transaction before expensive work. Do not move storage unlink out of the existing protected transaction.
- [x] Shorten touched deletion comments while retaining the actual lock invariant and the already-documented residual upload-side race. Do not expand this task into an upload redesign.
- [x] Run `uv run pytest tests/test_media_usage.py tests/contrib/test_attachments.py tests/contrib/test_attachments_admin_bulk.py tests/integration/test_media_deletion_lock.py`. Expected: all pass; token expiry, acknowledgement, cross-site filtering and htmx behavior remain covered.

### Task 3: One early bulk limit (#535)

**Files:** `src/bragi/core/bulk_action.py`, `src/bragi/contrib/attachments/admin.py`, `tests/unit/test_bulk_action.py`, `tests/contrib/test_attachments_admin_bulk.py`.
**Interfaces:** Define `DEFAULT_MAX_BATCH = 200`; use it as the default for both `bulk_delete` and `check_bulk_limit(ids: Sequence[int], *, max_batch: int = DEFAULT_MAX_BATCH) -> None`, raising the existing `BulkLimitExceeded` with the existing message. Both `bulk_delete` and the early attachment guard call it; preserve custom `max_batch` callers.

- [x] Add `test_bulk_delete_limit_precedes_media_scan`: submit 201 IDs, assert the warning and no scanner execution. Include author-role rejection before the limit response and the existing 200-item boundary.
- [x] Run `uv run pytest tests/unit/test_bulk_action.py tests/contrib/test_attachments_admin_bulk.py`. Record baseline behavior; the new coverage protects the extraction even where current behavior is already correct.
- [x] Extract the shared check and message, call it before media scanning and inside `bulk_delete`, and remove the attachment route's now-redundant later exception handler. Keep the batch bound at 200.
- [x] Run the same focused tests. Expected: all pass with one threshold/message definition and no pre-limit parsing.

### Task 4: Checklist outcomes, documentation and final checks

**Files:** `src/bragi/core/media_usage.py` where already touched, `CHANGELOG.md`, this plan; inspect `docs/recovery.md` and `README.md` for affected claims.

- [x] Resolve #537's dictionary `.in_` readability item as part of the snapshot work, using explicit keys where the expression remains.
- [x] Record the storage-existence suggestion as deferred: `StorageBackendSpec` has only `store`, `read` and `remove`; no concrete correctness defect or measured existence-check bottleneck justifies expanding that public contract. Do not silently claim that `backend.read` became a cheap existence check.
- [x] Add Unreleased entries for reliable literal reference detection and deletion confirmation without parsing/rendering under the writer lock. Correct operator documentation only where the changed behavior invalidates a claim; no unrelated README rewrite.
- [x] Run `uv run ruff check src/ tests/ alembic/`.
- [x] Run `uv run ruff format --check src/ tests/ alembic/`.
- [x] Run `uv run mypy src/`.
- [x] Run `uv run pytest`.
- [x] Inspect the topic diff for secrets, changed transaction exits and unrelated edits. Record results and remaining limits below; leave changes uncommitted.

## Issue outcomes and progress

- #530: implemented immutable snapshots and a locked comparison of exact scanned inputs and selected attachment fingerprints. Parsing and preview rendering happen without the writer lock; storage removal retains it. Raw input reread remains linear in content size.
- #534: literal code spans and sentence punctuation are recognized, dotted rendition names remain valid, and the parser comment now describes base CommonMark accurately.
- #535: the early attachment guard and bulk loop share the 200-item default, check, and message. Default/custom boundaries and author rejection are covered. Redirect coverage and scheduling outcomes belong to topics 3 and 1.
- #537: the dictionary `.in_` query disappeared with snapshot-backed rendition matching. Storage existence API expansion remains deferred; `backend.read` is unchanged. Remaining editor and recovery items belong to their owning topics.
- Tasks 1 to 3 completed in the uncommitted topic slice. Final focused run: `uv run pytest tests/test_media_usage.py tests/unit/test_bulk_action.py tests/contrib/test_attachments.py tests/contrib/test_attachments_admin_bulk.py tests/integration/test_media_deletion_lock.py -q --tb=short` passed 184 tests in 25.86 seconds.
- Owned-path Ruff lint and formatting checks passed; mypy passed for all three changed source files. The main thread completed shared CHANGELOG updates, full repository gates and independent review; results are recorded below.
- Inspected README and `docs/recovery.md`: their known-usage and backup claims remain accurate; no operator correction is required for this topic.
- Ruling: scanner sources use explicit SQL columns rather than ORM instances, avoiding eager Post tag and featured-image loading under the writer lock. Records remain frozen plain values with deterministic ordering.
- Ruling: added the 200-item boundary coverage because the plan's claimed existing attachment-boundary test was absent. Existing behavior passed before extraction; disabling the early guard failed the new scan-order test.
- Evidence and remaining limits: `.superpowers/sdd/2026-10-03-02-media-deletion/worker-report.md`. No commits, push, publication or deployment performed.

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
