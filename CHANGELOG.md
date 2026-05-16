# Changelog

All notable changes to bragi are documented here. Format adapted
from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **TipTap rich-text editor on the page admin form.** Pages
  shipped 1.0.0 with only a plain textarea for the body; only
  posts got the full TipTap toolbar + image picker. The editor
  was extracted into a shared partial at
  `bragi/templates/admin/_tiptap_editor.html` (lives in core,
  not contrib, so both plugins can include it without crossing
  the contrib boundary), and the page edit template now
  `{% include %}`s it next to the same `name="body_markdown"`
  textarea fallback. Backend save path is unchanged; the
  textarea remains the canonical form input. Mount and toolbar
  DOM IDs were renamed `post-editor*` -> `tiptap-editor*` since
  they're shared now.

### Fixed
- **Editor toolbar stays visible while scrolling.** The TipTap
  toolbar above the body editor used to scroll off the top of
  the viewport on long bodies, so the operator had to scroll
  back to apply formatting. `position: sticky; top: 0` now pins
  it to the viewport once the page scrolls past it. Lives in
  the shared partial, so the fix applies to both the post and
  page edit forms.

## [1.7.0] - 2026-05-16

Admin post-list reordering. After the 1.6.1 Ghost-importer fix
landed, the next thing the operator hit was that the admin post
list looked like it was sorted alphabetically: every imported
row got `created_at = now()` clustered in microseconds, in the
order Ghost iterated its export, so a 60-post import surfaced
in Ghost's internal id order rather than anything meaningful
to the operator. The same effect would hit Hugo and WordPress
imports for the same reason.

