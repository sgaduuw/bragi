# bragi

A multisite CMS built with Python, Flask, and htmx. Markdown source
of truth, plugin-extensible from day one, SEO as a first-class
citizen.

## Status

Pre-1.0.0, under active development. The scaffolding is in place;
the v1 built-in plugin set (Post, Page, Tags, GitHub auth, local
bootstrap, Hugo / Ghost importers, redirects, analytics, Pygments
code highlighting, sitemap / robots / security.txt, TipTap editor)
is being built out as separate packages under `bragi.contrib.*`.

Releases follow git-flow with `develop` as the default branch.
v1.0.0 will tag the first feature-complete cut.

## What bragi is

- **Multisite by design.** One database serves many sites; the Host
  header at the WSGI edge resolves to a Site row. Every content
  table has a `site_id` FK.
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

## Importers

v1 ships importers for:

- **Hugo**: walks `content/`, parses TOML / YAML / JSON
  frontmatter, translates common shortcodes
  (`{{< figure >}}`, `{{< highlight >}}`, `{{< youtube >}}`) to
  directive syntax, registers aliases as redirect rows.
- **Ghost**: JSON export. Older posts (`markdown` field) import
  directly; Mobiledoc / Lexical bodies pass through Ghost's HTML
  renderer and convert to markdown via `markdownify`.

WordPress (WXR XML) is the v1.1 priority. Notion / Substack /
Medium are opportunistic.

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

## Project layout

```
bragi/
├── src/bragi/
│   ├── api.py                  # public plugin API
│   ├── hookspecs.py            # internal hookspec definitions
│   ├── plugins.py              # PluginManager + entry-point discovery
│   ├── settings.py             # Pydantic Settings
│   ├── apps/
│   │   ├── admin.py            # create_admin_app
│   │   └── delivery.py         # create_delivery_app
│   ├── core/                   # shared, non-plugin code
│   │   ├── models/             # SQLAlchemy models (single source of truth)
│   │   ├── middleware/         # site resolver, auth, redirects, analytics
│   │   ├── render/             # markdown + transform registries
│   │   ├── auth/               # service, passwords, permissions
│   │   ├── content/            # registry, publishing, slug
│   │   ├── htmx.py             # HX-Request dispatch helpers
│   │   └── seo.py              # title/meta/canonical/og + JSON-LD
│   └── contrib/                # built-ins as plugins
│       ├── post/
│       ├── page/
│       ├── tags/
│       ├── auth_github/
│       ├── auth_local/
│       ├── redirects/
│       ├── import_hugo/
│       ├── import_ghost/
│       ├── analytics/
│       ├── code_highlight/
│       ├── seo/                # sitemap, robots.txt, security.txt
│       └── editor_tiptap/      # admin editor frontend
├── alembic/                    # migrations
├── docker/                     # admin.Dockerfile, delivery.Dockerfile
├── .github/workflows/          # ci.yml, docker.yml
└── tests/
    ├── unit/                   # pure logic, no DB
    ├── contrib/                # one file per built-in plugin
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
