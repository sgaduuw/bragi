# Effective metadata previews implementation plan

> **For agentic workers:** Use `superpowers:executing-plans` task by task. Track completed checks below and record deviations with their evidence.

**Tech stack:** Flask, SQLAlchemy, migrated SQLite, Jinja, pytest; existing browser checks for JavaScript.

## Global constraints

- Worktree: `bragi-topics`, baseline `ffa182f`. No dependencies or schema changes unless a demonstrated requirement makes them necessary.
- User authorized planning and implementation of all five topics on 2026-10-03. The user authorized five topic commits with explanatory bodies at 22:37 CEST. Push, PR creation, issue closure and deployment remain separate actions.
- Markdown remains canonical. Preserve contrib boundaries and one supplied session for lifecycle writes. Content and hooks commit once.
- Observe meaningful regression failures before implementation. Use migrated file-backed SQLite and independent connections for transaction behavior.
- Keep disputed issue claims as explicit outcomes, rather than implementing them automatically. #520 and importer/block-tree work stay deferred.
- Run Ruff lint, Ruff formatting, mypy and the full pytest suite before final completion. Update CHANGELOG and affected operator documentation.
- Execute after topics 1 through 4. Preserve their authentication, conflict-token, transaction and browser-recovery behavior.

**Goal:** Authors can inspect illustrative search and social cards for saved or unsaved post/page content, using the effective values delivery emits.

**Spec:** GitHub `sgaduuw/bragi` #522, including its acceptance criteria and comments. Custom editors currently omit metadata controls although the models, working-copy field lists and delivery support them.

**Architecture:** Expose the existing fields and extract the existing metadata fallback rules into one core resolver used by delivery and admin. The existing forms gain a named, read-only preview action. Reuse the featured-image picker, image resolver, profile view, excerpt generator and URL helpers; no JavaScript metadata calculations, new routes or separate preview metadata pipeline.

## Review focus

- An explicit empty field clears an override; an older client omitting metadata preserves existing values.
- Previewing a published item or working copy neither saves nor consumes an edit token or recovery receipt.
- Working-copy row IDs and editor IDs must not replace the live content identity and author.
- Profile-page bio/avatar and blog-index pagination are real exceptions to the common fallbacks.
- Unsafe canonical URLs, foreign-site images/parents, missing public URLs and raw HTML input must remain safe and explainable.

## Decisions and interfaces

### Unsaved interaction

Add a native `type="submit"` button named `_metadata_preview`, value `1`, with `formnovalidate`, labelled **Update previews**. Place it after the existing primary Save button in DOM order so implicit Enter retains its current save behavior. It submits to the form's existing new/edit/working-copy-save endpoint. Keep the current normal and boosted submission mechanisms; no new JavaScript is needed to serialize metadata. Existing TipTap submit handling supplies current Markdown.

All six endpoints branch on this action after their normal read authorization and object/site checks, before `lock_editor_write`, model mutation, revision creation, tags, hooks, audit or `editor_saved()`. For working copies, load and validate the copy under the normal editor permission before rendering. Return the full existing editor form with submitted values and the original `_edit_token`. A stale token does not forbid a read-only preview and does not become fresh; a later save must still conflict. Keep the recovery ID as unacknowledged submitted state. Do not require scheduling, pinning or resume-save validation merely to preview metadata.

On GET, show the saved values. On explicit preview, describe the cards as **Unsaved preview. Nothing has been saved or published.** The preview reflects that submission; typing afterward requires another Update previews action. Reuse the submitted values on ordinary validation/conflict responses too, after the rollback boundary introduced in topic 1.

Use plain transient `Post` or `Page` instances containing only the metadata inputs, never `db.add`, `merge`, relationships or mutation of an attached row. For unchanged stored Markdown, use its stored excerpt. When submitted Markdown differs, derive the excerpt with the existing `make_excerpt`; all rendering remains outside the writer lock.

### Shared effective values

