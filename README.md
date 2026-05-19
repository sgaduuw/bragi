# bragi

A multisite CMS built with Python, Flask, and htmx. Markdown source
of truth, plugin-extensible from day one, SEO as a first-class
citizen.

## Status

1.14.0 shipped 2026-05-19. Theme catalog, multi-arch images, and
plugin-platform hardening. Added: three new in-tree themes
(`bragi.contrib.theme_minimal`, `theme_serif`, `theme_terminal`),
each with `@media (prefers-color-scheme: dark)` and a
`<meta name="color-scheme" content="light dark">` hint, so every
shipped theme follows the visitor's OS preference automatically;
`theme_default` retrofitted with the same dark-mode treatment so
the auto-light/dark contract is uniform across the in-tree set;
admin theme picker now lists four options instead of one (#126).
README gains an "Authoring a third-party theme" section covering
the `bragi-theme-<slug>` distribution-name convention, the
`register_theme` hookimpl pattern, the `delivery/base.html` block
surface a theme must preserve, the `/theme/<slug>/static/<path>`
URL space, the recommended `prefers-color-scheme: dark` recipe,
and the install / activate / disable cycle. Container images now
ship as multi-arch manifest lists covering `linux/amd64` and
`linux/arm64` on every tag push: Apple Silicon laptops, Ampere /
Graviton servers, and ARM homelabs run natively rather than under
QEMU emulation; `docker pull` resolves the right variant for the
host architecture automatically (#167). Plugin-set boot smoke
test asserts that `create_admin_app()` and `create_delivery_app()`
boot cleanly under the real entry-point manifest, that every
declared entry-point name reaches the running `PluginManager`,
that every loaded plugin contributes a hookimpl, and that
back-to-back factory calls both succeed (#169). Changed:
`Registry.add_*` methods now dedup on the canonical unique field
(`.name` / `.slug` / `.endpoint`) and raise
`DuplicateRegistration` on collision instead of bare-appending and
silently shadowing the second registration; external
architectural review (2026-05-18) flagged this as a load-bearing
silent-failure surface (#188). Sitemap builder prewarms the
page-URL identity map in one bulk SELECT before iterating
content-type rows, dropping `K * D` per-row queries on a deep
docs-style page tree to one query for the whole sitemap; rows are
stashed on `db.info` so SQLAlchemy's weak-referenced identity map
can't drop them mid-loop (#172). Closes #126, #167, #169, #172,
#188.

1.13.0 shipped 2026-05-19. Eighth audit-pass rollup plus operator
ergonomics. Security: webmention receiver gates `source` /
`target` through the centralised `safe_external_url` (the local
`_is_absolute_http` helper that accepted Unicode bidi-formatting
codepoints and C0/DEL control characters is gone, closing a
moderation-row spoofing surface and a 500-on-redirect surface);
`safe_external_url` now rejects U+202A-U+202E / U+2066-U+2069
and C0/DEL control characters at the gate, backstopping any
future callers (#209); ActivityPub inbox catches `RecursionError`
on deeply-nested JSON so an unauthenticated attacker can no
longer flood the inbox with `[[[...]]]` past Python's default
1000-frame recursion limit and trigger uncaught 500s (#215).
Added: `cms plugins list` prints every registered plugin with
its origin (`in-tree` vs distribution + version) and hookimpl
count, intended for operators triaging "is this plugin even
loaded?" and for plugin authors auditing what they ship against
(#190); `bragi.api` carries a top-of-module stability docstring
codifying what's covered (hookimpl, hookspec signatures, spec
dataclasses, entry-point group) and what isn't, plus the
two-step deprecation policy (#190); new
`tests/contrib/test_plugin_layout.py` walks every in-tree
`bragi.contrib.*` package and asserts each plugin's `templates/`
top-level entries are either the plugin's own slug or the
shared prefixes `admin` / `delivery` (so two plugins shipping
`templates/detail.html` can't shadow each other unpredictably);
admin backlinks view ("Backlinks »" link on the post / page
edit forms) lists every same-site post / page that references
this target via `data-bragi-link`, backed by a new
`internal_links` edge table populated by the internal_links
plugin's `on_post_published` / `on_post_updated` /
`on_post_deleted` hooks (#116). Fixed: attachment delete cleanup
moved between `db.flush()` and `db.commit()` so SQLAlchemy's
writer lock queues other writers under WAL until our cleanup
commits, closing a narrow refcount race where a concurrent
upload of the same content-addressed bytes could insert a row
referencing the storage_key we then unlinked (#171); empty
`BRAGI_ADMIN_SESSION_COOKIE_SECURE=` no longer crashes boot
(#226); `pyproject.toml` migrated to PEP 621 `[project]` table
ahead of Poetry 2.x metadata deprecation (#168, portfolio-wide
sweep). Plus moderator-facing `[IDN]` badge on the webmention
moderation list (#225), `cms db vacuum` SQLite-engine gate
mirroring `cms backup` (#227), admin session cookie defaults to
`Secure` in production (#199), redirects admin caps
`source_path` at 256 characters to bound regex backtracking
worst case (#200), and a denser `compose.yml` admin block with
per-knob rationale moved to the `Settings` field docstrings
(#232). Closes #168, #171, #189, #190, #199, #200, #205, #209,
#212, #215, #217, #224, #225, #226, #227, #229, #230, #231,
#232 plus the pass-7 / pass-8 finding rollups (PRs #223, #233,
#234).

1.12.0 shipped 2026-05-18. Hardening release across the v1.11.0
surface following seven rounds of audit. Security: GitHub OAuth
no longer auto-links a new identity onto an existing local user
by matching email (an account-takeover primitive against
operators with both auth methods enabled); attachment uploads
gate on a content-type allowlist plus Pillow magic-byte
verification, delivery serves with `X-Content-Type-Options:
nosniff` and inline-disposition only for safe types
(closes a stored-XSS surface via SVG / HTML / forged content-type);
redirects admin rejects absolute URLs, protocol-relative `//`,
backslash-escaped (`/\evil.example/x`), and C0/DEL control
characters in `target=` (open-redirect + persistent per-URL
500 DoS); page-admin validates cross-site `parent_id` on both
create and edit and on revision restore; webmention receiver
verifies source HTML before persisting (closes a per-request DoS
surface), dedupes repeat `(source, target)` presentations, gates
h-card URLs to http(s)-only (stored-XSS via `javascript:` URL),
and tightens the source-fetch timeout to 3 s; the SSRF guard
re-resolves the host at send-time to refuse DNS rebinding; the
bearer middleware caches verify outcomes to bound argon2 DoS
amplification, invalidates the cache on token revoke, and
re-checks `expires_at` per request; `current_user()` treats
`is_active=False` users as anonymous; ActivityPub Signature
parsing is quote-aware. Ops: containers run as a non-root
`bragi` user, gunicorn has a 25 s graceful-shutdown window,
`compose.yml` sets `stop_grace_period: 30 s` and
`BRAGI_TRUSTED_PROXY_HOPS=1` (apps wrap in
`werkzeug.middleware.proxy_fix.ProxyFix` when the knob is > 0
so OAuth callbacks build as `https://...` and audit / session
rows record real client IPs rather than the proxy), the task
sidecar retries `alembic upgrade head` with backoff and exits 0
after exhausting attempts (no more livelock on broken
migrations), the GHCR `:latest` tag is gated to non-prerelease
semver tags via `flavor: latest=auto`, and the webmention +
ActivityPub outboxes abandon PENDING rows on post-unpublish so
followers don't receive Notes pointing at a 404. CQ: FTS5
search `total` is short-TTL cached and invalidated on lifecycle
events with case-folded sorted token keys; page / post revision
restore fires `on_post_updated` and `on_post_published` so
plugin subscribers see the same lifecycle a hand edit produces;
the actor cache reads under its lock and rechecks after fetch;
the AP fanout is idempotent across restore-as-republish so
followers don't receive duplicate Notes; `cms backup` /
`cms export` route timestamps through `aware_utcnow()`; the
audit-log `action` filter escapes SQL `LIKE` metacharacters.
Closes #181, #182, #183, #184, #186, #187, #195 plus the
audit-pass-1-through-7 finding rollups (PRs #194, #196, #197,
#198, #208, #214, #220).

1.11.0 shipped 2026-05-18. Three publishing surfaces land in this
release: ActivityPub federation (one follow-able actor per site,
RSA-SHA256 HTTP signatures, Mastodon-compatible Follow / Undo,
Create+Note fanout on publish), indieweb webmentions (send +
receive + admin moderation, h-card author parsing, "Mentioned by"
aside), and personal access tokens with a JSON REST surface
(`/admin/api/sites/<slug>/posts/`, scope-gated by `post:write`).
Plus: Open Graph + Twitter Card meta on every post / page, per-tag
Atom feeds, auto-rendered table of contents on multi-section
posts, chronological archive at `<post_index>/archive/`, post-page
chrome (author byline, reading time, "Updated YYYY-MM-DD"),
related-posts aside, `cms export` (Hugo-shaped corpus dump),
`cms backup` (single-file DB + attachments tarball), KaTeX +
Mermaid + footnotes markdown, and a `/healthz` liveness endpoint
on both apps. The deploy posture also hardened: SSRF guard on
every outbound HTTP call driven by remote input, body-size cap
(`MAX_CONTENT_LENGTH`), in-process replay cache on the AP inbox,
production `SECRET_KEY` boot check, and `ON DELETE` actions
across the full model graph so removing a Site / Post / User no
longer leaves orphan rows. The hardcoded `/posts/` URL space is
gone: posts now live under a per-site `Page` of kind `post_index`,
so a site at `/blog/` gets post URLs at `/blog/<slug>/`. The
migration auto-creates a `slug="posts"` post_index on every
existing site to keep legacy URLs resolving without operator
intervention. Closes #117, #115, #143, #144, #145, #146, #147,
#148, #137, #133, #130, #129, #128, #127, #126, #124.

1.10.0 shipped 2026-05-16. All day-one built-in plugins are in
place: Post, Page, Tag, GitHub OAuth + local-credential auth
(with `must_change` rotation), Hugo / Ghost / WordPress
importers, redirects with prefix / regex matching and slug-change
auto-301, delivery-time internal links (`[text](post:42)` /
`[text](page:about)`) that follow slug renames without re-rendering
source bodies, with an admin TipTap picker for inserting them,
per-site analytics (with UA classification), attachments
with a full media library (renditions, bulk alt-text, TipTap embed
picker, `<picture srcset>` at delivery), server-side sessions,
audit log, Pygments highlighting, heading anchors, per-site
`sitemap.xml` / `robots.txt` / `security.txt` / Atom `feed.xml`,
BlogPosting JSON-LD, TipTap editor, per-site roles plus first-class
site ownership, post / page revision history, HTTP cache management
with an `on_cache_purge` plugin hookspec, IndexNow push-crawl on
publish / update / delete, file-based themes (with an in-tree
default theme registered through the same `register_theme` hook a
third-party theme package would use), SQLite FTS5 search, per-site
team management UI, external-content embeds (YouTube click-to-load,
Bluesky, allowlisted oEmbed), and a task-runner sidecar container
that owns alembic plus periodic scheduled-publish, pending-embed
rerender, and SQLite maintenance ticks.

1.9.1 fixes a quietly broken task-runner sidecar: the
`flask --app bragi.apps.admin cms ...` invocation in
`docker/scheduler.sh` had been silently exiting rc=2 since
1.8.0 because Flask's CLI autodiscovery only resolves factories
named `create_app` / `make_app`, not `create_admin_app`. The
sidecar kept ticking but nothing scheduled-publish, pending
embed retries, or SQLite maintenance ever ran. Hotfix uses the
explicit `module:factory` form everywhere it was misspelled.

1.9.0 promotes the canonical site shell to a registered theme.
The `delivery/base.html` template moved out of
`bragi/templates/delivery/` and into a new
`bragi.contrib.theme_default` contrib package, registered under
slug `"default"` via `register_theme`: the same hook surface a
third-party `bragi-theme-foo` package uses. `ThemeAwareLoader`
now resolves `Site.theme=NULL` to slug `"default"`, and falls
back to `"default"` for any uninstalled slug rather than
rendering an unstyled page. `Site.theme` stays nullable; no
data migration. The two empty namespace-only packages
`bragi.core.auth` and `bragi.core.content` (docstring-only,
zero callers) were also removed.

1.8.0 adds external-content embeds (`bragi.contrib.embeds`) and
a task-runner sidecar container. The new markdown directive
`::: embed <url> :::` resolves URLs at save time, dispatches to a
provider (YouTube click-to-load, Bluesky, allowlisted oEmbed),
and inlines the rendered HTML into `body_html`; readers never hit
external services. Save-time failures fall back to a styled link
card that the sidecar's `cms embeds rerender-pending` tick
retries on a cadence. The sidecar (`bragi-tasks` service in
`compose.yml`) replaces the one-shot `migrate` container,
owns `alembic upgrade head` on start, and dispatches periodic
`cms scheduled-publish`, `cms embeds rerender-pending`,
`cms db analyze`, and `cms db vacuum` commands at configurable
intervals. The `register_markdown_extension` plugin hook is now
wired end-to-end on both admin and delivery factories; no
schema change.

1.7.0 reorders the admin post list by `COALESCE(published_at,
updated_at) DESC` instead of `created_at DESC`. Published posts
sort by publication date, drafts by last edit, and imported posts
land in their original Ghost / WordPress / Hugo publish order
rather than reflecting the importer's iteration order. Editing
an old draft also bubbles it back up. No schema or hookspec
change; admin URLs and delivery output are unchanged.

1.6.1 fixes a Ghost-importer detection regression on Ghost 6.x
exports (#95): the earlier head-scan heuristic looked for
`"posts"` in the first 4 KB, but modern exports lead `db[0].data`
with `benefits` / `custom_theme_settings`, pushing `posts` past
the cutoff and causing valid exports to be rejected. Detection
now does a full parse; no schema or interface change.

1.6.0 cleans up the production deploy posture: containers run
gunicorn against the WSGI factory (sync workers, access log on
stdout) instead of Werkzeug's dev server. Worker counts default
to 2 / 4 (admin / delivery), tunable via `ADMIN_WORKERS` /
`DELIVERY_WORKERS`. No code, schema, or interface changes; the
old image silently ran the dev server, the new image doesn't.

1.5.0 wrapped the four-phase IA refactor (#77, #78, #79, #80):
admin content URLs moved under `/admin/sites/<slug>/...`, analytics
scoped to the site you've entered, owners get a UI to invite
collaborators. Public delivery URLs are unchanged; plugin hookspecs
are unchanged.

Releases follow git-flow with `develop` as the default branch.
Container images ship to GHCR as `bragi-admin:vX.Y.Z` and
`bragi-delivery:vX.Y.Z` on every tag push, as multi-arch
manifest lists covering `linux/amd64` and `linux/arm64`. `docker
pull` resolves the right variant for the host architecture
automatically; Apple Silicon laptops, Ampere / Graviton servers,
and ARM homelabs run natively rather than through QEMU
emulation.

## What bragi is

- **Multisite by design.** One database serves many sites; the Host
  header at the WSGI edge resolves to a Site row. Every content
  table has a `site_id` FK.
- **Sites are first-class workspaces.** Each site has a designated
  owner (with implicit-admin power) and a collaborator roster.
  Admin content lives under `/admin/sites/<slug>/...` (posts,
  pages, redirects, attachments, analytics, team), with a per-site
  dashboard and a picker that auto-redirects single-site users
  into their workspace. Cross-site id probes return 404 so the
  response code can't be used to enumerate other sites' content.
- **Two-binary architecture.** `bragi-admin` (editor UI, write API)
  and `bragi-delivery` (read-only public renderer) share one DB
  and one plugin manager; only the middleware stacks and registered
  Blueprints differ. Admin runs on its own subdomain.
- **htmx as the render strategy.** Server-rendered HTML always;
  partial swaps via the `HX-Request` header. No SPA, no
  client-side routing, no separate prerender step. Crawlers see
  complete pages.
- **Markdown source of truth.** Post and Page bodies persist as
  markdown text with a cached HTML render alongside. TipTap (with
  its markdown serializer) is the admin editor; the data model is
  editor-independent. CommonMark + tables out of the box; the
  `markdown_extras` built-in plugin adds footnotes
  (`text[^id]` + `[^id]: body`), KaTeX-compatible math
  (`$x$` / `$$x$$`), and Mermaid code fences
  (` ```mermaid `). Plugins can register more extensions via
  the `register_markdown_extension` hookspec.
- **Post-page chrome.** Each post renders with an author byline,
  reading-time estimate (220 WPM, rounded up), and an
  "Updated YYYY-MM-DD" line that only appears when the edit is
  meaningfully after first publish. Optional `User.bio` text
  surfaces as an "About the author" aside below the body. A
  table of contents auto-renders for multi-section posts (h2 /
  h3 headings).
- **Related posts at end of article.** Tag-overlap ranks
  same-site published posts ("more shared tags wins, recency
  ties"); rendered as a "You may also like" aside under the body.
  Per-site count override via `Site.extra_settings["related_posts_count"]`
  (default 3); zero-tag posts render no aside.
- **Chronological archive.** `<post_index>/archive/` lists years
  with counts (newest first); drilling in shows months for that
  year, then posts in that month (oldest first, journal-style).
  Drafts are excluded; out-of-range or empty buckets 404. Each
  level carries the standard `ETag` + `Last-Modified` validators
  so feed readers and crawlers get cheap 304s.
- **Plugin-extensible from day one.** Built-ins (Post, Page,
  redirects, importers, analytics, ...) register through the
  `bragi.plugins` entry-point group, the same path third parties
  use. No internal fast path.
- **SEO as a first-class citizen.** Per-page title / meta /
  canonical / JSON-LD editable in admin. Open Graph + Twitter
  Card meta on every post and page (with a per-post / per-page
  attachment override and a per-site default OG image), so
  social shares render rich previews. Per-site `sitemap.xml`,
  `robots.txt`, `security.txt`. Atom 1.0 feeds at `/feed.xml`
  (whole site) and `<post_index>/<tag_segment>/<slug>/feed.xml`
  (per tag). Server-side Pygments highlighting for code blocks
  (Ansible / Python / Terraform lexers in core).
- **Redirects as a core subsystem.** Slug renames auto-301;
  importers preserve source URLs as redirect rows; resolution
  middleware runs on every public 404. `410 Gone` for tombstoned
  content.
- **Revision history.** Every post / page save captures a
  pre-edit snapshot in `post_revisions` / `page_revisions`.
  Admin views list revisions, show a side-by-side with the live
  row, and restore (with the restore itself recorded as a fresh
  revision so it stays reversible).
- **HTTP caching baked in.** Delivery 2xx HTML carries
  `Cache-Control` (short browser cache, longer shared cache),
  weak `ETag`, and `Last-Modified`; `If-None-Match` /
  `If-Modified-Since` short-circuit to 304. Admin forces
  `private, no-store`. The `on_cache_purge` plugin hookspec
  fires on every content commit so a CDN invalidator has
  something to subscribe to.
- **Push-crawl via IndexNow.** Post / page publish, update, and
  delete fire a fire-and-forget POST to the configured IndexNow
  endpoint so participating search engines (Bing, Yandex, Seznam,
  Naver, ...) hear about the change immediately. Per-site key
  bootstrapped with `cms indexnow setup --site <slug>`; the
  verification key file lives at `/<key>.txt` on the delivery
  app.
- **Programmatic posting via API tokens.** Personal access
  tokens at `/admin/account/tokens/` (list / create / revoke;
  plaintext shown once on create) authenticate scripts and bots
  via `Authorization: Bearer brg_<id>_<secret>`. The JSON REST
  surface at `/admin/api/sites/<slug>/posts/` covers GET list,
  POST create, PATCH update, and POST publish, scope-gated by
  `post:write`. Argon2id-hashed at rest; expiry honoured; every
  use recorded in the audit log.
- **Indieweb webmentions (send + receive).** Outbound: on
  publish, every external link in a post is queued; the
  cron-driven `cms webmentions send-pending` performs W3C
  endpoint discovery (Link header, then
  `<link rel="webmention">`) and POSTs the mention. Inbound:
  `POST /webmentions` on the delivery app validates the source
  actually links to the target, extracts an h-card author
  shape, and stores the mention pending admin moderation.
  Approved rows render in a "Mentioned by" aside under the
  post; discovery `<link rel="webmention">` is injected into
  the delivery `<head>` automatically.
- **ActivityPub federation (one actor per site).** Each site
  is a follow-able fediverse actor addressed as
  `@<site-slug>@<hostname>`. Endpoints (delivery app):
  `/.well-known/webfinger`, `/actor`, `/actor/inbox`,
  `/actor/outbox`, `/actor/followers`. Mastodon-compatible
  HTTP signatures (RSA-SHA256, draft-cavage-12) on outbound
  POSTs; inbound `Follow` / `Undo Follow` verified against the
  sender's public key. On post publish, a Create+Note fans out
  to every follower; `cms activitypub send-pending` ships
  the queued deliveries. Per-site keypair generated on first
  `/actor` hit or via `cms activitypub keygen --site <slug>`.

## What bragi is not

- Not multilingual at the post level. Each `Site` has one `locale`;
  per-post translations are not supported.
- Not a SaaS / multi-tenant cloud product. Single-operator, with
  multiple sites under that operator.
- Not a block-tree editor. Markdown is the source of truth.
- Not a real-time / collaborative editor.

## Stack

- Python 3.12+
- Flask 3.x
- SQLAlchemy 2.0 + alembic
- Pydantic Settings
- pluggy (plugin framework)
- Authlib (GitHub OAuth + future OIDC providers)
- markdown-it-py + Pygments
- htmx (delivery side) + TipTap (admin editor)
- SQLite (WAL) primary store; DuckDB reserved for later dataset
  paths
- gunicorn (production WSGI server, sync worker class)

## Importers

All three ship in 1.x and are idempotent via `Post.source_id`,
so re-running the importer over an updated source updates rows
in place rather than duplicating them.

- **Hugo**: walks `content/**/*.md` (skipping `_index.md`),
  parses TOML or YAML frontmatter, and copies the markdown body
  through verbatim. The same bragi markdown pipeline that runs
  on native authoring then renders it, so no shortcode
  translation step is needed. Every `aliases:` entry becomes a
  301 Redirect from the legacy URL to the post's bragi canonical
  under the site's `post_index` page (e.g. `/blog/<slug>/` when
  the site's post index lives at `/blog/`); fragments and query
  strings on the alias are stripped before matching. Sites with
  no `post_index` page have no public post URLs, so the importer
  skips the redirect emission for those. `tags:` lists upsert by
  slug. CLI: `cms import hugo --site <slug> [--author <email>]
  [--dry-run] <path>`.
- **Ghost**: parses the single-file JSON export
  (`db[0].data.posts`). Bodies arrive as HTML and convert to
  markdown via `markdownify(heading_style="ATX")`; tags come
  from `data.tags` + `data.posts_tags`; authors match existing
  Users by email (else fall back to the first user). For every
  published post a 301 lands from Ghost's permalink (`/<slug>/`)
  to bragi's canonical under the site's `post_index` page (e.g.
  `/blog/<slug>/`) so legacy bookmarks survive. CLI:
  `cms import ghost --site <slug> [--author <email>] [--dry-run]
  <path>`.
- **WordPress**: parses WXR (WordPress eXtended RSS) XML
  exports. `wp:post_type=post` rows become Posts, `page` rows
  become Pages; bodies are converted from WordPress HTML to
  markdown and run through the same pipeline. Categories and
  tags upsert by slug; authors match by email or fall back to
  the first user. Permalinks captured at export time become 301
  redirects to the bragi canonical (posts resolve through the
  site's `post_index` page; pages resolve through the static-page
  chain). Idempotency keys on `(site_id, source_id)` via
  `wp:post_id`. CLI: `cms import wordpress --site <slug>
  [--author <email>] [--dry-run] <wxr.xml>`.

Notion, Substack, and Medium importers are deferred to
follow-up packages; no v1.x commitment.

## Export (portability)

`flask --app bragi.apps.admin cms export [--site <slug>] [--output <dir>]`
writes a Hugo-shaped tree per site: posts as
`content/posts/<slug>.md` with YAML frontmatter, pages under
`content/pages/`, attachment bytes under `static/attachments/`
alongside an `attachments.csv` metadata manifest, and the
per-site redirect table as `redirects.csv`. Default output is
`bragi-export-YYYYMMDD-HHMMSS/` in the CWD.

Output is deterministic: re-running against an unchanged DB
yields byte-identical files, so a periodic `cms export` doubles
as a diffable snapshot. Posts round-trip through `cms import
hugo`: importing the export and re-exporting changes nothing
beyond timestamps, so the corpus is portable back into any Hugo
build at any time.

## Backups

`flask --app bragi.apps.admin cms backup [--output PATH]` writes
a single `.tar.gz` containing a consistent SQLite snapshot
(produced with `VACUUM INTO`, so no companion `-wal` / `-shm`
files) plus the contents of `Settings.attachments_root` as
`attachments/`. Default output: `bragi-backup-YYYYMMDD-HHMMSS.tar.gz`
in the current working directory.

To restore: extract the tarball, drop `bragi.db` and
`attachments/` into a fresh deployment (matching paths), and
restart the admin + delivery processes. There is no `restore`
subcommand by design; a tool that overwrites a live deployment
is a big risk for not much help.

`cms backup` is SQLite-only and exits 2 with a clear message
under a non-SQLite `BRAGI_DATABASE_URL` (its `VACUUM INTO` is
SQLite-specific). Postgres operators: use `pg_dump` for the
DB half and a separate tar of `attachments_root` for the file
half. `cms db vacuum` follows the same gate (`PRAGMA
wal_checkpoint(TRUNCATE)` is SQLite-only); on Postgres use
`VACUUM (FULL)` or your usual autovacuum tooling instead.

## Quick start (development)

```sh
poetry install
poetry run alembic upgrade head
make dev    # runs bragi-admin on :8001 and bragi-delivery on :8002 via honcho
```

Then:

- Admin: <http://127.0.0.1:8001/>
- Delivery preview: <http://127.0.0.1:8002/> (with a configured
  Site hostname resolving to localhost)

Lint, type-check, and test:

```sh
make lint
make typecheck
make test
```

## Quick start (production / docker compose)

The repo ships an example [compose.yml](compose.yml) that pulls
the published images from GHCR. The tag is parameterised via
`BRAGI_TAG` (default `latest`); pin to a specific release in
production:

```sh
BRAGI_TAG=v1.14.0 BRAGI_SECRET_KEY="$(openssl rand -hex 32)" docker compose up -d
```

A `bragi-tasks` sidecar owns `alembic upgrade head` on start
(touching `/data/.migrated` once the schema is current), then
enters a sleeper loop that dispatches periodic CMS commands:
`scheduled-publish` (flips drafts whose `scheduled_for` has
elapsed), `embeds rerender-pending`, `webmentions send-pending`,
`activitypub send-pending`, `db analyze` (daily), and `db vacuum`
(weekly). The admin and delivery services gate their start on
the sidecar's healthcheck, so a fresh deploy and a schema-bump
deploy work the same way. Each web container also exposes its
own `/healthz` endpoint that does a `SELECT 1` round-trip; the
compose healthcheck stanza watches both so a wedged worker
restarts via `restart: unless-stopped`. The shared `bragi-data`
volume backs `/data/bragi.db`, `/data/uploads/` (attachments),
and the `/data/.migrated` sentinel; back it up. Ports bind to
`127.0.0.1` only; front the apps with a reverse proxy
(Caddy / nginx / Traefik) for TLS and hostname routing.

`BRAGI_ENV=production` (set on both web services in the example
compose) tells the app it's running in production. When set,
booting with the bundled dev `SECRET_KEY` is fatal rather than
just logging a warning, so a misconfigured `BRAGI_SECRET_KEY`
fails loud instead of running with a predictable signing key.
Leave unset for local dev.

`BRAGI_TRUSTED_PROXY_HOPS` (default 0; the example compose sets
it to 1) tells the apps how many trusted reverse-proxy hops sit
in front of them. When > 0, both `create_admin_app` and
`create_delivery_app` wrap the WSGI callable in
`werkzeug.middleware.proxy_fix.ProxyFix(x_for, x_proto, x_host)`
with that hop count. Without it, three breakages manifest on a
fresh prod deploy: (a) `url_for(..., _external=True)` for the
GitHub OAuth `redirect_uri` emits `http://...` and GitHub
rejects the callback; (b) every `AuditLog.ip` and `Session.ip`
row records the reverse proxy's IP, hiding real client IPs;
(c) per-IP analytics groups every visit under the proxy.
**Never set this higher than the actual reverse-proxy depth**:
each unit of trust extends the `X-Forwarded-*` spoofability
boundary one hop outward.

Container runtime hardening already in the published images:
both `admin` and `delivery` run as a non-root `bragi` user
(`--uid 1000`, pinned identically across the two so the shared
`/data` volume is writable from both); gunicorn ships with
`--graceful-timeout 25` paired with `stop_grace_period: 30s`
on the compose services so an in-flight outbound POST
(webmention sender, AP delivery) has up to 25 s to return on
`docker compose stop` before SIGKILL fires; the `bragi-tasks`
sidecar retries `alembic upgrade head` with backoff
(`ALEMBIC_MAX_ATTEMPTS=5`, `ALEMBIC_RETRY_DELAY=15s`) and exits
0 after exhausting attempts so a broken migration shows as a
clean `Exited (0)` rather than livelocking the deploy.

`BRAGI_MAX_REQUEST_BYTES` (default 1 MiB) caps the request body
size to protect the federation inboxes from streaming-body OOM.
On the admin app, this cap is automatically raised to
`max(max_request_bytes, attachments_max_bytes + 64 KiB)` so
attachment uploads up to `BRAGI_ATTACHMENTS_MAX_BYTES`
(default 20 MiB) still go through. Raise both knobs in lockstep
for larger uploads.

Both apps run under gunicorn inside the container (sync worker
class; `--access-logfile -` to stdout). Worker counts default to
2 for admin and 4 for delivery; tune via `ADMIN_WORKERS` /
`DELIVERY_WORKERS` env vars on each service if your traffic
shape needs it.

Task-runner cadences (all in seconds, set on the `bragi-tasks`
service) default to `SCHEDULED_PUBLISH_EVERY=60`,
`EMBEDS_RERENDER_EVERY=600`, `WEBMENTIONS_SEND_EVERY=300`,
`ACTIVITYPUB_SEND_EVERY=60`, `ANALYZE_EVERY=86400`,
`VACUUM_EVERY=604800`. Override in `compose.yml` if a different
rhythm suits your workload. The webmentions / ActivityPub
cadences only do work when there are queued rows; a site that
hasn't enabled either plugin pays nothing per tick.

## Project layout

```
bragi/
├── src/bragi/
│   ├── api.py                  # public plugin API
│   ├── hookspecs.py            # internal hookspec definitions
│   ├── plugins.py              # PluginManager + entry-point discovery
│   ├── settings.py             # Pydantic Settings
│   ├── cli.py                  # `cms` top-level click group
│   ├── apps/
│   │   ├── admin.py            # create_admin_app
│   │   └── delivery.py         # create_delivery_app
│   ├── core/                   # shared, non-plugin code
│   │   ├── models/             # SQLAlchemy models (single source of truth)
│   │   ├── middleware/         # site_resolver, csrf, sessions, redirects
│   │   ├── render/             # markdown + transform registries
│   │   ├── audit.py            # AuditLog writer
│   │   ├── cache.py            # Cache-Control / ETag / 304 helpers
│   │   ├── db.py               # SessionLocal
│   │   ├── htmx.py             # HX-Request dispatch helpers
│   │   ├── permissions.py      # per-site role enforcement
│   │   ├── registry.py         # in-process Registry (content types, importers, nav, ...)
│   │   ├── security.py         # current_user / is_superuser
│   │   ├── seo.py              # title/meta/canonical/og helpers
│   │   ├── storage.py          # attachment storage backend
│   │   ├── text.py             # slugify
│   │   └── useragent.py        # bot / browser / feed-reader classifier
│   └── contrib/                # built-ins as plugins
│       ├── activitypub/        # one fediverse actor per site (follow / undo / outbox fanout)
│       ├── analytics/          # per-site pageview sink + admin dashboard
│       ├── anchors/            # heading id injection
│       ├── api_tokens/         # personal access tokens + JSON REST surface
│       ├── attachments/        # upload + serve + media library
│       ├── audit/              # audit-log admin
│       ├── auth_github/        # OAuth via Authlib
│       ├── auth_local/         # email + password + must-change rotation
│       ├── embeds/             # external-content embeds (directive + providers + rerender)
│       ├── highlight/          # Pygments html transform
│       ├── import_ghost/       # Ghost JSON importer
│       ├── import_hugo/        # Hugo content-tree importer
│       ├── import_wordpress/   # WordPress WXR XML importer
│       ├── indexnow/           # IndexNow push-crawl on publish/update/delete
│       ├── internal_links/     # [text](post:42) save-time + delivery-time resolver + admin picker
│       ├── markdown_extras/    # bundled markdown-it extensions (footnotes, ...)
│       ├── page/               # nested page content type
│       ├── post/               # post content type + tags + tiptap editor
│       ├── redirects/          # resolve_redirect + admin + slug-change auto-301
│       ├── search/             # SQLite FTS5 over post + page bodies
│       ├── seo/                # sitemap, robots.txt, security.txt, feed.xml
│       ├── sessions/           # session admin (list / revoke)
│       ├── sites/              # Site CRUD admin + alias subcommands
│       ├── team/               # per-site team management (list / grant / revoke)
│       ├── theme_default/      # in-tree default theme (registers slug "default")
│       ├── theme_minimal/      # lean, content-first theme (slug "minimal")
│       ├── theme_serif/        # long-form reading theme (slug "serif")
│       ├── theme_terminal/     # monospace dev-focused theme (slug "terminal")
│       ├── themes/             # file-based theme registry + admin picker
│       └── webmentions/        # indieweb send + receive + admin moderation
├── alembic/                    # migrations
├── docker/                     # admin.Dockerfile, delivery.Dockerfile
├── .github/workflows/          # ci.yml, docker.yml
└── tests/
    ├── unit/                   # pure logic, no DB
    ├── contrib/                # one file per built-in plugin
    ├── core/                   # core middleware / cache / permissions tests
    └── integration/            # full stack lifecycle scenarios
```

## Authoring a third-party theme

A theme is a plain Python package that registers a `ThemeSpec` via
the `register_theme` hook on the `bragi.plugins` entry-point group.
Same surface the in-tree `theme_default` / `theme_minimal` /
`theme_serif` / `theme_terminal` use; nothing internal-only.

**Distribution name.** Follow the `bragi-theme-<slug>` convention
(e.g. `bragi-theme-coral`). It keeps third-party packages
greppable on PyPI and signals theme-package shape without further
inspection. The Python import name is independent (`coral_theme`,
`bragi_theme_coral`, whatever you like); only the distribution
name follows the convention.

**Package layout.**

```
bragi-theme-coral/
├── pyproject.toml
├── README.md
└── coral_theme/
    ├── __init__.py
    ├── plugin.py
    ├── templates/
    │   └── delivery/
    │       └── base.html
    └── static/                # optional
        └── theme.css
```

**`plugin.py` (the whole file).**

```python
from __future__ import annotations

from pathlib import Path

import jinja2

from bragi.api import ThemeSpec, hookimpl


@hookimpl
def register_theme() -> ThemeSpec:
    return ThemeSpec(
        slug="coral",
        display_name="Coral",
        template_loader=jinja2.PackageLoader("coral_theme", "templates"),
        # Drop `static_dir` if your theme inlines its CSS in
        # `delivery/base.html` (the in-tree themes do).
        static_dir=Path(__file__).parent / "static",
    )
```

**`pyproject.toml` entry-point declaration.**

```toml
[project.entry-points."bragi.plugins"]
coral_theme = "coral_theme.plugin"
```

The entry-point name (`coral_theme` above) must be unique across
every plugin installed in the deployment; bragi's runtime fails
loud on collision (#188). Pick a name that includes your slug so
the `cms plugins list` output (#190) reads naturally.

**Required template: `delivery/base.html`.** Bragi resolves
`delivery/base.html` against your theme first (via
`ThemeAwareLoader`) for every Site that selected your slug. Your
template must preserve the block surface every content-type
template extends:

| Block | Purpose |
|---|---|
| `title` | `<title>` content |
| `meta` | description / canonical / robots meta tags |
| `feed_links` | Atom `<link rel="alternate">` |
| `social_meta` | Open Graph + Twitter Card meta (content templates override) |
| `jsonld` | JSON-LD `<script>` (content templates override) |
| `content` | the page body |

Plus the Jinja globals plugins register: `pygments_css_url`,
`webmention_endpoint_url`, etc. Easiest path: copy
`bragi.contrib.theme_default`'s `delivery/base.html` as your
starting scaffold and restyle from there.

**Optional templates: anything under `delivery/`.** A theme that
ships `delivery/post_detail.html` shadows the post plugin's
default, etc. Override only the templates you actually want to
change; the rest fall through to the plugin's own
`templates/delivery/`.

**Static assets.** If `static_dir` is set, the delivery app
serves your files at `/theme/<slug>/static/<path>`. Reference
them from your templates with that URL:

```html
<link rel="stylesheet" href="/theme/coral/static/theme.css">
```

The path is reserved; `bragi.contrib.themes` owns the
blueprint that serves it.

**Automatic light / dark.** The in-tree themes all use the
`@media (prefers-color-scheme: dark)` pattern with CSS custom
properties. Recommended:

```html
<meta name="color-scheme" content="light dark">
<style>
  :root {
    color-scheme: light dark;
    --bg: #ffffff;
    --fg: #222222;
    /* ... */
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d0d0d;
      --fg: #f3f4f6;
      /* ... */
    }
  }
</style>
```

Cribbed verbatim from `bragi.contrib.theme_minimal`; pick the
palette that suits your theme.

**Installing.** Install your package into the same Python
environment as bragi (the `admin` and `delivery` containers, or
`poetry add` in a dev tree):

```sh
pip install bragi-theme-coral
```

Restart both apps; the entry-point group is read at process
boot. Once installed, your slug appears in the admin theme
picker on the site-edit form, and `cms plugins list` reports
your distribution name + version under "origin".

**Activating.** Per-Site selection via the admin site-edit
form, or set `Site.theme = "coral"` in the DB. NULL means "use
the bundled default theme"; an unknown slug falls back to
default with a logged warning rather than 500ing the page.

**Disabling a bundled theme.** Comment its line under
`[project.entry-points."bragi.plugins"]` in bragi's
`pyproject.toml` and rebuild the images; no internal fast path
keeps it around. Same mechanism for any bundled plugin.

## Versioning and releases

The version lives in `pyproject.toml` (`version` field), read at
runtime via `importlib.metadata` and exposed as `bragi.__version__`.

Production images are tagged `bragi-admin:vX.Y.Z` and
`bragi-delivery:vX.Y.Z` on the GitHub Container Registry, built by
the `docker.yml` workflow on git tag push as multi-arch manifest
lists covering `linux/amd64` and `linux/arm64`.

PyPI publication is not on the path (the `bragi` distribution name
is held by an unrelated project); ship is container-only.

## License

MIT. See [LICENSE](LICENSE).