Switch the admin list's `order_by` to `COALESCE(published_at,
updated_at) DESC` with `id DESC` as the tie-break. Published
posts now sort by publication date; drafts fall back to their
last edit; imported posts surface by the publish date the
importer preserved. Native authoring is also better off:
editing an old draft bubbles it back up the list.

No schema, hookspec, or URL change; the order_by clause on one
admin query is the entire diff. Pages have the same pattern at
`bragi/contrib/page/admin.py:135` but were left untouched on
the read that pages are structural, not chronological;
revisitable if operator feedback says otherwise.

### Changed
- **Admin post list sorts by recency, not creation time
  (#99).** `/admin/sites/<slug>/posts/` orders by
  `COALESCE(published_at, updated_at) DESC` with `Post.id DESC`
  as the tie-break. Replaces the previous `created_at DESC`
  order, which leaked import iteration into the admin list and
  didn't bubble a freshly edited old draft to the top either.
  Regression test seeds four posts whose `created_at` order is
  in direct conflict with publish-recency and asserts the
  COALESCE key wins.

## [1.6.1] - 2026-05-16

Bug-fix release for the Ghost importer's export-detection step.
On Ghost 6.x exports the importer was rejecting otherwise valid
files: a user's 6.27.0 export reproduced this on the first try.
The cause was a 4 KB head-scan heuristic in `looks_like_ghost`
that grepped the file's first 4 KB for `"posts"`. Modern Ghost
exports lead `db[0].data` with sizable `benefits` and
`custom_theme_settings` arrays, so `"posts"` lands past the
cutoff and the heuristic returned False.

Detection now parses the file in full via `load_export` and
returns False only on `OSError` / `ValueError` (which covers
malformed JSON, unreadable files, and the missing-envelope shape
checks already enforced in `load_export`). Files are at most
low-MB; the optimisation wasn't worth the false negatives.

No schema, interface, or behaviour changes outside the detection
path. The `cms import ghost ...` CLI shape is unchanged.

### Fixed
- **Ghost importer detects modern Ghost exports again (#95).**
  Replaces the 4 KB head-scan in
  `bragi.contrib.import_ghost.loader.looks_like_ghost` with a
  full `load_export` parse. Regression test seeds an export with
  a fat `custom_theme_settings` block before `posts` and asserts
  the `"posts"` key lands past 4 KB, so the test fails loud if a
  byte-window heuristic is ever reintroduced.

## [1.6.0] - 2026-05-16

Production deploy posture cleanup. Up to and including 1.5.1, the
container images shipped Werkzeug's dev server as their `CMD`;
gunicorn wasn't even in `pyproject.toml`. In production behind
Caddy that meant single-process, no worker pool, no graceful
reload, and Werkzeug's own "do not use in production" line in
every startup log. 1.6.0 swaps the entrypoint to gunicorn against
the WSGI factory.

No schema, no interface, no public-URL changes. The
v1.5.1-vs-v1.6.0 image diff is operational only: process model,
worker count, access-log format, restart semantics. Operators
parsing container logs will see gunicorn's combined-log lines
where Werkzeug's request lines used to be.

### Changed
- **Production containers run gunicorn, not Werkzeug's dev
  server (#91).** Both Dockerfile CMDs now invoke
  `gunicorn ... 'bragi.apps.X:create_X_app()'` with the `sync`
  worker class, `--timeout 30`, and `--access-logfile -` for
  stdout-friendly combined-log access lines. The
  `bragi-admin` / `bragi-delivery` click scripts stay the
  local-dev entrypoints (`make dev`); they call `app.run(...)`
  for Werkzeug's auto-reload. Workers default to 2 on admin
  (low concurrency) and 4 on delivery (read-heavy, parallel
  reads under SQLite WAL); both env-tunable via
  `ADMIN_WORKERS` / `DELIVERY_WORKERS`. Removes Werkzeug's
  "do not use in production" startup warning that previously
  shipped in every running image. README and compose example
  document the new env vars; the BRAGI_TAG example pins
  v1.6.0.

## [1.5.1] - 2026-05-16

A pure docs release: brings `README.md` up to date for 1.5.0.
No code, schema, behaviour, or configuration changes. The
v1.5.0 git tag had the pre-refresh 1.3.0-era README attached;
this release replaces it with the current snapshot. Pinning to
`v1.5.0` versus `v1.5.1` makes no operational difference;
images are identical except for the README baked into the
build context.

Also adds a "verify README is a current snapshot" step to the
project's release checklist (in the gitignored CLAUDE.md) so
the same drift doesn't recur silently.

### Changed
- **README refreshed for 1.5.0 (#88).** Status header bumped
  from 1.3.0 / 2026-05-14 to 1.5.0 / 2026-05-15; feature list
  picks up themes (1.4.0, #40), SQLite FTS5 search (1.4.0, #43),
  and the IA refactor cluster (1.5.0, #77 / #78 / #79 / #80).
  "What bragi is" gains a "Sites are first-class workspaces"
  bullet. Importers section promotes WordPress (#39) out of
  "deferred". Compose example bumped to `BRAGI_TAG=v1.5.0`.
  Project layout adds `search/`, `themes/`, `team/`, and
  `import_wordpress/` to the contrib tree. Status header
  re-bumped to 1.5.1 / 2026-05-16 as part of this release.

## [1.5.0] - 2026-05-15

A four-phase information-architecture refactor that makes sites
first-class workspaces: every admin content URL now lives under
`/admin/sites/<slug>/...`, the picker turns into a "pick where
to work" surface, analytics scope to the site you've entered,
and owners get a UI to invite collaborators. Public delivery
URLs are unchanged; plugin hookspecs are unchanged. The admin
URL shape changed, which is the only operator-visible break.

Shipped as four sequential PRs (#77, #78, #79, #80) plus a
post-rollout UX polish (#85) discovered while using the new
shape live.

### Added
- **Site ownership as a first-class fact (P1 / #77).** Each
  site has a NOT NULL `owner_user_id` FK on `sites`. Owners are
  implicit admins on their own site:
  `has_role(user, site, "admin")` returns true for them whether
  or not an explicit `UserSiteRole` exists. New permission
  helpers `is_site_member(user, site)` and
  `accessible_sites_for(user)` underpin the rest of the
  refactor. `cms site create` gained `--owner` (defaults to the
  first superuser); new `cms site transfer --site --to`
  command reassigns ownership and writes a
  `site.owner.transferred` audit row.
- **Per-site admin dashboard at `/admin/sites/<slug>/` (P2 /
  #78).** Lands on a short welcome plus a sections grid pulled
  from the active user's site-scoped NavItems, so the dashboard
  self-updates when new site-scoped plugins register. The
  picker at `/admin/sites/` becomes the "pick where to work"
  page; non-superusers with exactly one accessible site are
  redirected straight into it, superusers always see the
  picker.
- **`resolve_site_or_abort(db, site_slug) -> Site`.** Shared
  helper in `bragi.core.permissions` used by every site-scoped
  blueprint to do the slug-to-Site lookup, the member gate
  (404 on unknown slug, 403 on non-member), and the
  `g.current_site` / `g.site_slug` stash in one call. The
  resolved row is expunged from the session so the chrome can
  safely read it post-render.
- **Team management UI (P4 / #80).** New
  `bragi.contrib.team` plugin mounts at
  `/admin/sites/<slug>/team/` with list / grant / revoke. Only
  the site owner (or a superuser) sees the page or can mutate
  the team; collaborators with the `admin` role get 403. Grant
  writes `team.granted` (or `team.role_changed` when the user
  already had a role on the site); revoke writes
  `team.revoked`. The owner row is unrevokable from the UI; the
  ownership-transfer path stays on `cms site transfer`.
- **`NavItem.scope` and `NavItem.permission="site_owner"`.**
  `scope: "global" | "site"` (default "global") splits the
  chrome's nav into global items (Sites, Sessions, Audit,
  Account) and site items (Posts, Pages, Redirects,
  Attachments, Analytics, Team). The new `site_owner`
  permission value is recognised by the chrome's visibility
  predicate; superusers continue to pass every gate.

### Changed
- **Admin content routes are now site-prefixed (P2 / #78).**
  Posts, pages, redirects, and attachments moved from global
  `/admin/X/` URLs to `/admin/sites/<site_slug>/X/`. Every
  site-scoped view resolves the slug to a Site, gates on
  `is_site_member`, and refuses cross-site id probes with a 404
  (not 403) so an owner on site A cannot enumerate site B's id
  space by watching the response code. The old global URLs
  hard-404; admin URLs are not a public contract so no redirect
  bridge ships. An app-level `url_defaults` hook injects
  `site_slug` from `g` into outgoing `url_for(...)` calls, so
  templates and view-side links stay free of plumbing. The
  redirects new/edit form lost its now-redundant site picker;
  the attachments list / picker lost theirs too.
- **`/admin/sites` is now member-readable (P1 / #77).** The
  picker no longer requires superuser. Any signed-in user sees
  the sites they own or hold a role on; superusers see all.
  Write actions on the picker (Deactivate, Activate, New site)
  remain superuser-only and self-gate via a blueprint hook plus
  template conditional, replacing the blanket `_superuser_only`
  guard.
- **Picker rows Enter the site (#85).** Clicking a slug in
  `/admin/sites/` takes you to the per-site dashboard,
  matching the action both collaborators and superusers
  actually want from the picker. Site settings (hostname,
  title, theme, aliases) moved to a superuser-only "Settings"
  link next to the title on the dashboard; the underlying
  `/admin/sites/<int:site_id>/edit` URL and endpoint are
  unchanged. Deactivate / Activate stays on the picker since
  deactivation is a cross-site operator decision.
- **Analytics is now per-site (P3 / #79).** The dashboard moved
  from the cross-site `/admin/analytics/` to
  `/admin/sites/<slug>/analytics/`; queries hard-filter on
  `AnalyticsEvent.site_id == site.id`. Cross-site aggregation
  is no longer surfaced anywhere. Permission shifted from
  superuser-only to any site member, so owners and
  collaborators read their own rollups without elevation. The
  Analytics NavItem gained `scope="site"` and dropped its
  `permission="superuser"` gate. The writer is untouched;
  this is a read-path change only. The old `/admin/analytics/`
  hard-404s.

### Migration
- **`add_site_owner`** (`7fd0ed6fe2df`). Three-pass schema
  change: add `owner_user_id` as nullable, backfill (existing
  site admin then first superuser then fail loudly if no
  candidate exists), then promote to NOT NULL via
  `batch_alter_table` so SQLite is happy. Downgrade drops the
  column. Operators with no superuser and no admin-role rows
  must seed at least one superuser before `alembic upgrade
  head` will complete.

## [1.4.1] - 2026-05-15

A pure-bugfix release sweeping eight defects surfaced by a full
audit of the test suite. No schema changes, no contract changes;
all fixes target correctness, observability, and security gaps
that were silent on the happy path but real under their failure
modes.

### Fixed
- **Page admin fires `on_post_published` / `on_post_updated`
  (#57).** Page admin was firing only `on_post_deleted`. Search
  indexing, slug-change auto-301, IndexNow ping, and cache-purge
  subscribers all saw page create/edit as no-op events. Pages
  were surfacing in search only because `cms search reindex`
  walked them after the fact. Page admin now mirrors the post
  admin's lifecycle wiring; the page-edit form gains a
  `skip_redirect` checkbox so typo-fixes in drafts don't insert
  stale 301s.
- **`site_resolver` honours `active=False` (#58).** The admin
  "Deactivate" toggle updated the flag but the resolver ignored
  it; deactivated sites kept serving. The canonical-hostname and
  alias-fallback queries now filter on `Site.active`, so the
  toggle actually takes a site off the air.
- **`on_user_login` fires for local password auth (#59).** GitHub
  OAuth fired the hook; the local password flow didn't.
  Observability subscribers (analytics, audit enrichment, future
  plugins) were blind to half the auth surface. Failed logins
  still do not fire the hook (the contract is "successful auth").
- **Redirect chain follow + loop detection (#60).** The middleware
  resolved a single redirect and stopped, leaving multi-hop
  chains as browser-visible double-hops and turning data-layer
  cycles into infinite browser-side chains. Now follows up to 3
  hops, collapses the chain into a single user-visible redirect
  (status code from the first hop, target from the last), detects
  loops (direct and indirect) and serves 500 with a log line, and
  short-circuits on 410 anywhere in the chain.
- **IndexNow no longer pings on draft saves (#61).** The
  `on_post_updated` hookimpl fired unconditionally on every
  save, including draft-to-draft edits. The plugin POSTed to the
  IndexNow endpoint with URLs that 404 publicly, wasting per-host
  quota and training search engines to downweight the site. Now
  filters: draft→draft is silent, draft→published / published
  edits / published→draft / delete all ping (the unpublish case
  is intentional so the engine learns the URL is now 404).
- **Sitemap walks all registered content types (#62).** The
  sitemap was Post-only; published Pages were silently excluded.
  Now iterates `registry.content_types` filtered by
  `sitemap_eligible` and emits a `<url>` per published row of
  each spec. Future-proof for any third-party content-type plugin
  that registers with the same shape.
- **Session id rotated on login (#63).** Both auth paths set
  `user_id` on the session without rotating the underlying sid.
  Any pre-auth sid an attacker may have planted on the victim's
  browser was inherited intact through authentication: textbook
  session fixation. `BragiServerSession.regenerate()` rotates
  the sid while preserving dict contents; `rotate_sid()` helper
  is called by both `auth_local` and `auth_github`. Failed
  logins do not rotate.
- **Sites admin gated behind superuser flag (#64).** The
  /admin/sites endpoints had only the global "logged in" guard;
  any author or editor on one site could edit, deactivate, or
  alias-swap any other site. Now refuses non-superuser hits with
  403, and the Sites nav entry is hidden from non-superusers to
  match. Conservative default for solo-operator bragi; a future
  multi-admin scenario can replace the gate with per-site role
  checks.

## [1.4.0] - 2026-05-15

### Added
- File-based theme registry (#40). New `bragi.contrib.themes`
  ships the consumer surface (delivery blueprint serving theme
  static assets at `/theme/<slug>/static/<path>`, and a
  `cms theme list` CLI). The contract itself lives in core: a
  `ThemeSpec` dataclass in `bragi.api`, a `register_theme`
  hookspec, and a `ThemeAwareLoader` wrapping the delivery
  app's Jinja loader chain. When the active site's `theme`
  matches a registered ThemeSpec, the theme's loader is
  consulted before the plugin / default chain, so the theme can
  shadow any template name (Hugo-style override granularity).
  Sites with `theme=NULL` render with the default chain
  exactly as before; an orphaned slug (theme uninstalled while
  the site still references it) falls back without 500'ing.
  Admin: the site edit form gains a Theme dropdown listing
  discovered themes; unknown slugs are rejected with a friendly
  error rather than persisted. v1 ships the contract only with
  no in-tree theme; a `bragi-theme-foo` package slots in via
  the `bragi.plugins` entry-point group like every other
  plugin. Database-stored templates remain rejected (CONTEXT.md
  "Deferred surfaces"). Schema: `sites.theme` nullable string
  (migration `add_site_theme`, rev `2a429b18c1d8`).

- SQLite FTS5 search backend (#43). New `bragi.contrib.search`
  ships the day-one default backend (`name="sqlite-fts5"`),
  registered via the `register_search_backend` hookspec that
  CONTEXT.md reserved. Two FTS5 virtual tables (`posts_fts`,
  `pages_fts`) hold per-row inverted indexes over `title`, `body`,
  `meta_description`, `excerpt` with `porter unicode61`
  tokenisation; the body is fed through `strip_code_fences` first
  so language hints in fenced blocks don't pollute the index.
  Lifecycle is wired through `on_post_published`,
  `on_post_updated`, and `on_post_deleted` (which page admin reuses
  for pages, per the existing convention): the index follows
  publish/unpublish/delete events. `GET /search?q=<query>&page=N`
  renders results full-page on cold load and a partial on htmx
  swaps, with `<mark>`-decorated `snippet()` excerpts and
  bm25-sorted ranking; the route is mounted on the delivery app
  with the standard `default-html` cache policy. CLI `cms search
  reindex [--site <slug>] [--dry-run]` rebuilds the index from
  scratch (drop-and-rebuild is fine at personal-blog scale).
  `Registry.search_backend()` resolves the active backend with a
  priority rule that lets a third-party backend override
  `sqlite-fts5` by registering with any other `name`.
  Migration `add_search_fts` (rev `1eff692ffe2b`) creates the
  FTS5 tables; reversible.

## [1.3.0] - 2026-05-14

### Added
- WordPress (WXR) importer (#39). New
  `bragi.contrib.import_wordpress` reads a WP export, lands posts
  and pages with HTML bodies converted to markdown via
  `markdownify`, and preserves source URLs as 301 redirect rows
  (Source `import:wordpress`). Tags and categories collapse into
  `Tag`; categories get a `category:` slug prefix so they survive
  round-trips. Shortcodes are stripped with a one-line warning
  per unique shortcode name; comments and attachments are
  counted and warned (out of scope for v1). Idempotent re-import
  via `(site_id, source_id)`. CLI: `cms import wordpress --site
  <slug> [--author <email>] [--dry-run] <wxr.xml>`.
  Schema: `pages.source_id` and `pages.source_meta` added (parity
  with Post; migration `add_page_source_id`, rev
  `17c7f26e8fde`) so page idempotency works the same way.
- TipTap image picker + responsive image rendering (#41 Phase 4,
  closing the issue). New endpoint `GET /admin/attachments/picker`
  returns an htmx-loaded grid of image attachments with a per-site
  filter and pagination; each card carries the storage_key, alt
  text, and filename in data attributes. The post edit page gains
  an "Image" toolbar button that opens a native `<dialog>` hosting
  the picker; clicking a card inserts a markdown image link
  (`![alt](/attachments/<key>)`) at the cursor and closes the
  dialog. A new HTML transform `pictureify` runs at delivery time:
  it walks rendered post / page HTML, finds `<img>` tags pointing
  at `/attachments/<key>`, and rewrites them into a `<picture>`
  block with a `<source srcset>` that includes the full rendition
  ladder plus the original. The transform adds `width`, `height`,
  and `loading="lazy"` from the Attachment row (markdown can't
  express them) but preserves the author's `alt` verbatim
  (`alt=""` stays empty for decorative images per WCAG).
- Bulk alt-text editing + reindex CLI (#41 Phase 3). The
  attachments admin list view gains a `?missing_alt=1` filter
  that lists image rows lacking alt text, with an inline
  htmx-driven save form per row so an operator can fill in
  many at once without leaving the page. The header surfaces a
  count badge linking to the filtered view. A new endpoint
  `POST /admin/attachments/<id>/alt-text` saves a single row
  (htmx returns the row partial; non-htmx redirects). New CLI:
  `flask cms media reindex [--site SLUG] [--dry-run]` walks
  image attachments and fills in any rendition slots missing
  from the current ladder (purely additive; existing slots
  untouched). The list view partial is now extracted to
  `admin/_attachment_row.html` so htmx and full-page renders
  share markup.
- Image rendition ladder (#41 Phase 2). Uploads now generate one
  `AttachmentRendition` per configured target width (default
  `[320, 800, 1600]`, override via
  `BRAGI_ATTACHMENT_RENDITION_WIDTHS`). Widths at or above the
  source skip (no upscale); the ladder runs synchronously on
  upload because the day-one workload is small and a job queue
  would be premature. `ImageProcessorSpec` gains an optional
  `resize(data, target_width) -> bytes | None`; the default
  Pillow processor implements it with aspect-preserving thumbnail
  scaling (LANCZOS, quality 85 for JPEGs). New Jinja global
  `srcset_for(attachment)` emits a `<picture srcset>`-compatible
  value spanning each rendition plus the original. The delivery
  route at `/attachments/<key>` now also serves rendition bytes
  (matched against `AttachmentRendition.storage_key`, joined to
  the parent attachment for per-site isolation). Delete cascades
  the rendition rows and unlinks orphan storage keys across both
  tables. Migration `add_attachment_renditions`
  (rev `25c6f95918c4`).
- Media library foundation (#41 Phase 1). The `Attachment` model
  gains `width`, `height`, `alt_text`, `title`, `focal_x`,
  `focal_y` columns; image uploads now populate width / height
  automatically via Pillow. The `bragi.contrib.attachments` admin
  gains an edit view for alt text / title / focal point (focal
  coordinates are clamped to `[0.0, 1.0]`). Two reserved hooks go
  live: `register_storage_backend` (default: local-disk under
  `Settings.attachments_root`) and `register_image_processor`
  (default: Pillow). Storage access in the attachments admin and
  delivery routes now goes through `bragi.core.storage.resolve()`,
  which reads the active backend from the Registry and falls back
  to the local default. Pillow is a new runtime dependency.
  Migration `add_attachment_image_fields` (rev `44fe91537fd5`).
  Renditions, the richer media-library admin, and the TipTap embed
  picker land in later phases of #41.

## [1.2.0] - 2026-05-14

### Added
- IndexNow push-crawl plugin (#36). New `bragi.contrib.indexnow`
  fires on `on_post_published`, `on_post_updated`, and
  `on_post_deleted` (which covers pages too, since the page admin
  reuses the post lifecycle hooks). For each event, the plugin
  resolves the item's site, reads `extra_settings['indexnow_key']`,
  builds the public URL via the content-type registry's
  `url_for`, and POSTs to the configured endpoint
  (`BRAGI_INDEXNOW_ENDPOINT`, defaulting to
  `https://api.indexnow.org/indexnow`). HTTP errors are logged
  and swallowed so a missed ping never breaks a publish. The
  delivery app serves the verification key file at
  `GET /<key>.txt` (24h cache, scoped per Site). New CLI:
  `cms indexnow setup --site <slug> [--key <key>]` generates a
  32-hex-char key (or accepts an explicit one), validates it,
  writes it into the site's `extra_settings`, and prints the
  verification URL.