In `src/bragi/core/seo.py`, add a read-only `MetadataItem` protocol covering `title`, `meta_title`, `meta_description`, `body_excerpt`, `canonical_url`, `noindex` and `featured_image_id`. It must accept the existing ORM content and working-copy delivery view objects without importing sibling plugins.

Add frozen `EffectiveMetadata` with `title`, `document_title`, `description`, `canonical_url`, `image_url`, `noindex`, plus `title_source`, `description_source`, `canonical_source` and `image_source` strings for the visible explanations.

Add:

`effective_metadata(*, item: MetadataItem, site: Site | None, public_path: str | None, db: Session, page_kind: str | None = None, profile: ProfileView | None = None) -> EffectiveMetadata`

This extracts existing delivery decisions, rather than creating alternate rules:

| Value | Existing behavior to preserve |
| --- | --- |
| Social title | Nonempty `meta_title`, otherwise content title. |
| Search/document title | Effective title followed by ` · ` and site title, matching public `<title>`. |
| Description | `meta_description`, otherwise stored/generated body excerpt. For PROFILE pages, author `profile.bio_text` precedes the body excerpt. Empty result means the tag is absent. |
| Canonical URL | Explicit override, otherwise `site.base_url + public_path` when both exist. Never derive it from the admin request host. |
| Blog-index canonical | POST_INDEX always uses its generated path; each pagination page self-canonicalizes. Existing per-page canonical overrides are ignored for this kind. |
| Social image | Existing `featured_image_url_for(item=..., site=..., db=...)`, including rendition selection and site default. PROFILE pages prefer the author's avatar when present. |
| Indexing | Existing `item.noindex`; pass it through for every supported page kind, including POST_INDEX. |

Use `profile_view` on the actual content author's User for PROFILE metadata; pass the already-resolved profile from delivery where available. A deleted selected image does not silently invent a site-image fallback that the current image resolver does not use. Missing site canonical URL means no generated absolute canonical and no locally resolved social image, while an explicit valid canonical override or profile avatar may still exist.

`post/plugin.py::_render_post`, every branch of `page/plugin.py::_render_page`, and `page/delivery.py::render_post_index_page` consume this resolver. Preserve the existing template context names (`meta_description`, `canonical_url`, `noindex`, `og_image_url`) for themes. Pass `metadata` for bundled title/social blocks, replacing their repeated title-fallback expressions. Keep unrelated JSON-LD and content rendering unchanged. Blog-index delivery currently omits `noindex`; supplying the same resolved value makes its newly exposed control effective.

### Prospective URLs and working copies

- Posts use `post_url_for(site, submitted_slug, published_at=live.published_at, db=db)`. With no published blog index, show that no public post URL is available. Never manufacture one.
- A never-published post on dated permalinks has the helper's current flat fallback. Explain that first publication supplies the date segments; do not assume the scheduled time will be the actual publication time.
- Pages use `page_path_preview(db, site=site, parent_id=validated_parent, slug=submitted_slug, page_id=live_page_id)`. This retains nested paths and home-page `/` behavior. An empty slug shows an incomplete-address explanation instead of an invented URL.
- For working copies, take title/body/metadata/image/slug/parent/kind from the copy or submitted form, but identity, original author and publication timestamp from its live Post/Page. The card describes metadata after promotion. Its proposed slug/parent may differ from the existing signed theme preview, which deliberately renders at the live URL. Explain that distinction near the existing working-copy Preview link without changing that preview service.
- POST_INDEX metadata previews represent page 1. Keep generated canonical URLs and explain that later pages use their own addresses. Do not display an editable canonical override for this kind. Preserve any stored override for a later kind change; ignore it while kind is POST_INDEX.

### Controls and validation

Add shared `admin/_metadata_fields.html` for `meta_title`, `meta_description`, `canonical_url`, `noindex` and hidden `_metadata_fields=1`. Explanations: blank text uses the displayed fallback; canonical chooses the preferred indexing address without redirecting readers; noindex asks search engines not to list the page and does not make it private. Keep the existing featured-image picker and explain PROFILE avatar precedence.

