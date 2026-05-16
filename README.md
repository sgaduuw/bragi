# bragi

A multisite CMS built with Python, Flask, and htmx. Markdown source
of truth, plugin-extensible from day one, SEO as a first-class
citizen.

## Status

1.9.0 shipped 2026-05-16. All day-one built-in plugins are in
place: Post, Page, Tag, GitHub OAuth + local-credential auth
(with `must_change` rotation), Hugo / Ghost / WordPress
importers, redirects with prefix / regex matching and slug-change
auto-301, per-site analytics (with UA classification), attachments
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

1.5.0 wrapped the four-phase IA refactor (#77, #78, #79, #80):
admin content URLs moved under `/admin/sites/<slug>/...`, analytics
scoped to the site you've entered, owners get a UI to invite
collaborators. Public delivery URLs are unchanged; plugin hookspecs
are unchanged.

1.6.0 cleans up the production deploy posture: containers run
gunicorn against the WSGI factory (sync workers, access log on
stdout) instead of Werkzeug's dev server. Worker counts default
to 2 / 4 (admin / delivery), tunable via `ADMIN_WORKERS` /
`DELIVERY_WORKERS`. No code, schema, or interface changes; the
old image silently ran the dev server, the new image doesn't.

1.6.1 fixes a Ghost-importer detection regression on Ghost 6.x
exports (#95): the earlier head-scan heuristic looked for
`"posts"` in the first 4 KB, but modern exports lead `db[0].data`
with `benefits` / `custom_theme_settings`, pushing `posts` past
the cutoff and causing valid exports to be rejected. Detection
now does a full parse; no schema or interface change.

1.7.0 reorders the admin post list by `COALESCE(published_at,
updated_at) DESC` instead of `created_at DESC`. Published posts
sort by publication date, drafts by last edit, and imported posts
land in their original Ghost / WordPress / Hugo publish order
rather than reflecting the importer's iteration order. Editing
an old draft also bubbles it back up. No schema or hookspec
change; admin URLs and delivery output are unchanged.

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

Releases follow git-flow with `develop` as the default branch.
Container images ship to GHCR as `bragi-admin:vX.Y.Z` and
`bragi-delivery:vX.Y.Z` on every tag push.

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
  editor-independent.
- **Plugin-extensible from day one.** Built-ins (Post, Page,
  redirects, importers, analytics, ...) register through the
  `bragi.plugins` entry-point group, the same path third parties
  use. No internal fast path.
- **SEO as a first-class citizen.** Per-page title / meta / OG /
  canonical / JSON-LD editable in admin. Per-site `sitemap.xml`,
  `robots.txt`, `security.txt`. Server-side Pygments highlighting
  for code blocks (Ansible / Python / Terraform lexers in core).
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
  301 Redirect from the legacy URL to the post's bragi
  canonical (`/posts/<slug>/`). `tags:` lists upsert by slug.
  CLI: `cms import hugo --site <slug> [--author <email>]
  [--dry-run] <path>`.
- **Ghost**: parses the single-file JSON export
  (`db[0].data.posts`). Bodies arrive as HTML and convert to
  markdown via `markdownify(heading_style="ATX")`; tags come
  from `data.tags` + `data.posts_tags`; authors match existing
  Users by email (else fall back to the first user). For every
  published post a 301 from Ghost's permalink (`/<slug>/`) to
  bragi's (`/posts/<slug>/`) lands so legacy bookmarks survive.
  CLI: `cms import ghost --site <slug> [--author <email>]
  [--dry-run] <path>`.
- **WordPress**: parses WXR (WordPress eXtended RSS) XML
  exports. `wp:post_type=post` rows become Posts, `page` rows
  become Pages; bodies are converted from WordPress HTML to
  markdown and run through the same pipeline. Categories and
  tags upsert by slug; authors match by email or fall back to
  the first user. Permalinks captured at export time become 301
  redirects so legacy bookmarks survive. Idempotency keys on
  `(site_id, source_id)` via `wp:post_id`. CLI:
  `cms import wordpress --site <slug> [--author <email>]
  [--dry-run] <wxr.xml>`.

Notion, Substack, and Medium importers are deferred to
follow-up packages; no v1.x commitment.

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
BRAGI_TAG=v1.9.0 BRAGI_SECRET_KEY="$(openssl rand -hex 32)" docker compose up -d
```

A `bragi-tasks` sidecar owns `alembic upgrade head` on start
(touching `/data/.migrated` once the schema is current), then
enters a sleeper loop that dispatches periodic CMS commands:
`scheduled-publish` (flips drafts whose `scheduled_for` has
elapsed), `db analyze` (daily), and `db vacuum` (weekly). The
admin and delivery services gate their start on the sidecar's
healthcheck, so a fresh deploy and a schema-bump deploy work
the same way. The shared `bragi-data` volume backs `/data/bragi.db`,
`/data/uploads/` (attachments), and the `/data/.migrated` sentinel;
back it up. Ports bind to `127.0.0.1` only; front the apps with
a reverse proxy (Caddy / nginx / Traefik) for TLS and hostname
routing.

Both apps run under gunicorn inside the container (sync worker
class; `--access-logfile -` to stdout). Worker counts default to
2 for admin and 4 for delivery; tune via `ADMIN_WORKERS` /
`DELIVERY_WORKERS` env vars on each service if your traffic
shape needs it.

Task-runner cadences (all in seconds, set on the `bragi-tasks`
service) default to `SCHEDULED_PUBLISH_EVERY=60`,
`ANALYZE_EVERY=86400`, `VACUUM_EVERY=604800`. Override in
`compose.yml` if a different rhythm suits your workload.

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
│       ├── analytics/          # per-site pageview sink + admin dashboard
│       ├── anchors/            # heading id injection
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
│       ├── page/               # nested page content type
│       ├── post/               # post content type + tags + tiptap editor
│       ├── redirects/          # resolve_redirect + admin + slug-change auto-301
│       ├── search/             # SQLite FTS5 contentless index over post bodies
│       ├── seo/                # sitemap, robots.txt, security.txt, feed.xml
│       ├── sessions/           # session admin (list / revoke)
│       ├── sites/              # Site CRUD admin + alias subcommands
│       ├── team/               # per-site team management (list / grant / revoke)
│       ├── theme_default/      # in-tree default theme (registers slug "default")
│       └── themes/             # file-based theme registry + admin picker
├── alembic/                    # migrations
├── docker/                     # admin.Dockerfile, delivery.Dockerfile
├── .github/workflows/          # ci.yml, docker.yml
└── tests/
    ├── unit/                   # pure logic, no DB
    ├── contrib/                # one file per built-in plugin
    ├── core/                   # core middleware / cache / permissions tests
    └── integration/            # full stack lifecycle scenarios
```

## Versioning and releases

The version lives in `pyproject.toml` (`version` field), read at
runtime via `importlib.metadata` and exposed as `bragi.__version__`.

Production images are tagged `bragi-admin:vX.Y.Z` and
`bragi-delivery:vX.Y.Z` on the GitHub Container Registry, built by
the `docker.yml` workflow on git tag push.

PyPI publication is not on the path (the `bragi` distribution name
is held by an unrelated project); ship is container-only.

## License

MIT. See [LICENSE](LICENSE).
