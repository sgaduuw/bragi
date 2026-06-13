# bragi

A multisite CMS built with Python, Flask, and htmx. Markdown source
of truth, plugin-extensible from day one, SEO as a first-class
citizen.

## Status

**Latest release:** 1.33.0 (2026-06-13).

**Functional surface today:** multisite CMS with markdown source-
of-truth, TipTap editor (with image size / alignment classes and
a bubble menu for inline picks), structured CV / resume page kind
(`PageKind.RESUME` with typed sections, Project↔Position linkage,
schema.org microdata, print-friendly), auto-derived public
navigation from the page tree (with per-page `show_in_nav` and
`menu_order` controls), GitHub OAuth + local
bootstrap, redirects as a first-class subsystem, importers for
Hugo / Ghost / WordPress / LinkedIn, attachments + media library with
theme-aware multi-format multi-width renditions (`<picture>` with
AVIF / WebP / fallback tiers and per-class `sizes`), Unsplash
plugin (search + insert from the admin image picker, photographer
credit auto-renders beneath inline-body images; requires
`BRAGI_UNSPLASH_ACCESS_KEY`), pinned posts on the landing page,
ActivityPub + webmentions, datasets (per-site DuckDB-backed data
file registry with an explore console, saved queries, and a
`::: dataset :::` markdown directive), page-admin slug recompute
from title (edit-form, page-list inline, and bulk) with a
full-URL-path column in the page list, four in-tree themes with
auto light/dark, sitemap / feed / JSON-LD, audit-driven hardening
from v1.12 through v1.19.

See [CHANGELOG.md](CHANGELOG.md) for per-release detail.

Releases follow git-flow with `develop` as the default branch.
Container images ship to GHCR as `bragi-admin:vX.Y.Z` and
`bragi-delivery:vX.Y.Z` on every GitHub Release, as multi-arch
manifest lists covering `linux/amd64` and `linux/arm64`. The
`release.yml` workflow publishes the PyPI wheel first, then builds
images that consume it via `pip install bragi-cms==X.Y.Z`. `docker
pull` resolves the right variant for the host architecture
automatically; Apple Silicon laptops, Ampere / Graviton servers,
and ARM homelabs run natively rather than through QEMU emulation.

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
  Per-site count override via `/admin/sites/<slug>/settings/`
  (`related_posts_count`, default 3); zero-tag posts render no aside.
- **Chronological archive.** `<post_index>/archive/` lists years
  with counts (newest first); drilling in shows months for that
  year, then posts in that month (oldest first, journal-style).
  Drafts are excluded; out-of-range or empty buckets 404. Each
  level carries the standard `ETag` + `Last-Modified` validators
  so feed readers and crawlers get cheap 304s.
- **Auto-navigation from the page tree.** Published pages
  appear in the public-site header nav by default, ordered by
  a per-page `menu_order`. Direct children become a native
  `<details>` submenu. The per-page checkbox opts an
  individual page out; the page mapped to the site's home is
  auto-hidden so the brand link is not duplicated.
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
  bootstrapped with `bragi indexnow setup --site <slug>`; the
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
  cron-driven `bragi webmentions send-pending` performs W3C
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
  to every follower; `bragi activitypub send-pending` ships
  the queued deliveries. Per-site keypair generated on first
  `/actor` hit or via `bragi activitypub keygen --site <slug>`.

## What bragi is not

- Not multilingual at the post level. Each `Site` has one `locale`;
  per-post translations are not supported.
- Not a SaaS / multi-tenant cloud product. Single-operator, with
  multiple sites under that operator.
- Not a block-tree editor. Markdown is the source of truth.
- Not a real-time / collaborative editor.

## Stack

- Python 3.14+
- Flask 3.x
- SQLAlchemy 2.0 + alembic
- Pydantic Settings
- pluggy (plugin framework)
- Authlib (GitHub OAuth + future OIDC providers)
- markdown-it-py + Pygments
- htmx (delivery side) + TipTap (admin editor)
- SQLite (WAL) primary store; DuckDB for the datasets plugin
- gunicorn (production WSGI server, sync worker class)