Text keys enter the parsed form only when actually submitted. The hidden marker distinguishes an unchecked noindex checkbox from an old client omitting the whole section: marker plus absent checkbox means false; neither marker nor key preserves the baseline. Explicit empty text becomes `None` on persistence. Include metadata values in live GET and working-copy GET form dictionaries, create constructors, and the existing `_apply_*_form_fields` seams. Existing `_EDITABLE_*_FIELDS` already carries metadata through fork, restage and promotion; do not add a parallel promotion path.

Use one `validate_metadata_form(form: Mapping[str, str]) -> str | None` helper in core/seo.py before any assignment. For a nonempty submitted canonical, reuse `safe_external_url`, catch `ValueError` for malformed URL syntax, and report a form error. Enforce existing 255-character model bounds on meta title and canonical URL. POST_INDEX retains its automatic canonical behavior instead of validating or applying a forged override field. Existing featured-image and parent validators reject wrong-site IDs in the preview as on save. Preserve bad submitted values and the original token on errors; suppress misleading preview cards until those inputs are valid.

Metadata remains escaped plain text. Preview addresses can be text rather than links. Gate image `src` through `safe_external_url` and handle malformed legacy addresses without a 500 or executable URL. Use heading-labelled sections, associated labels/help, descriptive image alternative text, and visible **Illustrative preview** wording explaining that search engines and sharing services may choose another presentation. Do not use `|safe` on metadata or promise rankings.

### Task 1: Establish one delivery/admin resolver

**Files:** `src/bragi/core/seo.py`, `src/bragi/contrib/post/plugin.py`, `src/bragi/contrib/page/plugin.py`, `src/bragi/contrib/page/delivery.py`; `src/bragi/contrib/post/templates/delivery/post.html`, `src/bragi/contrib/page/templates/delivery/page.html`, `src/bragi/contrib/page/templates/delivery/resume.html`, `src/bragi/contrib/page/templates/delivery/profile.html`, `src/bragi/contrib/page/templates/delivery/post_index.html`; `tests/contrib/test_og_meta.py`, `tests/contrib/test_profile_page.py`.
**Interfaces:** `MetadataItem`, `EffectiveMetadata`, `effective_metadata` as defined above; existing image resolver and theme context names remain available.

- [x] Add `test_effective_metadata_matches_public_head`, parsing actual public HTML to compare document title, description, canonical, OG/Twitter title/description/image and robots output. Include explicit and empty overrides, site defaults, a completed social rendition, no image, no site canonical, special characters, PROFILE bio/avatar, home/nested pages and POST_INDEX page 1/page 2 canonical addresses.
- [x] Add a nonempty noindex POST_INDEX case, assert its persisted precondition, and run it against baseline to observe the missing robots tag. Existing parity cases characterize behavior before extraction rather than being falsely reported as new failures.
- [x] Extract and integrate the resolver. Compute image/profile values with the supplied read session and preserve custom-theme context names. Pass resolved noindex to blog-index delivery; preserve paginated self-canonical behavior.
- [x] Run `uv run pytest tests/contrib/test_og_meta.py tests/contrib/test_profile_page.py tests/contrib/test_post_delivery.py tests/contrib/test_page_delivery.py`. Expected: all pass.

### Task 2: Metadata controls and persistence

**Files:** `src/bragi/core/seo.py`, `src/bragi/contrib/post/admin.py`, `src/bragi/contrib/page/admin.py`, new `src/bragi/templates/admin/_metadata_fields.html`, `src/bragi/contrib/post/templates/admin/edit.html`, `src/bragi/contrib/page/templates/admin/page_edit.html`, new `tests/contrib/test_metadata_editor.py`.
**Interfaces:** `validate_metadata_form`; existing request/form/application helpers extended without changing their caller contract. Form marker `_metadata_fields` has the compatibility semantics above.

