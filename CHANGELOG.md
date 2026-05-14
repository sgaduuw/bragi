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