## Importers

All four ship in 1.x and are idempotent via `Post.source_id`,
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
  slug. CLI: `bragi import hugo --site <slug> [--author <email>] [--dry-run] <path>`.
- **Ghost**: parses the single-file JSON export
  (`db[0].data.posts`). Posts and pages both land: posts become
  bragi Posts; pages become bragi `STATIC` pages with slug,
  title, body, and `meta_title` preserved. Bodies arrive as HTML
  and convert to markdown via `markdownify(heading_style="ATX")`; tags
  come from `data.tags` + `data.posts_tags`; authors match existing
  Users by email (else fall back to the first user). For every
  published post a 301 lands from Ghost's permalink (`/<slug>/`)
  to bragi's canonical under the site's `post_index` page (e.g.
  `/blog/<slug>/`) so legacy bookmarks survive. Featured images
  (`feature_image`, or `og_image` as fallback) are downloaded as
  bragi `Attachment` rows; `feature_image_alt` becomes the alt
  text. Additional fields picked up: `featured` sets `is_pinned`
  on posts. Failed image downloads warn and continue without the
  image. `__GHOST_URL__` placeholders in post / page bodies,
  excerpts, and meta descriptions are stripped to site-relative
  URLs. The same placeholder in `feature_image`, `og_image`, and
  `canonical_url` is substituted with the Ghost site URL (derived
  from the first non-empty `posts[*].url` in the export). CLI:

  ```sh
  # Common case: auto-detects the Ghost site URL from posts[*].url.
  bragi import ghost --site <slug> [--author <email>] [--dry-run] <path>

  # When the export lacks post URLs (rare on older exports) or you
  # want the substituted base URL to point at a different host:
  bragi import ghost --site <slug> --ghost-base-url https://oldsite.ghost.io \
      [--author <email>] [--dry-run] <path>
  ```
- **WordPress**: parses WXR (WordPress eXtended RSS) XML
  exports. `wp:post_type=post` rows become Posts, `page` rows
  become Pages; bodies are converted from WordPress HTML to
  markdown and run through the same pipeline. Categories and
  tags upsert by slug; authors match by email or fall back to
  the first user. Permalinks captured at export time become 301
  redirects to the bragi canonical (posts resolve through the
  site's `post_index` page; pages resolve through the static-page
  chain). Idempotency keys on `(site_id, source_id)` via
  `wp:post_id`. CLI: `bragi import wordpress --site <slug> [--author <email>] [--dry-run] <wxr.xml>`.
- **LinkedIn** (`bragi.contrib.import_linkedin`). Reads the
  seven resume-relevant CSVs in LinkedIn's "Download your data"
  export ZIP and populates a Resume page's `resume_data` via a
  two-phase plan-review-apply flow. The plan emits one
  `ChangeProposal` per concrete diff (add / update / remove);
  the operator approves a subset by editing the JSON plan file
  or by checking boxes on the admin review page. Re-imports
  preserve operator-authored narrative fields
  (`description_markdown`, `impacts`, `body_markdown`,
  `header.profile_links`, `highlights`) across matched rows;
  position-matching uses `(company, role, start_date)` so
  renamed titles surface as a remove+add pair the operator can
  spot and reject. CLI:
  `bragi import linkedin <zip> --site <slug> [--page-slug cv] [--plan-out PATH]`
  to plan;
  `bragi import linkedin --apply <plan.json>`
  to apply the filtered subset. Admin UI: upload widget on
  every resume page edit form, with a review page rendered
  after the upload.

Notion, Substack, and Medium importers are deferred to
follow-up packages; no v1.x commitment.

The admin now carries a site-scoped Import page at
`/admin/sites/<slug>/import/` that lists every importer wired
up with an admin form. Ghost is the first wired importer there,
with a plan-then-apply browser flow (upload → review → apply or
cancel) that mirrors LinkedIn's. The CLI invocations above
continue to work; the admin route is an alternative surface for
operators who prefer the browser. Hugo and WordPress remain
CLI-only for now; they'll grow admin tiles in follow-up PRs via
the new `register_importer_admin_tile` hookspec.