- [x] Add `test_metadata_round_trips_through_editor`, parameterized for post/page create, edit, stage, working-copy save and promotion. Seed nondefault values, save changed values, inspect GET controls and public head, explicitly clear fields, and verify omitted metadata remains unchanged. Assert working-copy saves leave live values unchanged until promotion.
- [x] Add `test_invalid_metadata_preserves_submission` for unsafe schemes, protocol-relative URLs, malformed IPv6, embedded control/bidi characters and overlong bounded fields. Assert no content/revision mutation and retained fields/token on error. Add stale-token preservation with submitted metadata.
- [x] Run `uv run pytest tests/contrib/test_metadata_editor.py`. Expected baseline failures: missing controls and ignored submitted metadata.
- [x] Implement shared controls/validation and extend create/apply/GET form paths. Update touched staging comments: metadata is now editable; subtitle remains absent from the post form. Preserve intentional legacy omission behavior.
- [x] Run `uv run pytest tests/contrib/test_metadata_editor.py tests/contrib/test_post_working_copy.py tests/contrib/test_page_working_copy.py`. Expected: all pass.

### Task 3: Saved and unsaved preview interaction

**Files:** both admin modules and editor templates from Task 2, new `src/bragi/templates/admin/_metadata_preview.html`, `tests/contrib/test_metadata_editor.py`, new `tests/integration/test_metadata_preview.py`, `tests/browser/check_editor_recovery.py`.
**Interfaces:** Existing six editor endpoints recognize `_metadata_preview=1`; existing `_render_post_form` and `_render_page_form` provide `EffectiveMetadata` plus preview state/error to the shared partial. No new routes.

- [x] Add `test_metadata_preview_does_not_save`, parameterized for post/page new, existing published and working-copy forms. Submit unsaved body, metadata, image and path fields with `_metadata_preview=1`. Assert their effective values appear, all content/working-copy/revision/redirect state remains unchanged, hooks and editor-save receipt do not fire, and `_edit_token` stays byte-for-byte identical. Include a stale token followed by a rejected normal save.
- [x] Add `test_working_copy_metadata_uses_live_identity`: use deliberately different live/copy IDs and a different editing user, with a staged slug/parent. Assert proposed URL, home-page handling and original author's PROFILE bio/avatar; compare after promotion with actual public metadata.
- [x] Add `test_preview_refuses_cross_site_inputs` and cases for incomplete new forms, absent post index, dated drafts, missing canonical configuration and POST_INDEX generated URLs. Assert readable explanations rather than fabricated output.
- [x] With migrated file-backed SQLite, probe an independent writer during excerpt generation and Jinja rendering. Assert it succeeds and the preview branch never starts `BEGIN IMMEDIATE`. Require normal CSRF and existing author-own/editor-all permissions.
- [x] Run `uv run pytest tests/contrib/test_metadata_editor.py tests/integration/test_metadata_preview.py` against unfixed handlers. Expected: a named preview action follows today's normal save path or lacks the preview; observe the relevant failure before adding the early branches.
- [x] Add the early preview branches and build transient metadata inputs. Render shared accessible cards with visible fallback sources and explicit saved/unsaved state. Keep the primary Save button first and preserve submitted form content.
- [x] Extend the existing browser recovery check to include metadata controls and an Update previews submission with no save receipt. Verify last-keystroke Markdown, normal named-button submission, keyboard use, metadata recovery and retention of the recovery copy. In a JavaScript-disabled browser context, assert the native button submits `_metadata_preview=1` and the entered fields; pair this with the actual-route no-mutation checks above. The existing tiny browser host proves browser interaction, not production persistence. No JS metadata calculator or new browser dependency is introduced.
- [x] Run the focused tests plus `uv run --with playwright python tests/browser/check_editor_recovery.py` using the existing browser setup. Record any environment limitation explicitly rather than claiming a browser result.

### Task 4: Documentation and final verification

**Files:** `docs/content.md`, `CHANGELOG.md`, this plan; inspect `README.md` for invalidated claims.

- [x] Document Update previews versus Save, the displayed fallback rules, canonical/noindex meaning, PROFILE sources, automatic blog-index canonical URLs and working-copy prospective URLs. Add an Unreleased entry, including the newly honored blog-index noindex control.
- [x] Run `uv run ruff check src/ tests/ alembic/`.
- [x] Run `uv run ruff format --check src/ tests/ alembic/`.
- [x] Run `uv run mypy src/`.
- [x] Run `uv run pytest`.
- [x] Review the topic diff for secrets, accidental publication, token refresh, content loss, unsafe URL rendering and unrelated changes. Record exact validation results below; leave all changes uncommitted.

