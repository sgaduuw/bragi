# Changelog

All notable changes to bragi are documented here. Format adapted
from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