## Export (portability)

`bragi export [--site <slug>] [--output <dir>]`
writes a Hugo-shaped tree per site: posts as
`content/posts/<slug>.md` with YAML frontmatter, pages under
`content/pages/`, attachment bytes under `static/attachments/`
alongside an `attachments.csv` metadata manifest, and the
per-site redirect table as `redirects.csv`. Default output is
`bragi-export-YYYYMMDD-HHMMSS/` in the CWD.

Output is deterministic: re-running against an unchanged DB
yields byte-identical files, so a periodic `bragi export` doubles
as a diffable snapshot. Posts round-trip through `bragi import
hugo`: importing the export and re-exporting changes nothing
beyond timestamps, so the corpus is portable back into any Hugo
build at any time.

## Datasets

Datasets is a per-site registry of uploaded data files. Supported
formats: native DuckDB databases (`.duckdb`), CSV, Parquet, and
SQLite. Every file is queried via DuckDB at render time, so a
single DuckDB instance can open CSV or Parquet files transparently.

**Admin explore console.** Site owners and site-level admins
reach a DuckDB console at
`/admin/sites/<slug>/datasets/<dataset-slug>/explore` to run
ad-hoc SQL against the file, inspect column types, and save named
queries. Access is author-only; the explore console is not exposed
on the delivery app.

**Saved queries.** Named SQL strings stored alongside the dataset.
A saved query is the required backing for a `format=chart`
directive (the Vega-Lite spec lives on the saved query; the render
pipeline executes the SQL, maps the result set onto the spec, and
bakes the chart HTML at save time).

**`::: dataset :::` directive.** Embeds dataset output into
post and page bodies at save time:

```
# Inline SQL, explicit table output:
::: dataset slug=revenue sql="SELECT year, total FROM summary ORDER BY year" format=table
:::

# Saved query, Vega-Lite chart (requires the query to carry a spec):
::: dataset slug=revenue q=annual_chart format=chart
:::

# Saved query, inline scalar:
::: dataset slug=revenue q=latest_mrr format=scalar
:::
```

The closing `:::` must sit on its own line. `format` is `table`,
`chart`, or `scalar`. For a saved query (`q=<name>`) `format`
defaults to `table`; inline SQL (`sql="..."`) must name an explicit
`format=` and cannot use `format=chart`. Chart output renders
client-side via CDN-loaded `vega-embed`; a `<noscript>` block
with the same data as an HTML table is baked alongside so
JS-disabled browsers and crawlers see the data.

**Refresh model.** Dataset output is baked into `body_html` at
post/page save time (same pipeline as embeds). Re-uploading a
dataset file re-bakes every referencing post and page
synchronously before the upload response returns.
`bragi datasets rerender <site-id> [<dataset-slug>]` is the
manual handle for out-of-band refreshes or recovery:

```sh
bragi datasets rerender 1              # re-bake all datasets for site 1
bragi datasets rerender 1 revenue      # re-bake only the "revenue" dataset
bragi datasets rerender 1 --dry-run    # report without writing
```

## Backups

`bragi backup [--output PATH]`
writes a single `.tar.gz` containing a consistent SQLite snapshot
(produced with `VACUUM INTO`, so no companion `-wal` / `-shm`
files) plus the contents of `Settings.attachments_root` as
`attachments/`. Default output: `bragi-backup-YYYYMMDD-HHMMSS.tar.gz`
in the current working directory.

To restore: extract the tarball, drop `bragi.db` and
`attachments/` into a fresh deployment (matching paths), and
restart the admin + delivery processes. There is no `restore`
subcommand by design; a tool that overwrites a live deployment
is a big risk for not much help.