## Progress and rulings

- Read-only source pass completed. The interaction and fallback exceptions above replace the earlier provisional preview action.
- #522 requires exposing existing metadata fields because the current custom forms omit them. No model or migration change is needed; working-copy promotion already copies these fields.
- Public PROFILE bio/avatar precedence and POST_INDEX pagination canonical rules were confirmed in the current renderer. The preview must preserve them, not assume every page behaves like a static page.
- POST_INDEX currently drops noindex from rendering. Task 1 fixes that concrete gap so the new control has the promised effect.
- Implemented tasks 1 through 3. The existing `core/seo.py` now supplies the single resolver; native form previews run before writer locks and preserve the original token. No route, dependency or schema was added.
- Shared helpers for parsing, applying and rendering existing metadata fields avoid repeated omission/clear rules across create, edit, stage and working-copy save. The post render helper keeps its established `site_id` signature; resolving the Site once locally is smaller than changing every caller.
- Both staging paths now explain that explicit restaging rebases current live fields before applying submitted fields. Promotion still uses the existing editable-field lists.
- Regression evidence: blog-index noindex was absent before the resolver integration; 22 initial editor tests failed on missing controls, ignored invalid metadata or preview submissions saving content. Reviewer normalization finding reproduced with two failing forged-canonical cases before the one-line normalized-kind correction.
- Owned focused command: `uv run pytest tests/contrib/test_metadata_editor.py tests/contrib/test_og_meta.py tests/contrib/test_profile_page.py tests/contrib/test_post_delivery.py tests/contrib/test_page_delivery.py tests/contrib/test_post_working_copy.py tests/contrib/test_page_working_copy.py -q --tb=short --show-capture=no`: 130 passed in 13.05s. Includes 35 public-head parity cases plus pagination, all form persistence paths, legacy omission/explicit clearing, stale-token retention, invalid and cross-site inputs, unsaved excerpt/image values, and unsafe legacy avatars.
- Mutation evidence: temporarily returning raw URLs from `metadata_image_src` made both unsafe legacy-avatar cases fail; the source was restored in a `finally` block and both cases passed afterward.
- Owned Ruff checks pass. `uv run mypy src/` passes for 332 source files. Full branch gates remain with the root agent.
- Root owns `tests/integration/test_metadata_preview.py` and the browser-check extension. Its first migrated run passed both original-author/live-identity promotion journeys, independent writer probes, persistence snapshots, hook/receipt checks, CSRF and permissions. A nested working-copy page path case is being added. The earlier in-memory dated-post promotion assertion did not establish a production defect; the real migrated journey passes.
- Root reports the browser check passed for normal and boosted keyboard preview, required-field bypass, latest TipTap Markdown, metadata/noindex recovery retention without a receipt, implicit Enter retaining Save, and JavaScript-disabled native submission. The small browser host tests interaction; migrated route tests establish persistence behavior.

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

Root verification: `uv run pytest tests/integration/test_metadata_preview.py -q --tb=short --show-capture=no` passed 21 cases in 7.37s. Every preview endpoint permits independent writers at actual excerpt generation and Jinja rendering, issues no `BEGIN IMMEDIATE`, calls no lifecycle/audit/save-receipt hook, and leaves persisted content, working copies, revisions, redirects and audit rows unchanged. Authorization checks cover CSRF, foreign-site content and author/editor permissions. Working-copy journeys compare promoted public metadata with the proposed dated post, home-profile and nested-profile addresses using the original author.

The normalized-kind review finding was reproduced by both valid and unsafe forged canonical values before the one-line fix. Both now pass. Unsafe legacy-image guard mutation failed both new checks before restoration. The shared resolver/route suite passed 130 focused cases; the full gate above includes all added coverage.