## [1.1.0] - 2026-05-14

### Added
- HTTP cache management on the delivery app (#33). All 2xx HTML
  responses now carry `Cache-Control: public, max-age=60,
  s-maxage=300` by default; post and page views additionally
  emit `ETag` + `Last-Modified` and honour `If-None-Match` /
  `If-Modified-Since` (returning a body-less 304 on a match).
  The static SEO routes get longer policies (feed / sitemap:
  10min shared; robots / security.txt: 24h). The admin app
  forces `Cache-Control: private, no-store` on every response.
  New hookspec `on_cache_purge(scope, key)` fires from post /
  page lifecycle commits so a future CDN-invalidation plugin
  has something to subscribe to; core ships no listener (it's a
  zero-cost pass-through until somebody wires it).
- Revision history for posts and pages (#32). New
  `PostRevision` and `PageRevision` tables capture the pre-edit
  state on every save (`title`, `slug`, `status`,
  `body_markdown`, `body_html`, `body_excerpt`,
  `meta_description`; pages additionally capture `parent_id`).
  New admin views: `GET /admin/posts/<id>/revisions` list,
  `GET /admin/posts/<id>/revisions/<rev_id>` side-by-side detail,
  `POST /admin/posts/<id>/revisions/<rev_id>/restore` (which
  itself writes a fresh revision of the now-current state so
  restores stay reversible). Mirror routes for pages. Migration
  `add_revisions` (revision `c9d608f87623`).

## [1.0.1] - 2026-05-14

### Fixed
- Docker image build: the `bragi-admin` / `bragi-delivery`
  Dockerfiles used `poetry export`, which Poetry 2.x dropped from
  the core CLI in favour of the `poetry-plugin-export` package.
  Both Dockerfiles now install Poetry pinned to `>=2.0,<3.0`
  alongside `poetry-plugin-export>=1.8` so the export step works
  again. v1.0.0 shipped the tag but the GHCR workflow failed to
  publish images; v1.0.1 is the first release with actual
  containers on GHCR.

## [1.0.0] - 2026-05-14

### Added
- Initial project scaffold: Poetry config, alembic setup, src/bragi/
  skeleton (apps, core, contrib), Makefile, Procfile.dev, Docker
  images for admin and delivery, CI workflow, smoke test, project
  README and CHANGELOG.
- Day-one plugin hook surface in `bragi/hookspecs.py`: 18 hookspecs
  across lifecycle, content types, markdown / HTML rendering,
  importers, auth, redirects, admin UI, content lifecycle, and
  analytics. `resolve_redirect` is `firstresult=True`; others
  collect all hookimpl results.
- Public plugin API in `bragi/api.py`: spec dataclasses
  (`ContentTypeSpec`, `FieldSpec`, `ImporterSpec`, `ImportPlan`,
  `ImportResult`, `OAuthProviderSpec`, `AuthMethodSpec`,
  `ExternalUser`, `NavItem`, `RedirectTarget`, `AnalyticsEvent`)
  plus the `hookimpl` marker.
- Runtime types: `bragi.core.registry.Registry` accumulates plugin
  contributions at boot; `bragi.core.render.transforms.TransformRegistry`
  is a priority-ordered transform pipeline for markdown text and
  rendered HTML.
- App factories now invoke the full hook flow at boot. Both apps
  expose `plugin_manager`, `registry`, `md_transforms`, and
  `html_transforms` on `app.extensions`.
- `tests/test_hookspecs.py` asserts the day-one hook surface is
  registered and that `resolve_redirect` is `firstresult`.
- Foundational SQLAlchemy models (`Site`, `User`, `Post`) with
  shared `IdMixin` / `TimestampsMixin`. `Base` lives in
  `bragi/core/models/_base.py`; individual models live in their
  own modules and re-export through `bragi.core.models`.
- Initial alembic migration creates `sites`, `users`, `posts`.
  Migration script template upgraded to modern type syntax
  (`X | Y`, `collections.abc.Sequence`). Post-write hook now
  invokes `ruff check --fix` and `ruff format` on generated
  revisions via the `exec` hook type.
- CI lint / format checks now cover `alembic/` too, plus a
  migration smoke run (`upgrade -> downgrade -> upgrade` on a
  fresh SQLite). Makefile `lint` / `fmt` targets include
  `alembic/`.
- First built-in plugin: `bragi.contrib.post`. Registers a
  `ContentTypeSpec` for Post via `register_content_type` with a
  stub `url_for` and `render`. Activated through the
  `[tool.poetry.plugins."bragi.plugins"]` entry-point group;
  visible in `app.extensions["registry"].content_types` on both
  admin and delivery apps. Real admin Blueprint and delivery
  templates land in follow-up commits.
- `Redirect` model (`bragi.core.models.redirect.Redirect`) with
  `MatchType` and `RedirectSource` string constants. Per-site
  unique constraint on `(site_id, source_path, match_type)`.
- alembic migration `add_redirects` (revision `36a0ec65ddbf`)
  adds the `redirects` table.
- `bragi.contrib.redirects` plugin: implements `resolve_redirect`
  (firstresult, exact-match table lookup filtered by
  `active=True`). Site-scoped via `site_id`; returns None when no
  site context is provided. Prefix and regex matching,
  hit-count tracking, admin Blueprint, and the slug-change
  auto-301 land in follow-up commits.
- Test DB fixtures (`db_engine`, `db_session_factory`,
  `db_session`) live in `tests/conftest.py`. In-memory SQLite,
  fresh per test, with all tables created via
  `Base.metadata.create_all`. The CI migration smoke step is the
  cross-check that schema and migrations agree.
- Site resolver middleware (`bragi.core.middleware.site_resolver`):
  before_request hook that resolves the Host header to a Site row
  via the DB and attaches it to `flask.g.site`. Installed on both
  admin and delivery apps.
- Redirect 404-fallback handler
  (`bragi.core.middleware.redirects`): errorhandler that calls
  `pm.hook.resolve_redirect` on every 404 with `g.site` and the
  requested path. Emits 301 / 302 / 307 / 308 on hit, 410 on a
  Gone-typed redirect, or a real 404 on miss. Installed on the
  delivery app only; admin URLs are statically defined.
- Integration test (`tests/integration/test_redirects_flow.py`)
  exercises the full pipeline: Host header resolution, 301 + 410
  emission, and 404 fallthrough for unknown hosts / paths.
- `LocalCredential` model (`bragi.core.models.local_credential`):
  argon2id password storage for the bootstrap auth path. One row
  per User (`user_id` as both PK and FK), with a `must_change`
  flag for forcing a rotation at next login.
- alembic migration `add_local_credentials` (revision
  `52631645e3c1`) adds the `local_credentials` table.
- `bragi.contrib.auth_local` plugin (third built-in): bootstrap
  authentication via email + argon2id password.
  - `register_admin_blueprint`: `/auth/login` (GET + POST),
    `/auth/logout` (POST).
  - `register_auth_method`: AuthMethodSpec with `bootstrap=True`.
  - `register_cli_command`: `flask --app bragi.apps.admin cms
    user create --email --display-name [--password] [--superuser]`.
    Generates a strong random password when `--password` is
    omitted; prints to stderr.
  - `on_app_init`: installs a `before_request` auth guard on the
    admin app that redirects anonymous hits to `/auth/login`,
    preserving the original path as `?next=`. Delivery is
    unaffected. Public endpoints whitelist: `auth_local.login`,
    `auth_local.logout`, `static`.
  - `_safe_next` restricts the post-login redirect target to
    relative paths so an attacker-controlled `next` cannot become
    an open-redirect off-site.
  - Sessions use Flask's signed cookies for v1. Server-side
    sessions (in the `sessions` table) are reserved for a
    follow-up when logout-invalidates-everywhere matters.
- Post admin Blueprint (`bragi.contrib.post.admin`): list / new /
  edit / delete views under `/admin/posts`. Plain HTML forms with
  textarea-based markdown editing (TipTap lands later). Title and
  slug are required; status transitions to `published` set
  `published_at` the first time. Delete shows a JS confirm.
  Registered via `register_admin_blueprint` and
  `register_admin_nav` on the post plugin.
- Markdown rendering pipeline stub
  (`bragi.core.render.markdown`): `markdown-it-py` configured
  with CommonMark + linkify + tables. Used by the post admin to
  populate `body_html` on save. Plugin-contributed transforms
  (Pygments code highlighting, anchor IDs, etc.) land here in a
  follow-up.
- Shared admin chrome at `bragi/templates/admin/base.html`:
  topbar with plugin-contributed nav items, current-user display,
  flash messages, and a styled form area. The admin app's Jinja
  loader chains `bragi.templates` so plugin templates can
  `{% extends "admin/base.html" %}`.
- `bragi.contrib.sites` plugin: contributes `cms site create
  --slug --hostname --title [--locale] [--timezone]
  [--canonical-url]` and `cms site list`. Slug and hostname are
  lower-cased to match the site_resolver's Host lookup.
- Site admin Blueprint at `/admin/sites`: list / new / edit views
  plus deactivate / activate POST endpoints. Deactivating sets
  `active=False` so requests to that hostname stop resolving
  without losing config; hard delete is intentionally not exposed
  (would cascade into orphaned content). Hostname and slug edits
  guard against UNIQUE violations with friendly errors. Admin nav
  entry under section `site`.
- Delivery render view for Posts. New `register_delivery_blueprint`
  hookspec lets content-type plugins own their public URL space.
  The post plugin's delivery Blueprint mounts `/posts/<slug>/`
  (slash and no-slash forms), filters by `(site_id, slug,
  status=published)`, and renders through
  `ContentTypeSpec.render`. Drafts, scheduled, and archived posts
  are not served publicly. Includes a shared
  `delivery/base.html` and a `delivery/post.html` with canonical
  URL, meta description (falling back to the body excerpt), and
  conditional `<meta name="robots" content="noindex">`.
- CSRF protection on the admin app
  (`bragi.core.middleware.csrf`). Hand-rolled, no Flask-WTF
  dependency: a `before_request` hook ensures `session["_csrf_token"]`,
  validates the `_csrf_token` form field or `X-CSRF-Token`
  header on POST / PUT / PATCH / DELETE in constant time, aborts
  400 on mismatch, and exposes `csrf_token()` as a Jinja global.
  Endpoints can opt out via `app.config["CSRF_EXEMPT_ENDPOINTS"]`.
  Login, post create / edit, delete, and logout forms now carry
  the hidden input. Delivery has no write endpoints and so does
  not install the guard.
- Server-side sessions backing the admin app
  (`bragi.core.middleware.sessions`). New `Session` model
  (`bragi.core.models.session.Session`) with `id` UUID PK,
  nullable `user_id` FK, `expires_at`, `last_seen_at`, `ip`,
  `user_agent`, and a JSON `data` blob. Custom `SessionInterface`
  replaces Flask's signed-cookie default: cookie carries only
  `bragi_sid` (32-char hex UUID, `HttpOnly`, `SameSite=Lax`); all
  state lives in the row. Logout calls `session.clear()` which
  rotates to a new sid (the old row is deleted, the post-logout
  flash lands on a fresh row): the leaked-cookie risk after
  logout is closed. Default lifetime 14 days; sliding via
  `last_seen_at`. Delivery keeps Flask's default cookie session
  to stay write-free on the read path.
- alembic migration `add_sessions` (revision `38aa6fec0409`)
  adds the `sessions` table with indexes on `user_id` and
  `expires_at`.
- `flask --app bragi.apps.admin cms session purge` deletes
  expired session rows; intended for a cron job. Reports the
  count of removed rows; exit 0 on no-op.
- Site admin Blueprint at `/admin/sites`: list / new / edit views
  plus deactivate / activate POST endpoints. Deactivating sets
  `active=False` so requests to that hostname stop resolving
  without losing config; hard delete is intentionally not exposed
  (would cascade into orphaned content). Hostname and slug edits
  guard against UNIQUE violations with friendly errors. Admin nav
  entry under section `site`.
- `bragi.core.security` helpers: `current_user()` returns the
  logged-in User row (or None), memoized per request via `g`;
  `is_superuser()` is a convenience wrapper. Admin views that
  need DB-coherent role checks call these rather than caching
  `is_superuser` in the session blob.
- NavItem.permission gating in the admin context processor.
  Items with `permission="superuser"` are filtered out of the
  rendered nav for non-superusers. Per-site role values land
  alongside #9.
- `bragi.contrib.sessions` plugin: admin Blueprint covering two
  surfaces. `/admin/account/sessions` lists the current user's
  active sessions with per-row revoke and a 'revoke everywhere
  except this' action; the current session is marked. Revoking
  the current row also clears the cookie and redirects to login.
  `/admin/sessions` is superuser-only and lists every session
  with the same revoke shape. Both nav entries under section
  `system`; the all-sessions entry hides itself for non-superusers.
- Redirects admin Blueprint at `/admin/redirects`: list / new /
  edit / delete views with CSRF on every POST, optional
  per-site filter, pagination at 50 rows. The new/edit form
  validates source_path starts with `/`, status_code is one of
  301/302/307/308/410, and surfaces the per-(site, source,
  match_type) UNIQUE constraint as a friendly flash rather than
  a 500. Admin nav entry under section `site`.
- `bragi.contrib.redirects.plugin.resolve_redirect` now bumps the
  hit row's `hit_count` and writes `last_hit_at` on every served
  redirect. The bump is best-effort: a DB error logs and the
  redirect is still served (a counter glitch never becomes a 500).
- `AuditLog` model (`bragi.core.models.audit_log.AuditLog`) and
  alembic migration `add_audit_log` (revision `9dbc52ee17a8`).
  Polymorphic target via `(target_type, target_id)` weakly
  referenced (no FK to the target table) so audit rows survive
  the deletion they document. Indexed on `occurred_at`, `action`,
  and `actor_user_id` for the typical filter shapes.
- `bragi.core.audit.audit(action, ...)` writer pulls actor /
  ip / user_agent from the request context, falling back to None
  when called from a CLI or background worker. Write is
  best-effort: a DB failure logs and swallows so the operation
  being audited never dies because of audit-side trouble.
  `AuditAction` constants live next to the helper so call sites
  don't sprinkle string literals.
- Call-site emits: post create / edit / delete in
  `bragi.contrib.post.admin`; login success, login failure (with
  the attempted email), logout in `bragi.contrib.auth_local.views`.
- `bragi.contrib.audit` plugin: admin Blueprint at `/admin/audit`
  showing the log most-recent-first, with substring filter on
  action and exact-match filter on actor id. Superuser-only;
  paginated at 50 rows. Nav entry under section `system`,
  permission-gated.
- `bragi.core.security` helpers `current_user()` /
  `is_superuser()` newly added in the previous batch are now the
  basis for the audit-view authorisation check and the
  NavItem.permission gate.
- `Attachment` model (`bragi.core.models.attachment.Attachment`)
  plus migration `add_attachments_and_post_image_fks` (revision
  `9f3fd65db818`). Migration also adds `featured_image_id` and
  `og_image_id` FKs to `posts` (both nullable). SQLite-compatible
  via `op.batch_alter_table` for the FK additions.
- `bragi.core.storage` local-disk backend. Content-addressed
  layout under `Settings.attachments_root`:
  `<site_slug>/<sha256[:2]>/<sha256>`. Uploads are deduped per
  site by SHA-256. `Settings.attachments_max_bytes` defaults to
  20 MiB. The reserved `register_storage_backend` /
  `register_image_processor` hooks point at this surface for the
  later `bragi.contrib.media` plugin (S3, renditions, alt-text).
- `bragi.contrib.attachments` plugin: admin Blueprint at
  `/admin/attachments` with upload form, paginated list, and a
  delete view that drops the on-disk file only when no other
  Attachment row references the same `storage_key`. Delivery
  Blueprint at `/attachments/<storage_key>` serves the bytes
  with a far-future `Cache-Control` (content-addressed; bytes
  never change for a given key). Admin nav entry under content.
  Audit emits for `attachment.uploaded` / `attachment.deleted`.
- htmx partial-rendering convention on the admin app. The post
  list view dispatches on `HX-Request: true` (via
  `bragi.core.htmx.is_htmx`) and returns just the table fragment
  for htmx requests; cold loads still get the full page with
  chrome. Partial template naming convention: a `_<name>.html`
  partial wrapped in a stable id, included by the full-page
  sibling so the markup isn't duplicated. Admin base template
  loads htmx from a CDN; a self-hosted bundle + Subresource
  Integrity hash is a follow-up. Documented in CLAUDE.md.
- TipTap editor on the post edit form, loaded from esm.sh as ES
  modules. Toolbar covers H1/H2/H3, bold, italic, inline code,
  bullet / ordered lists, blockquote, code block, link, unlink.
  The textarea is preserved as the canonical form input: the
  editor's `onUpdate` writes serialized markdown back into it
  via the `tiptap-markdown` extension, so the existing
  `body_markdown` POST path is unchanged. Submitting without
  JS still works (textarea visible). A self-hosted bundle and a
  Subresource Integrity hash are the follow-up to harden the
  v1 CDN dependency.
- `render_markdown` now applies plugin-contributed transforms.
  Pipeline: `md_transforms.apply` -> markdown-it-py ->
  `html_transforms.apply`. Registries are pulled from
  `current_app.extensions`; outside an app context the renderer
  still works with no transforms applied.
- `bragi.contrib.highlight` plugin: registers an html transform
  that rewrites markdown-it-py code blocks
  (`<pre><code class="language-X">`) into Pygments-tokenised
  HTML with span-class runs. Unknown languages fall back to
  `TextLexer` (no highlight, no crash). The plugin's delivery
  Blueprint serves the generated stylesheet at
  `/static/pygments.css`; the delivery base template emits the
  `<link>` when the plugin is loaded via the `pygments_css_url`
  Jinja global. Transform runs at priority 50.
- `bragi.contrib.anchors` plugin: registers an html transform
  that injects `id="<slug>"` on `<h1>` through `<h6>` lacking
  one. Slugify rule is NFKD-ASCII + lowercase + non-alphanumerics
  collapse to `-`. Duplicates within a single document get
  `-2`, `-3`, ... suffixes; existing explicit ids on a heading
  are honoured AND count toward the dedup pool. Transform runs
  at priority 200 (after the highlighter).
- Slug-change auto-301. `post_admin.edit_post` now fires the
  `on_post_updated` plugin hook with `before` / `after`
  snapshots. `bragi.contrib.redirects` adds an `on_post_updated`
  hookimpl that inserts a 301 from `/posts/<old>/` to
  `/posts/<new>/` when the slug changes, with
  `source=RedirectSource.SLUG_CHANGE`. The post edit form has a
  "Skip redirect when slug changes" checkbox for typo fixes on
  drafts. Idempotent: a row that already exists for the same
  (site, source, match_type) is updated in place rather than
  failing the UNIQUE constraint.
- `resolve_redirect` now supports `MatchType.PREFIX` and
  `MatchType.REGEX` in addition to EXACT. Resolution order:
  exact -> longest-prefix -> first-regex-match. PREFIX appends
  the unmatched tail to the target; REGEX uses Python's
  `match.expand` so `\1`, `\2`, ... in the target slot in
  capture groups. A bad regex pattern logs a warning and is
  skipped so one malformed row never 500s the resolver.
- `bragi.contrib.seo` plugin: per-site delivery endpoints for
  `/sitemap.xml` (sitemaps.org XML over published posts),
  `/robots.txt` (allow-all + `Sitemap:` pointing at the site's
  canonical URL), and `/.well-known/security.txt` (RFC 9116;
  404 unless `Settings.security_contact` is set, with
  `Settings.security_expires_days` controlling the `Expires:`
  horizon). Multiple Blueprints registered from one plugin via
  the `@hookimpl(specname="register_delivery_blueprint")`
  pattern.
- Per-site Atom 1.0 feed at `/feed.xml` in the SEO plugin. Lists
  the `FEED_ENTRY_LIMIT = 50` most-recent published posts, with
  author display name from `User.display_name`, CDATA-wrapped
  HTML content (`type="html"`), and self/alternate links built
  from `site.canonical_url`. Per-site isolation via the resolved
  `g.site`; unknown hosts 404.
- BlogPosting JSON-LD on `/posts/<slug>/`. The delivery base
  template gained a `{% block jsonld %}`; the post template
  emits a `<script type="application/ld+json">` carrying
  `@type=BlogPosting` with headline, url, datePublished,
  dateModified, author (Person), publisher (Organization from
  `site.title`), and description (falls back
  meta_description -> body_excerpt). Rendered via the `tojson`
  filter for safe escaping.
- Analytics events sink. New `AnalyticsEvent` model
  (`site_id`, `event_type`, `path`, `referrer`,
  `user_agent_class`, `user_id`, `occurred_at`, `extra` JSON)
  with indexes on the discriminator and time columns. Migration
  `add_analytics_events` (revision `1f966d693a2d`). User-agent
  classifier in `bragi.core.useragent` returns one of
  `browser` / `bot` / `feed-reader` / `other`; bots are
  filtered at emit time so they never pollute the table.
  `bragi.contrib.analytics` registers an `after_request` hook
  on the delivery app that records a `pageview` for HTML GET
  responses (skips 4xx/5xx, non-HTML, non-GET). Plugins emit
  their own events through the `record_analytics_event` hook.
- Analytics admin dashboard at `/admin/analytics/`,
  superuser-gated (403 otherwise; nav entry hidden when not
  superuser). Renders pageviews-by-day-and-UA-class over the
  last 30 days as a pivot table. `CONTEXT.md` calls out the
  rolling-monthly-table upgrade path
  (`analytics_events_2026_05`) once the single table starts to
  hurt under vacuum / time-range queries.
- `Site.extra_settings`: schemaless JSON blob on `sites` for
  plugin-contributed flat config. `MutableDict.as_mutable`
  tracks in-place mutations so `site.extra_settings["x"] = 1`
  survives commit without manual reassignment. Migration
  `add_extra_settings_to_sites` (revision `f679d5fc62bb`),
  with a `server_default='{}'` so existing rows backfill cleanly
  on upgrade.
- Lifecycle hooks wired into the post admin:
  `pm.hook.on_post_published` fires on first publish transition
  (both `new_post` when status starts as published and
  `edit_post` when status flips), and `pm.hook.on_post_deleted`
  fires before commit in `delete_post` so subscribers see the
  row in-session (useful for emitting a 410 tombstone redirect).
  `on_post_updated` was already wired by #11.
- `SiteAlias` model: extra hostnames that resolve to a Site via
  the site_resolver's fallback lookup
  (`bragi.core.middleware.site_resolver`). UNIQUE constraint
  spans `sites.hostname` and `site_aliases.hostname` so a
  hostname is never ambiguous. New CLI subgroup
  `cms site alias add/list/remove`; admin alias management on
  the Site edit form. Migration `add_site_aliases` (revision
  `0e57d255f32d`).
- `Page` content type for static / hierarchical content.
  `bragi.contrib.page` plugin: ContentTypeSpec, admin at
  `/admin/pages`, delivery catch-all that resolves slash-joined
  slug chains by walking `parent_id` step-by-step. Slug
  uniqueness is `(site_id, parent_id, slug)`; the DB UNIQUE
  constraint catches non-root collisions while the admin's
  app-level pre-flight covers the root case (SQLite UNIQUE
  treats NULL as distinct). Delete refuses to remove a page
  with children. Migration `add_pages` (revision
  `6275dd6feda6`).
- `Tag` model + `post_tags` junction with `ON DELETE CASCADE`.
  Tags are per-site (`UniqueConstraint(site_id, slug)`); the
  CSV input on the post edit form upserts by slug so existing
  tags get re-used. The post tag relationship is
  `selectin`-loaded to keep the list view a single query.
  Delivery surfaces a per-tag listing at `/tags/<slug>/` that
  shows the published posts attached to the tag, ordered
  newest-first; drafts and other-site posts never leak. The
  shared slug helper moved from `bragi.contrib.anchors.transform`
  to `bragi.core.text.slugify` so post / tag / anchor code can
  reuse it without crossing the plugin boundary. Migration
  `add_tags` (revision `46fe76487384`).
- `LocalCredential.must_change` is now enforced. After
  successful login, a credential row with `must_change=True`
  redirects to `/auth/change-password` instead of the original
  `next`; the admin guard only releases the user from that page
  once the password has been rotated. `cms user create` gained
  a `--must-change/--no-must-change` flag that defaults to ON
  when the password was generated (typical bootstrap path) and
  OFF when the operator supplied one explicitly.
- Per-site authorisation via `UserSiteRole` + the
  `bragi.core.permissions` helper. Three ranks
  (`admin > editor > author`); `is_superuser=True` short-circuits
  every check. The post admin guards now read:
  list / new / edit-own require `author+`; edit-any and delete
  require `editor+`. New CLI subcommand `cms user grant` upserts
  the `(user_id, site_id)` row. Migration `add_user_site_roles`
  (revision `2ecc724d6dfb`).
- Hugo importer (`bragi.contrib.import_hugo`). Detects a Hugo
  source tree by looking for `config.{toml,yaml,yml}` /
  `hugo.{toml,yaml,yml}` plus a `content/` directory, walks
  `content/**/*.md` (skipping section indexes `_index.md`),
  parses TOML / YAML frontmatter, and creates Post rows. Every
  `aliases:` entry becomes a 301 Redirect from the legacy URL
  to the post's bragi canonical (`/posts/<slug>/`). Idempotent
  via `Post.source_id` keyed on the repo-relative path:
  re-running an import updates rows in place. New runtime dep:
  `pyyaml`. CLI: `cms import hugo --site <slug> [--author
  <email>] [--dry-run] <path>`.
- Ghost importer (`bragi.contrib.import_ghost`). Detects either
  a `.json` export file or a directory containing one. Bodies
  arrive as HTML and get converted to markdown via
  `markdownify(heading_style="ATX")`; the rendered HTML is
  re-derived from that markdown so the rest of bragi's render
  pipeline (highlight, anchors, etc.) applies. Tags come from
  `data.tags` + `data.posts_tags`; authors match existing
  Users by email (else fall back to the first user). For every
  published post a 301 Redirect from Ghost's permalink
  (`/<slug>/`) to bragi's (`/posts/<slug>/`) lands so legacy
  bookmarks survive. Idempotent via `Post.source_id` = Ghost
  post id. CLI: `cms import ghost --site <slug> [--author
  <email>] [--dry-run] <path>`.
- GitHub OAuth via Authlib + `UserIdentity` table.
  `bragi.contrib.auth_github` plugin registers an OAuth provider
  spec and a Blueprint with `/auth/github/login` and
  `/auth/github/callback`. Callback fetches the GitHub profile,
  reuses an existing identity by `(provider, provider_user_id)`
  or falls back to email-match for operator-seeded users, then
  creates a new User if neither matched. Fires
  `pm.hook.on_user_login(user=, method='github', request=)`.
  Two new settings: `BRAGI_GITHUB_CLIENT_ID` /
  `BRAGI_GITHUB_CLIENT_SECRET`; both must be set for the flow
  to start (the login endpoint returns 503 when either is
  unset). New runtime dependency: `requests` (Authlib's Flask
  integration imports it lazily). Migration
  `add_user_identities` (revision `48e608e60109`).