`bragi backup` is SQLite-only and exits 2 with a clear message
under a non-SQLite `BRAGI_DATABASE_URL` (its `VACUUM INTO` is
SQLite-specific). Postgres operators: use `pg_dump` for the
DB half and a separate tar of `attachments_root` for the file
half. `bragi db vacuum` follows the same gate (`PRAGMA
wal_checkpoint(TRUNCATE)` is SQLite-only); on Postgres use
`VACUUM (FULL)` or your usual autovacuum tooling instead.

## Quick start (development)

```sh
uv sync
uv run alembic upgrade head
make dev    # runs bragi-admin on :8001 and bragi-delivery on :8002 via the in-repo Procfile supervisor
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
BRAGI_TAG=v1.33.0 BRAGI_SECRET_KEY="$(openssl rand -hex 32)" docker compose up -d
```

A `bragi-tasks` sidecar owns `bragi db upgrade` on start
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
sidecar retries `bragi db upgrade` with backoff
(`ALEMBIC_MAX_ATTEMPTS=5`, `ALEMBIC_RETRY_DELAY=15`, in seconds) and exits
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

`BRAGI_UNSPLASH_ACCESS_KEY` (optional) enables the Unsplash
plugin: authors search Unsplash from inside the admin
attachments picker (and the TipTap "Insert image" button, which
opens the same picker), pick a photo, and the plugin downloads
it into bragi's storage as a regular attachment with the
photographer's name and profile URL stored alongside. On the
public page the credit auto-renders beneath inline-body
Unsplash images. Set the key on the `admin` service only; the
`delivery` service doesn't talk to Unsplash. Get a key from
<https://unsplash.com/developers>. Leave unset to disable the
plugin (the Unsplash tab stays hidden). `BRAGI_UNSPLASH_APP_NAME`
(default `bragi`) controls the `utm_source` tag on credit links;
if you customise it, set it on both admin and delivery for
consistency.

`BRAGI_DATASET_MAX_UPLOAD_BYTES` (default 100 MiB),
`BRAGI_DATASET_QUERY_TIMEOUT_SECONDS` (default 10.0),
`BRAGI_DATASET_QUERY_MAX_ROWS` (default 1000),
`BRAGI_DATASET_QUERY_MEMORY_LIMIT` (default `512MB`), and
`BRAGI_DATASET_QUERY_TEMP_LIMIT` (default `1GB`) tune the
datasets plugin. Upload size, DuckDB query timeout, per-query row
cap, DuckDB memory ceiling, and the on-disk temp-spill bound
respectively. The memory ceiling is a soft cap: once exceeded
DuckDB spills intermediate data to a temp directory, and the
temp-spill bound is what stops a heavy query from filling the disk
(it errors instead). Set on the `admin` service; the delivery app
inherits the row, memory, and temp caps for render-time queries.
Leave at defaults unless a specific file size or query workload
demands otherwise.

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
│   ├── cli.py                  # `bragi` top-level click group
│   ├── apps/
│   │   ├── admin.py            # create_admin_app
│   │   └── delivery.py         # create_delivery_app
│   ├── core/                   # shared, non-plugin code
│   │   ├── models/             # SQLAlchemy models (single source of truth)
│   │   ├── middleware/         # site_resolver, csrf, sessions, redirects
│   │   ├── render/             # markdown + transform registries
│   │   ├── admin_notices.py    # cross-plugin admin notice writer
│   │   ├── audit.py            # AuditLog writer
│   │   ├── breadcrumbs.py      # Crumb dataclass + set_breadcrumbs helper (admin nav)
│   │   ├── bulk_action.py      # bulk-action helpers (shared by post / page / attachments)
│   │   ├── cache.py            # Cache-Control / ETag / 304 helpers
│   │   ├── db.py               # SessionLocal
│   │   ├── export.py           # corpus export writer (bragi export)
│   │   ├── feed.py             # Atom feed builder
│   │   ├── healthz.py          # /healthz handler
│   │   ├── htmx.py             # HX-Request dispatch helpers
│   │   ├── http.py             # hardened outbound fetcher (safe_get / safe_post)
│   │   ├── image_processor.py  # image transform helpers
│   │   ├── permissions.py      # per-site role enforcement
│   │   ├── registry.py         # in-process Registry (content types, importers, nav, ...)
│   │   ├── renditions.py       # attachment rendition pipeline
│   │   ├── safe_urls.py        # safe_external_url + IDN gate
│   │   ├── security.py         # current_user / is_superuser
│   │   ├── seo.py              # title/meta/canonical/og helpers
│   │   ├── storage.py          # attachment storage backend
│   │   ├── text.py             # slugify + unique-slug helpers (post/page collision-aware)
│   │   ├── themes.py           # ThemeAwareLoader + theme registry
│   │   ├── time.py             # aware_utcnow
│   │   ├── url.py              # URL helpers
│   │   └── useragent.py        # bot / browser / feed-reader classifier
│   ├── alembic/                # migrations bundled in wheel (bragi:alembic)
│   └── contrib/                # built-ins as plugins
│       ├── activitypub/        # one fediverse actor per site (follow / undo / outbox fanout)
│       ├── admin_imports/      # site-scoped admin importer index (tile aggregator)
│       ├── admin_notices/      # cross-plugin admin notice surface (toast / banner)
│       ├── analytics/          # per-site pageview sink + admin dashboard
│       ├── anchors/            # heading id injection
│       ├── api_tokens/         # personal access tokens + JSON REST surface
│       ├── attachments/        # upload + serve + media library
│       ├── audit/              # audit-log admin
│       ├── auth_github/        # OAuth via Authlib
│       ├── auth_local/         # email + password + must-change rotation
│       ├── datasets/           # per-site data file registry + explore console + directive
│       ├── embeds/             # external-content embeds (directive + providers + rerender)
│       ├── highlight/          # Pygments html transform
│       ├── import_ghost/       # Ghost JSON / ZIP importer (posts, pages, featured images)
│       ├── import_hugo/        # Hugo content-tree importer
│       ├── import_linkedin/    # LinkedIn data export (ZIP) importer for resume pages
│       ├── import_wordpress/   # WordPress WXR XML importer
│       ├── indexnow/           # IndexNow push-crawl on publish/update/delete
│       ├── internal_links/     # [text](post:42) save-time + delivery-time resolver + admin picker
│       ├── markdown_extras/    # bundled markdown-it extensions (footnotes, ...)
│       ├── nav/                # auto-derived public site navigation from the page tree
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
│       ├── unsplash/           # Unsplash image picker + photographer-credit render
│       └── webmentions/        # indieweb send + receive + admin moderation
├── alembic/                    # alembic.ini + dev shim (script_location = bragi:alembic)
├── docker/                     # admin.Dockerfile, delivery.Dockerfile
├── .github/workflows/          # ci.yml, release.yml, dispatch-theme-released.yml, _diagnose-pypi.yml
└── tests/
    ├── unit/                   # pure logic, no DB
    ├── contrib/                # one file per built-in plugin
    ├── core/                   # core middleware / cache / permissions tests
    ├── integration/            # full stack lifecycle scenarios
    └── test_*.py               # cross-cutting smoke (CLI, hookspecs, plugin set)
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
the `bragi plugins list` output (#190) reads naturally.

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
`uv add` in a dev tree):

```sh
pip install bragi-theme-coral
```

Restart both apps; the entry-point group is read at process
boot. Once installed, your slug appears in the admin theme
picker on the site-edit form, and `bragi plugins list` reports
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
the `publish-docker` job in `release.yml` on every GitHub Release
as multi-arch manifest lists covering `linux/amd64` and
`linux/arm64`. The job runs sequentially after `publish-pypi` and
consumes the freshly-published wheel via `pip install
bragi-cms==X.Y.Z`.

From v1.27.0, bragi is also published to PyPI as `bragi-cms` (the
`bragi` name is held by The Managarm Project's IDL):

```sh
pip install bragi-cms==1.33.0
```

The import path stays `import bragi`. Container images remain the
primary deploy artefact for operators who want pre-built images.

## License

MIT. See [LICENSE](LICENSE).
