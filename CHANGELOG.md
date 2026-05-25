# Changelog

All notable changes to bragi are documented here. Format adapted
from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Pinned posts on the index landing page.** Editor-chosen
  posts surface in a CSS scroll-snap carousel above the
  chronological recency list. Schema: `posts.is_pinned`
  (boolean) + optional `posts.pinned_until` (auto-clear
  datetime). Admin edit form gains a checkbox + datetime
  input near the status select (visible only when status is
  `published`); the post list gets a per-row Pin/Unpin button
  backed by an htmx-friendly endpoint. Pinned posts are
  removed from the page-1 recency list and reappear in their
  natural date position on page 2+. Single-pin renders as a
  plain card with no carousel chrome. Closes #125.

### Changed
- **`bragi.core.db.SessionLocal` is now a lazy proxy.** Previously
  it was a `sessionmaker` bound directly at import time, which
  meant every `from bragi.core.db import SessionLocal` captured
  the binding and tests had to enumerate every importer in a
  56-entry `_SESSION_LOCAL_IMPORTERS` list to monkey-patch them
  all (drifted silently when new modules added the import,
  masked by the local `bragi.db` dev file but red in CI). The
  proxy is a singleton callable that delegates to a swappable
  `_factory` field; `with SessionLocal() as db:` keeps its
  exact semantics. The conftest fixture now patches one place,
  and the `__all__ = ["SessionLocal", "bp"]` re-export in
  `bragi/contrib/post/delivery.py` (which existed only to
  satisfy 19 stale test patches) is gone; those tests now
  patch the canonical `bragi.core.db.SessionLocal._factory`
  path.
- **CI actions bumped to latest majors.** The v1.14.0 docker
  build surfaced GitHub's Node.js 20 deprecation warning on
  every `actions/*` and `docker/*` action used in the workflows
  (forced to Node.js 24 default starting 2026-06-02; Node.js 20
  removed from runners 2026-09-16). Both `ci.yml` and
  `docker.yml` now pin the current major of each action:
  `actions/checkout@v6`, `actions/setup-python@v6`,
  `docker/setup-qemu-action@v4`, `docker/setup-buildx-action@v4`,
  `docker/login-action@v4`, `docker/metadata-action@v6`,
  `docker/build-push-action@v7`. Input shapes are unchanged
  across all bumps (verified by re-running CI on the bumps PR);
  the only runtime delta is that each action now runs under
  Node.js 24, which the runners ship by default.

## [1.14.1] - 2026-05-20

### Fixed
- **Theme switching now actually changes the rendered output.**
  v1.14.0 shipped four in-tree themes and the per-Site picker on
  the site-edit form, but switching the picker had no visible
  effect: Jinja's `Environment` keeps a process-wide
  compiled-template cache keyed on template name, so the first
  request that resolved `delivery/base.html` cached the active
  theme's compile and every later request from a different site
  (with a different theme) reused that cached compile without
  consulting `ThemeAwareLoader` again. Fix: `ThemeAwareLoader`'s
  `get_source` now returns an `uptodate` callable that returns
  False whenever the request's active theme differs from the one
  that loaded the cached source; the delivery app factory pairs
  this with `app.jinja_env.auto_reload = True` so Jinja honors
  `uptodate` on every render. Same-theme repeats still hit the
  cache (uptodate returns True, no reparse); only the
  cross-theme case forces a recompile. The fallback path is
  wrapped too so a later theme registration or a site switching
  from "no theme" to a theme also invalidates the cache. Catch:
  the existing test fixtures create a fresh `create_delivery_app()`
  per test, so the cache started empty in every case and the
  bug never surfaced under CI; a new test now drives two
  back-to-back requests with different themes through the same
  app to pin the invariant.

## [1.14.0] - 2026-05-19

### Added
- **Multi-arch container images (`linux/amd64` +
  `linux/arm64`) (#167).** `.github/workflows/docker.yml` now
  registers QEMU binfmt handlers and passes `platforms:
  linux/amd64,linux/arm64` to `docker/build-push-action`, so
  every `v*.*.*` tag push produces a multi-arch manifest list
  for both `bragi-admin` and `bragi-delivery`. `docker pull`
  resolves the right variant for the host architecture
  automatically. Apple Silicon laptops, Ampere / Graviton
  servers, and ARM homelabs (Raspberry Pi, ...) now run native
  rather than through QEMU emulation. Both Dockerfiles are
  arch-agnostic by construction (`python:3.12-slim` is itself a
  multi-arch manifest list; every runtime dep ships arm64
  wheels), so no Dockerfile change was needed. Build time on
  tag push roughly doubles (arm64 builds run under QEMU
  emulation on the amd64 GHA runner); release is not a
  hot-path workflow, so the cost is paid once per tag rather
  than on every PR.
- **Three new in-tree themes plus automatic light / dark on
  every theme (#126).** `bragi.contrib.theme_minimal` (clean
  sans-serif, narrow column), `bragi.contrib.theme_serif`
  (long-form reading, serif body, paper-tone backgrounds), and
  `bragi.contrib.theme_terminal` (all-monospace, Solarized
  palette, bracket-delimited section markers) all ship under
  the `bragi.plugins` entry-point group with slugs `"minimal"`,
  `"serif"`, and `"terminal"`. Each theme's `delivery/base.html`
  carries a `<meta name="color-scheme" content="light dark">`
  hint plus a `@media (prefers-color-scheme: dark)` block, so
  every shipped theme follows the visitor's OS preference
  automatically. `theme_default` gained the same light / dark
  treatment in lockstep so the contract is uniform across the
  whole in-tree set. The admin theme picker now lists four
  options instead of one; `Site.theme = "minimal"` (etc.)
  selects them with no schema change. Backed by a parametrized
  test catalog that asserts each theme registers with the
  expected slug + label, ships a resolvable `delivery/base.html`
  with the `content` block intact, includes the dark-mode CSS,
  and uses a `PackageLoader` (not a `DictLoader` or filesystem
  path) so the templates ride inside the wheel. README gains a
  new "Authoring a third-party theme" section covering the
  `bragi-theme-<slug>` distribution-name convention, the
  package layout, the `register_theme` hookimpl pattern, the
  `delivery/base.html` block surface a theme must preserve, the
  `/theme/<slug>/static/<path>` static-file URL space, the
  recommended `prefers-color-scheme: dark` pattern with CSS
  custom properties, and the install / activate / disable
  cycle. Same hook surface the in-tree themes use; no
  internal-only fast path.
- **Plugin-set boot smoke test (#169).** New
  `tests/integration/test_plugin_set_smoke.py` asserts that
  `create_admin_app()` and `create_delivery_app()` complete
  without exception under the real `bragi.plugins` entry-point
  manifest, that every declared entry-point name in
  `pyproject.toml` is present in the running `PluginManager`
  (silently-dropped entry-points fail loud), that every loaded
  plugin contributes at least one hookimpl (catches a plugin
  loaded with a stale `@hookimpl` marker or empty body), and
  that back-to-back factory calls both succeed (regression-pins
  the per-app `Registry` invariant against a future
  module-level state refactor that would trip #188's
  duplicate-registration guard). Pluggy load order across
  `entry_points` discovery isn't deterministic; the other
  integration tests touch specific flows but none assert the
  whole manifest boots in isolation. Filed deliberately as
  deferred during the v1.11.0 audit (pass 2).

### Changed
- **Sitemap builder prewarms the page-URL identity map (#172).**
  `bragi.core.url._resolve_segments` walks a Page's parent chain
  one `db.get(Page, parent_id)` per ancestor depth, fine for the
  typical 1-3 CMS depth but pathological in a batch: a sitemap of
  K pages whose deepest chain is D would have issued `K * D`
  per-row queries. The pre-v1.11.0 audit (pass 2) flagged this
  as latent. Fix: new
  `bragi.core.url.prewarm_page_url_cache(db, site_id)` bulk-loads
  every Page on the site into the session's identity map; the
  sitemap builder calls it once before iterating, so every
  `_resolve_segments` call's ancestor walk hits in-memory rather
  than the DB. Net cost drops from `K * D` queries to one query
  for the whole sitemap. Rows are stashed on `db.info` so
  SQLAlchemy's weak-referenced identity map can't drop them
  mid-loop. Single-page callers (admin edit, slug-change
  auto-301) are unchanged on purpose; loading every site page to
  build one URL would be a regression. Recursive-CTE and
  denormalised-slug-path alternatives discussed in the issue
  rejected: identity-map prewarm is strictly smaller and
  identical in steady-state cost.
- **Registry fails loud on duplicate plugin registrations (#188).**
  Each `Registry.add_*` method now dedups on the canonical unique
  field (`.name` for content types / importers / OAuth providers /
  auth methods / storage backends / image processors / search
  backends, `.slug` for themes, `.endpoint` for admin nav items)
  and raises `bragi.core.registry.DuplicateRegistration` when a
  second spec claims the same key. Pre-v1.14.0 the bare
  list-append silently shadowed the second registration: a
  third-party plugin reusing `name="post"` (or `slug="default"`,
  etc.) failed open with its registration dead and no log line.
  External architectural review (2026-05-18) flagged this as a
  silent-failure surface. The error message quotes the colliding
  kind + key and nudges operators to `cms plugins list` (#190)
  for triage; per-spec ownership tracking via pluggy's
  hook-caller introspection (so the error can name the offending
  plugin directly) remains a follow-up. The complementary
  `Registry.freeze()` proposal from the review is deferred: it
  would close the mutate-after-boot surface but breaks the three
  test sites in `tests/contrib/test_themes.py` that inject test
  themes through `app.extensions["registry"]` after factory
  return, and the bug it prevents has not been observed in
  practice.

## [1.13.0] - 2026-05-19

### Security
- **Webmention receiver gates `source` / `target` through
  `safe_external_url`.** Pre-v1.13.0 the inbox accepted both URLs
  through a local `_is_absolute_http` helper that only verified
  scheme + non-empty netloc. That let through (a) Unicode
  bidi-formatting codepoints in `source_url`, which render flipped
  in the admin moderation list AND in the public post page's
  "Mentioned by" `<a href>`, so a moderator can be fooled into
  approving a row whose visible destination differs from its real
  destination, and (b) C0 / DEL control characters, which 500
  werkzeug's header-value writer the moment the stored value
  flows through a response header or `redirect(...)`. Both
  classes are now rejected at the inbox via the centralised
  `safe_external_url` gate; the unused `_is_absolute_http`
  helper is gone.
- **`safe_external_url` rejects C0 / DEL control characters.**
  Mirrors the gate `safe_relative_path` already had. Backstops
  the webmention receiver hardening above for any future caller
  (OG-image source-page extraction, internal-links destinations)
  so the same shape can't slip back in.
- **AP inbox catches `RecursionError` on deeply-nested JSON
  (#215).** `json.loads` recurses; an unauthenticated attacker
  could flood the inbox with `[[[...]]]` / `{"a":{"a":...}}` past
  Python's default 1000 recursion limit and trigger uncaught 500s
  (the except clause previously matched only `ValueError` /
  `UnicodeDecodeError`). Signature verification happens after
  JSON parse, so no auth was needed to trigger; the now-broader
  except returns a clean 400.
- **`safe_external_url` rejects Unicode bidi-formatting
  codepoints (#209).** A stored `Webmention.author_url` with a
  `‮` (RTL override) renders flipped in the admin
  moderation list and can fool a moderator into approving a row
  whose real destination is malicious. The h-card extractor
  (and any future caller of the now-centralised
  `bragi.core.safe_urls.safe_external_url`) now rejects
  `U+202A`-`U+202E` and `U+2066`-`U+2069`.

### Added
- **`cms plugins list` CLI for plugin discoverability (#190).**
  Prints every registered plugin, its origin (`in-tree` for
  `bragi.contrib.*` packages, otherwise the distribution name +
  version), and the count of hookspecs it participates in. Reads
  the live plugin manager bound on the admin app at boot. Useful
  when triaging "is this plugin actually registered?" and as a
  quick capability survey for third-party plugin authors. Run
  via `flask --app 'bragi.apps.admin:create_admin_app' cms
  plugins list`.
- **`bragi.api` stability boundary documented (#190).** New
  top-of-module docstring in `src/bragi/api.py` codifies what's
  covered by the public plugin API (hookimpl marker, hookspec
  signatures, spec dataclasses, entry-point group) and what's
  not (`bragi.hookspecs`, `bragi.core.*`, `bragi.contrib.*`
  internals). Documents the two-step best-effort deprecation
  policy: deprecation warning across one minor version, then
  removal in the named release with a back-link from the
  CHANGELOG entry.
- **Plugin template-namespacing test (#189).** New
  `tests/contrib/test_plugin_layout.py` walks every in-tree
  `bragi.contrib.*` package and asserts that each plugin's
  `templates/` directory only carries top-level entries that
  are either the plugin's own slug or one of the shared
  prefixes `admin` / `delivery`. Two plugins shipping
  `templates/detail.html` would otherwise shadow each other
  unpredictably (Flask's Jinja loader resolves by blueprint
  registration order, which depends on pluggy discovery order).
  Theme-over-plugin shadowing stays intentional and documented.
  `bragi.contrib.auth_local`'s `login.html` /
  `change_password.html` move under `templates/auth_local/`
  to satisfy the rule; eight `render_template` callsites in
  `auth_local/views.py` updated in lockstep. No user-visible
  change to the rendered templates themselves.
- **Admin backlinks view (#116).** From a post or page edit
  form, the new "Backlinks »" link reaches a list of every
  same-site post / page whose `body_html` references this
  target via `data-bragi-link`. Useful for impact analysis
  before renaming or unpublishing a target. Backed by a new
  `internal_links` edge table (composite PK on `(site_id,
  source_type, source_id, target_type, target_id)`, indexed
  on the inbound query); the table is populated by the
  internal_links plugin's `on_post_published` /
  `on_post_updated` hooks on every save, and `on_post_deleted`
  drops edges referencing the deleted item from either side.
  New `cms internal-links rebuild-backlinks [--site <slug>]`
  command rebuilds the table from existing content on upgrade.
  Schema migration `2e99f2f0e525` creates the table; the
  table ships empty so the migration is fast on any DB size.
  Slug-form markers (`post:my-slug`) are ignored by the
  indexer; the delivery-time rewriter hardens them into
  integer form on first render, and the next save re-indexes.

### Changed
- **Moderator-facing IDN badge on the webmention moderation
  list (#225).** `safe_external_url` accepts Cyrillic / Greek
  homograph hostnames (`раураl.com`, `аpple.com`) because they
  parse as legitimate IDN, and rejecting them outright would
  break real-world non-Latin domains (`пример.рф`, `例え.jp`).
  New `is_idn_host(url)` helper renders an `[IDN]` badge next
  to source / author URLs in the admin moderation list so a
  reviewer has the signal without losing IDN traffic. Adjacent
  to the bidi rejection in #209 but distinct: bidi has no
  legitimate use, IDN does.
- **`cms db vacuum` gates on the SQLite engine (#227).** Mirror
  of the `cms backup` gate; the `PRAGMA wal_checkpoint(TRUNCATE)`
  finaliser is SQLite-specific and would 500 on Postgres.
  Without the gate the scheduler sidecar's weekly tick would log
  `failed rc=*` lines under `BRAGI_DATABASE_URL=postgresql://...`
  until ops notice. Same shape applied to
  `bragi.contrib.search.SQLiteFTS5SearchBackend`: registration
  returns None under a non-SQLite engine, so `/search` returns
  "no backend registered" instead of a per-request SQL 500.
- **Admin session cookie defaults to `Secure` in production
  (#199).** New `Settings.admin_session_cookie_secure` knob
  (default `None` = derive from `env`); `create_admin_app` sets
  `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE = "Lax"`,
  and `SESSION_COOKIE_HTTPONLY` explicitly. The default closes
  the case where a misconfigured reverse proxy or a curl probe
  against `:80` could leak `bragi_sid` over plain HTTP without
  the operator opting in. `make dev` over `http://localhost`
  still works because `env != "production"` defaults to
  `Secure=False`.
- **Redirects admin caps `source_path` at 256 characters (#200).**
  A REGEX-mode row goes through `re.compile(...).fullmatch(...)`
  at delivery time; without a length cap an editor-rank user
  could persist a catastrophic-backtracking pattern (`^(a+)+$`
  shape) and tie up a worker per matching 404. Capping the
  length bounds the worst-case input.
- **`cms backup` gates on the SQLite engine (#204).** When
  `BRAGI_DATABASE_URL` points at Postgres (or any non-SQLite
  engine), the command now exits 2 with a clear "use pg_dump"
  message rather than crashing through an opaque
  `VACUUM INTO`-not-supported SQL error.
- **Container `bragi` UID pinned to 1000 in both Dockerfiles
  (#212).** Pinned identically so a base-image bump can't shift
  the system uid range and break /data volume sharing across
  the admin and delivery images.
- **scheduler.sh sleep is interruptible (#217, #228).** New
  `sleep_interruptible` helper backgrounds the sleep so the
  SIGTERM trap fires immediately. POSIX `/bin/sh` (dash) doesn't
  interrupt a foreground `sleep` for a trapped signal, so a
  `docker compose stop` mid-tick used to wait up to the full
  sleep duration (10s main-loop / 15s alembic retry) before the
  trap could forward. Applied to both the alembic-retry sleep
  and the main-loop sleep. Reentrancy guard kills any previously
  set `$SLEEP_PID` before backgrounding a new sleeper, so a
  caller bug or future retry-wrap can't leak the prior PID past
  the trap's reach.
- **`Procfile.dev` adds a `tasks:` line (#206).** New
  `scripts/dev-tasks.sh` mirrors `docker/scheduler.sh` but
  invokes the cms commands against the local SQLite DB on a 60s
  loop, so `make dev` exercises the same code paths the
  production sidecar does.
- **compose.yml documents embed / rendition / IndexNow knobs
  (#207).** `BRAGI_EMBED_YOUTUBE_MODE`,
  `BRAGI_EMBED_OEMBED_TIMEOUT_PER` / `_AGGREGATE` /
  `_RERENDER_TIMEOUT_PER`, `BRAGI_ATTACHMENT_RENDITION_WIDTHS`,
  `BRAGI_INDEXNOW_ENDPOINT`, and `BRAGI_SECURITY_EXPIRES_DAYS`
  now appear as commented `# OPTIONAL` lines with defaults.
- **compose.yml admin block is denser (#232).** The admin
  service env block had 60 comment lines vs 5 active vars
  (12:1); operators scanning to confirm `BRAGI_ENV` /
  `BRAGI_TRUSTED_PROXY_HOPS` had to skip past 12 multi-line
  optional-knob explanations. Each optional knob is now a
  one-line commented assignment; the per-knob rationale lives
  in `src/bragi/settings.py` field docstrings (pointed at from
  the new header comment). Same shape on the delivery block.
- **README backups section spells out the SQLite-only gate
  (#230).** Postgres operators now learn from the README that
  `cms backup` exits 2 and pointed at `pg_dump`, rather than
  discovering by running it.
- **`bragi.core.safe_redirect` renamed to `bragi.core.safe_urls`
  (#231).** The module houses both `safe_relative_path` (for
  redirect targets) and `safe_external_url` (for URLs that
  never feed a redirect: webmention author URLs render as
  `<a href>`, not 302 destinations). Module name now matches
  the docstring's "user / attacker-supplied URLs" scope. Five
  callers updated; `git mv` preserves history.

### Fixed
- **Attachment delete cleanup now happens inside the cascade-delete
  transaction (#171).** Pre-v1.13.0 the delete route committed the
  cascade-delete first and then refcounted + unlinked the on-disk
  file post-commit, leaving a narrow window during which a
  concurrent upload of the same content-addressed bytes could
  insert a new row referencing the storage_key we then unlinked.
  The refcount check and `backend.remove` call now run between
  `db.flush()` and `db.commit()` so SQLAlchemy's writer lock
  (SQLite RESERVED, acquired on the cascade DELETE) queues other
  writers under WAL until our cleanup commits. Regression test
  pins the ordering invariant: every `backend.remove` call must
  precede the delete-view's commit.
- **`pyproject.toml` migrated to PEP 621 `[project]` table
  (#168).** Poetry 2.x deprecated `[tool.poetry]` for metadata
  (name / version / description / authors / dependencies /
  scripts / readme / license / requires-python); on 2.x `poetry
  check` emitted one warning per field. All metadata now lives
  under `[project]`; the `bragi.plugins` entry-point group moves
  to `[project.entry-points."bragi.plugins"]`; the
  `bragi-admin` / `bragi-delivery` console scripts move to
  `[project.scripts]`. `[tool.poetry]` retains only the
  src-layout `packages` declaration (no portable PEP 621
  equivalent). Caret constraints translated to PEP 508 explicit
  bounds; runtime behaviour unchanged. Portfolio-wide sweep also
  applied to mimir + johnny in parallel PRs.
- **Empty `BRAGI_ADMIN_SESSION_COOKIE_SECURE=` no longer crashes
  boot (#226).** Pydantic-settings parses an exported-but-empty
  env value as `""`, which pydantic's bool validator rejects with
  `bool_parsing`. Operators who delete a value from `.env` and
  leave the key (or write `KEY=` in shell sourcing) hit a fatal
  boot-time error instead of the documented "unset = derive from
  env" path. A `BeforeValidator` on the field coerces `""` to
  `None`.
- **`fanout_for_post` docstring spells out the unfollow/refollow
  edge case (#224).** Pass-8 audit flagged the dedup as missing
  the cycle, but inspecting the schema shows `follower_id` is
  `ondelete=CASCADE`: the Undo Follow handler deletes all outbox
  rows referencing the follower (SENT included). The dedup
  invariant holds for surviving rows; the unfollow/refollow case
  re-queues a fresh Create+Note that the receiver dedups by
  `activity.id`. No code change; docstring now matches reality
  and notes why the SET NULL alternative wasn't pursued.
- **`cms backup` Postgres-engine path now has test coverage
  (#229).** Mocked dialect, asserted `exit_code == 2` and the
  user-facing "requires SQLite" / "pg_dump" message; a future
  refactor can't silently drop the gate.
- **CI workflow has explicit minimum permissions (#205).**
  `permissions: contents: read` at the top level so the
  workflow token never carries repo-write by default when CI
  runs untrusted PR code from forks.
- Miscellaneous internal refactors (closing #201, #202, #203,
  #211, #213, #216, #218): `_safe_external_url` lifted from
  `webmentions/parse.py` into `bragi.core.safe_urls` for
  reuse; webmention `_queue_outbox_for_post` fallback uses
  `urlparse(...).hostname` not `.netloc`; redirects admin flash
  enumerates "no control characters" alongside the existing
  rejections; activitypub views / signature `import time` /
  `threading` moved to module level; search `_is_published`
  drops the dead `or status == "published"` clause; new test
  for AP unpublish-cleanup parity with the webmention side;
  compose.dev.yml carries inline comments explaining why
  `BRAGI_ENV` and `BRAGI_TRUSTED_PROXY_HOPS` are deliberately
  unset.

## [1.12.0] - 2026-05-18

### Security
- **Cookie-session path treats inactive users as anonymous.**
  An admin who flips `User.is_active=False` expected access to
  stop immediately. The bearer middleware already re-checks
  `is_active` per request, but `current_user()` (the cookie-
  session path used by every admin route gate) returned the
  inactive User row unchanged. A disabled user kept their
  privileges until they logged out or the session row expired.
  `current_user()` now returns None when `User.is_active=False`,
  closing the asymmetry. `auth_local`'s login already enforces
  `is_active=True` at sign-in, so a fresh login still works as
  soon as the admin re-enables the user.
- **Bearer cache re-checks `PersonalAccessToken.expires_at` on
  every request.** The verify-result cache (#M4 / pass-4) stores
  `(token_id, user_id, scopes_tuple)` with no timestamp, so a
  token verified just before its `expires_at` kept authenticating
  for up to TTL (10 s) past the declared end. The cache-hit path
  already re-reads the token row to bump `last_used_at`; it now
  also reads `expires_at` and treats a past value as no-auth.
- **Page revision restore re-runs the cross-site `parent_id`
  check.** Pass-4 closed cross-site `parent_id` on the page
  create / edit paths via `_validated_parent_id_or_error`, but
  the revision-restore handler still copied
  `revision.parent_id` verbatim. A revision row captured before
  v1.12.0 (or any future row planted by a manual SQL fixup, a
  background-job bug, or a non-admin write path) could carry a
  cross-site value that the create/edit paths would now reject;
  clicking "Restore" reintroduced the corrupted row. The restore
  path now runs the same validator and refuses the restore with
  a flash message if the captured `parent_id` doesn't name a
  page on the current site.
- **`safe_relative_path` rejects C0 / DEL control characters.**
  A login `?next=/\nfoo` 500s the auth view's `redirect(...)`
  call (werkzeug's HTTP header writer rejects `\r`/`\n`); worse,
  a redirects-admin row persisted with `target="/\nfoo"` turns
  every subsequent matching delivery request into a 500 on the
  response-header writer. Reject inputs with any of `\x00-\x1f`
  or `\x7f` at the application gate so the failure stays on
  the input side rather than turning into a persistent per-URL
  DoS owned by editor-rank users.
- **Bearer cache invalidates on admin token revoke.** The
  per-process verify-result cache (#M4 / pass-4) re-checks
  `User.is_active` on a hit but not whether the token row still
  exists. An admin who clicked "Revoke" expected the token to
  stop authenticating immediately; in practice it kept working
  for up to `_VERIFY_CACHE_TTL_S` (10s) because the cache
  short-circuited the DB lookup that would have noticed the row
  is gone. `revoke_token` now calls a new
  `invalidate_verify_cache_for_public_id(public_id)` that drops
  every cache entry for the revoked token's public_id.
- **Login `?next=` / redirects `target=` reject backslash-escaped
  off-domain URLs.** Browsers normalise `\` to `/` in special-
  scheme (http / https) URLs before parsing per the WHATWG URL
  spec, so a `next=/\evil.example/x` lands the user on
  `evil.example` after the 302 even though the path looked like
  a same-host redirect. Pass-4 closed the `//` protocol-relative
  case but missed the WHATWG-equivalent `\` form; the admin
  domain (where the user just typed credentials) is a credible
  launchpad for phishing. A new shared
  `bragi.core.safe_redirect.safe_relative_path` helper backs
  three callsites: `auth_local._safe_next`, `auth_github._safe_next`,
  and the redirects admin `_validate`. Inputs with `\` (single
  or repeated) are now rejected at form/query-arg time.
- **Webmention h-card extractor drops `javascript:` / `data:` URLs
  before persistence.** An attacker hosting a page like
  `<a class="h-card" href="javascript:fetch('/'+document.cookie)">`
  could send a webmention pointing at it; `extract_hcard` previously
  ran `urljoin` and stored the string verbatim as
  `Webmention.author_url`. The admin moderation list shows the
  URL as truncated plain text, so a moderator couldn't easily
  preview the trap; once Approved, the URL rendered as a
  clickable `<a href>` on the public post and a reader's click
  executed attacker JS in the delivery origin. `extract_hcard`
  now gates `author_url` and `author_photo` through a new
  `_safe_external_url` that requires an http(s) scheme.
- **Redirects admin rejects absolute and protocol-relative
  targets.** The admin form's `target` field accepted any string;
  the redirect resolver follows the chain and serves the raw
  target as the 301 destination. An editor-rank user could
  insert `target=https://evil.example/phish` and turn the site's
  redirect table into a 301 phishing primitive against its
  readers. The admin validator now requires `target` to start
  with `/` and rejects `//evil.example/x` (protocol-relative).
  Importers and slug-change auto-301s always construct relative
  targets, so the constraint only affects the human-facing
  admin form.
- **Page admin validates cross-site `parent_id` on both
  create and edit.** Pre-fix, an author on site A could POST
  `parent_id=<id-of-page-on-site-B>` and persist a row with
  `(site_id=A, parent_id=B)`. Delivery-side resolution filters
  by site so the corrupted row never serves real content, but
  the cross-site parent slug leaked into the sitemap and any
  slug-change auto-redirect derived from the URL chain. A new
  `_validated_parent_id_or_error` helper mirrors the same-site
  check the OG-image and default-OG-image resolvers already do.
- **Bearer middleware caches verify outcomes to bound argon2
  amplification.** Every bearer request invoked `argon2.verify()`
  (~100 ms / ~64 MiB) whenever the presented public_id matched a
  row. An attacker who guessed any valid public_id could
  saturate a worker by hammering with random secrets, and a
  legitimate high-QPS integration paid argon2 per request. The
  middleware now caches `(public_id, sha256(secret))` -> verify
  outcome for 10 seconds with a 4096-entry bound. Repeat
  presentations short-circuit argon2; a smart attacker who
  varies the secret per request still pays full argon2 (keys
  are unique to them) but they're now bound only by network
  throughput. The `last_used_at` bump and `User.is_active`
  re-check still run on every cache-hit request so a disabled
  user takes effect immediately; only argon2 is short-circuited.
- **SSRF guard re-resolves the host at send-time and refuses
  on DNS rebinding.** A DNS-rebinding attacker can serve a
  public IP at URL-validation time and a private IP at
  connection time (TTL 0 or interleaved A records). The
  validation-time `_validate_url` then succeeds, the
  `requests` / `urllib3` stack does its own `getaddrinfo` at
  connect time, and the socket opens against the private IP.
  `_validate_url` now returns the validated IP set;
  `_GuardedAdapter.send` re-resolves the host one more time
  and asserts the connect-time IPs are a subset of the
  validated set. A residual TOCTOU window remains between
  this check and the kernel's actual `getaddrinfo` at socket-
  connect time (microseconds); full IP-pin-and-Host-rewrite at
  the urllib3 connection layer is the next step.
- **Webmention inbox fetch timeout tightened from 10s to 3s.**
  The verify-first refactor in v1.12.0 moved DB writes after
  the source fetch, eliminating the row-per-request DoS surface,
  but the synchronous fetch still tied up a worker for the
  full timeout window. With 4 workers and a 10s timeout, 0.4
  req/s sufficed to pin the inbox. The dedicated
  `HTTP_TIMEOUT_SECONDS = 3.0` on the receiver still tolerates
  typical Mastodon-class RTTs while bounding the blast radius
  of a malicious slow-source URL.
- **GitHub OAuth callback no longer auto-links a new identity
  to an existing local User by matching email.** Operators
  running v1.x with both local-credential auth (e.g. an
  `admin@example.com` seeded via `cms user create`) and GitHub
  OAuth could be impersonated: an attacker who registered a
  GitHub account using the operator's email and clicked "Sign
  in with GitHub" was logged in as that admin, because the
  callback's email-match fallback linked the brand-new
  GitHub identity onto the existing local user row. The
  fallback is gone. The callback now refuses an OAuth login
  whose email collides with an existing local user, redirects
  the user back to the local-auth login form with a clear
  flash, and audits the attempt as
  `auth.login.failure` with
  `reason="oauth-email-collides-with-existing-user"`. Linking
  a second auth method onto an existing account is a future
  admin-side affordance (out of scope for the fix); until
  then, operators who want both methods should pick one or
  use a different email per identity.
- **Attachment uploads gate on a content-type allowlist;
  delivery serves with `X-Content-Type-Options: nosniff`
  and inline-only-for-safe types.** Before this change, any
  author-rank user on any site could upload an SVG (which can
  embed `<script>`) or an HTML payload with a forged
  `Content-Type`, and the delivery handler served the bytes
  with the persisted content-type, `Content-Disposition:
  inline`, and a year-long `Cache-Control: public, max-age,
  immutable`. The result was a stored XSS on the public
  reader surface with a year-long edge-cache TTL. New
  `_ATTACHMENT_ALLOWED_CONTENT_TYPES` in `attachments/admin.py`
  accepts only the Pillow-handled image types, `application/pdf`,
  and `text/plain`; everything else is rejected at upload time
  with a clear error. For declared `image/*` uploads the
  Pillow probe must succeed (magic-byte verification), so a
  forged `image/png` content-type carrying HTML is rejected.
  Delivery now sets `X-Content-Type-Options: nosniff` on every
  response and only serves `Content-Disposition: inline` for
  inline-safe types (images / PDF / text); rows that pre-date
  the allowlist serve as `attachment` instead.
- **Webmention receiver verifies before persisting (#181).**
  The inbox used to insert a `PENDING` row immediately on
  receipt, then flip it to `REJECTED` or `FAILED` when the
  source-fetch or link-presence checks failed. With no `UNIQUE`
  on `(site_id, source_url, target_url)` and no rate limit, that
  was a per-request DoS surface: an unauthenticated attacker
  could flood `POST /webmentions` with arbitrary pairs and
  persist one row per request. Source fetch + link check now run
  before any DB write; failed attempts log a warning and return
  400 without persisting. The admin moderation list now contains
  only verified-but-pending-approval rows, which is the surface
  humans act on.
- **ActivityPub `Signature` header parser is quote-aware (#182).**
  The previous `raw.split(",")` form silently mis-parsed any
  parameter value carrying a comma, which the draft-cavage spec
  permits for parameter-extension values. A new regex tokenizer
  preserves the whole quoted value. Not exploitable today
  (verification still requires the signature to validate
  end-to-end), but matters as soon as a signer library adopts an
  extension that embeds commas. Bare unquoted values continue to
  parse for spec-permissive senders.

### Changed
- **FTS5 search `total` is short-TTL cached (#183).** The single
  UNION ALL + LIMIT/OFFSET paged hit fetch is cheap, but `total`
  still required two MATCH-evaluating `COUNT(*)` queries on
  every search page. The total now passes through a per-process
  `(site_id, safe_query) -> (count, written_at)` cache with a
  30-second TTL and a 4096-entry bound, and is invalidated on
  every post / page publish, update, and delete event so newly
  indexed rows surface in the count immediately. The hit list
  stays fresh because the LIMIT/OFFSET query runs every request;
  only the count can lag by up to one TTL window when no write
  intervenes. Cache keys also case-fold and sort tokens, so
  equivalent queries (`Hello World` vs `world hello`) share a
  cache slot.
- **Admin app asserts boot ordering of CSRF and plugin
  middleware (#187).** Plugin-provided before_request middleware
  (notably the bearer middleware in `bragi.contrib.api_tokens`
  that sets `g.api_csrf_exempt`) must register BEFORE the CSRF
  guard so it runs first at request time, otherwise valid
  bearer-token POSTs without a session CSRF token would be 400'd
  by the CSRF guard. `register_csrf` now raises a `RuntimeError`
  at boot if `pm.hook.on_app_init` hasn't run yet, naming
  `apps/admin.py` as the fix location. CSRF itself was already
  fail-closed (`g.api_csrf_exempt` defaults falsy, exemption
  fires only when truthy and only after verified-bearer auth);
  the assertion catches an ordering regression that would break
  the bearer API, not a CSRF bypass.

### Fixed
- **ActivityPub fanout is idempotent across restore-as-republish.**
  PR-D's `_drop_pending_outbox_for_post` deletes PENDING outbox
  rows on unpublish but keeps SENT / FAILED rows as audit, and
  PR-E added `on_post_published` firing on restore-as-publish.
  Without dedup, a publish -> unpublish -> restore-as-republish
  cycle queued a SECOND Create+Note per follower (followers
  received a duplicate). `fanout_for_post` now skips followers
  that already have a non-FAILED `ActivityPubOutbox` row for
  `(post_id, follower_id)`. FAILED rows are NOT skipped so an
  operator who wants to retry a previously-failed delivery via
  republish can. The webmention plugin already did this shape
  via `existing_targets`; the AP version now mirrors it.
- **Outbox sender no longer rolls back the whole batch when an
  unpublish race deletes a row mid-flight.** The webmention and
  ActivityPub outbox senders SELECT every PENDING row, mutate
  them in-memory, then `db.commit()` once at the end. When the
  unpublish-cleanup path (`_drop_pending_outbox_for_post`)
  deleted one of those rows out from under the sender, SQLAlchemy
  2.x's default `confirm_deleted_rows=True` raised
  `StaleDataError` on the sender's UPDATE for the deleted row,
  rolling back EVERY successful send in the same batch. On the
  next tick the recipients of the successful sends received
  duplicates. Setting `__mapper_args__ = {"confirm_deleted_rows":
  False}` on both `WebmentionOutbox` and `ActivityPubOutbox`
  makes the 0-row UPDATE a no-op; the other status flips persist.
- **`bragi-tasks` no longer livelocks on a persistently-failing
  migration.** A broken `alembic upgrade head` exited 1, compose
  restarted `unless-stopped`, alembic failed again, repeat
  forever; the web services sat in `created` state and operators
  saw no clear failure signal. `scheduler.sh` now retries
  alembic with backoff (`ALEMBIC_MAX_ATTEMPTS=5`,
  `ALEMBIC_RETRY_DELAY=15s`) and exits 0 with a loud log line
  after exhausting attempts. `compose.yml` switches the
  `bragi-tasks` restart policy from `unless-stopped` to
  `on-failure` so the deliberate exit 0 terminates the restart
  loop and the service shows as `Exited (0)` with the failure
  message above it.
- **Apps wire `ProxyFix` when behind a trusted reverse proxy.**
  New `Settings.trusted_proxy_hops` (default 0; the production
  `compose.yml` sets it to 1). When > 0, both `create_admin_app`
  and `create_delivery_app` wrap the WSGI callable in
  `werkzeug.middleware.proxy_fix.ProxyFix(x_for, x_proto, x_host)`
  with that hop count. Three breakages on a fresh prod deploy
  used to manifest as: (a) the GitHub OAuth `redirect_uri` built
  via `url_for(_external=True)` emitted `http://...` because
  `request.scheme` was the proxy's tell-the-app value; (b) every
  `AuditLog.ip` and `Session.ip` row recorded the reverse proxy's
  IP, hiding the real user; (c) per-IP analytics grouped every
  visit under the proxy. ProxyFix rewrites all three from the
  `X-Forwarded-*` headers. NEVER set the hop count higher than
  the actual reverse-proxy depth: each unit extends spoofability
  one hop outward.
- **Post `published_at` is preserved across republish.** The
  admin update path read "draft -> published" as a first-publish
  transition unconditionally and stamped `published_at =
  naive_utcnow()`, so a draft -> published -> draft -> published
  cycle re-stamped the column and silently floated old posts to
  the top of "newest first" lists. The api_tokens write path
  already gated on `published_at is None`; the admin path now
  mirrors that. The comment "Re-publishing doesn't reset the
  timestamp" finally describes what the code does.
- **Revision restore fires `on_post_published` on a
  draft->published transition.** PR-C added `on_post_updated`
  firing on restore; the publish event was missed. A restored
  draft that crosses the published boundary now fires
  `on_post_published` so the ActivityPub plugin's Create+Note
  fanout, the sitemap rebuild trigger, and any other
  `on_post_published` subscriber see the transition. The post
  restore also stamps `published_at` on the first publish via
  restore (`post.published_at is None` -> set), matching the
  normal-edit path.
- **Webmention + ActivityPub outbox PENDING rows are abandoned
  on post unpublish.** Both senders processed every PENDING row
  without checking the source post's current status, so a post
  unpublished after `on_post_published` queued the fan-out
  delivered a Note/webmention pointing at a URL that now
  404s/410s. `webmentions.plugin.on_post_updated` and a new
  `activitypub.plugin.on_post_updated` now detect the
  `published -> not-published` transition and delete the
  PENDING rows for the post; SENT / FAILED rows stay as audit.
  Issuing a fediverse `Delete` activity for already-sent Notes
  is the richer fix and is deferred.
- **Webmention inbox dedupes repeat presentations of the same
  `(source, target)` pair.** A well-behaved Mastodon retry would
  previously accumulate one row per send in the moderation
  queue, and approving one of them didn't dedupe the rest. The
  receiver now queries for an existing row matching
  `(site_id, source_url, target_url)` before insert; on a hit,
  it refreshes the parsed h-card / content snippet / mention
  type / `verified_at` but leaves moderation state (`status`,
  `approved`) untouched so a previously-rejected mention can't
  be re-presented into the queue.
- **Containers run as non-root `bragi` user; gunicorn has a
  graceful-shutdown window.** `docker/admin.Dockerfile` and
  `docker/delivery.Dockerfile` add a `USER bragi` directive
  (and `chown` /app + /data) so a worker RCE escapes to a
  uid != 0 process rather than uid 0 with write access to the
  bind-mounted /data volume. Both gunicorn `CMD`s gain
  `--graceful-timeout 25` and `compose.yml` services set
  `stop_grace_period: 30s` so an in-flight outbound POST
  (webmention sender, AP delivery) has up to 25s to return
  before SIGKILL fires. `docker/scheduler.sh` traps SIGTERM /
  SIGINT and forwards to the active child PID so a
  `docker compose stop` mid-vacuum lets the vacuum finish
  cleanly rather than getting SIGKILLed after the loop's
  `sleep 10`.
- **`docker.yml`: `:latest` is gated to non-prerelease semver tags.**
  The `type=raw,value=latest` line emitted `:latest` on every tag
  push, including `v1.12.0-rc1` and any hotfix off an older
  lineage. Switched to `flavor: latest=auto` so `:latest` only
  follows non-prerelease tags. Caveat: a hotfix cut off an
  older major (e.g. `v1.10.5` after `v1.11.0` shipped) still
  claims `:latest` because the action compares the tag pattern
  rather than the tag ordering against the registry. Operators
  cutting hotfixes off older majors must manually re-tag
  `:latest` afterwards if needed.
- **FTS5 search `total` cache invalidates on lifecycle events.**
  Post / page publish, update, and delete now flush
  `_SEARCH_TOTAL_CACHE` via the lifecycle hookimpls in
  `bragi.contrib.search.plugin`, so a newly published document
  appears in the "X results" counter immediately rather than
  after the next TTL window. Equivalent queries (`Hello World`
  vs `world hello`) also collapse to one cache slot: `_safe_query`
  now case-folds and sorts tokens, which is safe because FTS5
  is case-insensitive by default and AND is commutative.
- **Page / post revision restore fires `on_post_updated`.**
  Restoring a prior revision mutates the live row (slug, title,
  status, body, ...) but never fired the same lifecycle hook a
  normal save does. Plugin subscribers (search index reindex,
  redirects auto-301 on slug change, AP outbox fanout on a
  status->published transition) silently missed every restore.
  Both `post_admin.restore_revision` and
  `page_admin.restore_page_revision` now capture before/after
  snapshots, fire `pm.hook.on_post_updated`, and dispatch
  `on_cache_purge` so the restore is observable end-to-end.
- **Audit log `action` filter escapes SQL LIKE metacharacters.**
  An admin filter value containing `%` or `_` was interpreted as
  a wildcard, so `auth%` matched every row whose action started
  with `auth` and `_` matched any single character. The filter
  now SQL-escapes both characters (plus `\`) before interpolation
  and passes `escape='\\'` to `like()`, so the input matches
  literally and the planner can use the action index.
- **Actor cache reads happen under the lock; post-fetch recheck.**
  `_fetch_actor` previously read the cache without holding
  `_ACTOR_CACHE_LOCK`, so the read could race a concurrent
  overflow eviction. The lock now also brackets the read and a
  post-fetch recheck: a concurrent inbox POST for the same IRI
  that won the fetch race writes its result first, and we use
  that fresher copy instead of overwriting it. Full single-flight
  (per-IRI condition variables) is still deferred.
- **`is_external` compares on hostname, not netloc.**
  `urlparse(url).netloc` includes the port and userinfo, so a
  remote URL carrying an explicit `:443` would never match a
  `Site.hostname` (no port). Affected the webmention outbound
  link-scan: same-site links could be misclassified and a
  spurious mention sent to a sibling site. Now compares on
  `hostname`.
- **`cms backup` / `cms export` route timestamps through
  `bragi.core.time.aware_utcnow`** for parity with the rest of
  the codebase. `VACUUM INTO` now SQL-escapes single quotes in
  the destination path; the path comes from a freshly-created
  `TemporaryDirectory`, so this is defence-in-depth against a
  pathological `$TMPDIR` rather than a real bug.
- **`docker/compose.dev.yml` parity with production `compose.yml`.**
  The local-build compose file was missing `BRAGI_ATTACHMENTS_ROOT`
  on every service and `/healthz` healthchecks on `admin` /
  `delivery`. A `compose -f docker/compose.dev.yml up` run now
  matches the production shape so smoke tests don't drift from
  what the published images get. Production `bragi-tasks` also
  gains `BRAGI_ENV: production` (and `BRAGI_ATTACHMENTS_ROOT`) so
  a mis-set `BRAGI_SECRET_KEY` fails the sidecar's first
  migration tick rather than silently running with the dev key.
- **`redirects.source_path` enforces `length(...) > 0` (#184).**
  Defence-in-depth: the admin form already rejects empty input
  and every programmatic writer constructs non-empty strings,
  but the SQL-side PREFIX resolver
  (`substr(:path, 1, length(source_path)) = source_path`) would
  collapse to `'' == ''` for any incoming path if an empty row
  ever landed (importer bug, manual SQL fix-up, future code
  path). The new `ck_redirects_source_path_nonempty` constraint
  rejects empty rows at the DB level. Alembic migration
  `a1b2c3d4e5f6` adds the check via `batch_alter_table`.

## [1.11.0] - 2026-05-18

### Added
- **ActivityPub federation: one actor per site (#148).** New
  `bragi.contrib.activitypub` plugin turns each Site into a
  follow-able fediverse actor addressed as
  `@<site-slug>@<hostname>`. Mastodon users follow the actor;
  published posts arrive as Create+Note activities with a link
  back to the canonical post URL. New tables: `site_keypairs`
  (per-site RSA 2048, generated on first /actor hit or via
  `cms activitypub keygen --site SLUG`), `activitypub_followers`
  (one row per remote actor), `activitypub_outbox` (per-recipient
  delivery queue). Endpoints on the delivery app:
  `/.well-known/webfinger`, `/actor`, `/actor/inbox`,
  `/actor/outbox`, `/actor/followers`. HTTP signatures
  (draft-cavage-http-signatures-12, RSA-SHA256) on outbound
  POSTs; inbound POSTs verified against the sender's published
  `publicKeyPem`. `Follow` and `Undo Follow` activities are
  handled; other types ACK silently. On post publish, a fanout
  queues one row per follower, and `cms activitypub send-pending`
  ships them. Out of v1: receiving replies as comments (bragi
  has no comment system), outbound Like / Boost / Reply, DM-
  style ActivityPub, multi-actor per author. New `cryptography`
  dep (was already transitively present via authlib).
- **Webmentions: send + receive + moderate (#147).** New
  `bragi.contrib.webmentions` plugin closes the indieweb loop on
  both sides. On `on_post_published` / `on_post_updated` (when
  the post lands published), every external `<a href>` in the
  rendered body is queued in `webmention_outbox`; the new
  `cms webmentions send-pending` CLI walks the queue, performs
  endpoint discovery per W3C §3.1.2 (Link header first, then
  `<link rel="webmention">` in `<head>`), and POSTs the mention.
  Idempotent on already-sent rows; bounded by `--limit` for
  cron-friendly runs. On the inbox side, `POST /webmentions` on
  the delivery app validates the source URL fetches and links to
  the target (per W3C §3.2.1), extracts an h-card author shape
  (regex subset; full mf2py is out of v1 per the issue), and
  inserts a row with `status=verified, approved=false`. Admin
  moderation at `/admin/sites/<slug>/webmentions/` approves /
  rejects; the post template renders an "Mentioned by" aside
  listing approved verified rows. Discovery
  `<link rel="webmention">` is injected into the delivery
  `<head>` automatically.
- **API tokens for programmatic posting (#146).** New
  `personal_access_tokens` table backs long-lived bearer
  credentials in the format
  `brg_<public_id>_<secret>` (public_id is a 22-char urlsafe
  base64 of a uuid4; secret is 32 url-safe chars argon2id-hashed
  at rest). The new `bragi.contrib.api_tokens` plugin installs a
  `before_request` (with `tryfirst=True` so it runs ahead of the
  session auth guard) that accepts
  `Authorization: Bearer ...`, populates `g._cached_user` from
  the token's owner, bumps `last_used_at`, and writes a
  `token.used` audit row. CSRF middleware steps aside when an
  `Authorization: Bearer` header is present (a CORS-restricted
  header that cross-origin browser scripts can't add). Admin
  pages at `/admin/account/tokens/` list / create / revoke
  tokens (plaintext shown ONCE on create); a JSON REST surface at
  `/admin/api/sites/<slug>/posts/` covers GET list, POST create
  (201), PATCH update, and POST publish, scope-gated by
  `post:write` on bearer requests. Token scopes are a JSON list;
  `page:write` is reserved for a follow-up page REST surface.
  Audit rows: `token.created`, `token.revoked`, `token.used`.
- **`cms export` CLI: per-site Hugo-shaped corpus dump (#145).**
  `flask --app bragi.apps.admin cms export [--site SLUG] [--output DIR]`
  writes a Hugo content tree for each site: posts as
  `content/posts/<slug>.md` with YAML frontmatter (title, date,
  draft, description, tags, aliases, og_image); pages under
  `content/pages/` with extra `kind` + `parent_slug` keys;
  attachment bytes as `static/attachments/<storage_key>` alongside
  an `attachments.csv` metadata manifest (filename, content_type,
  alt_text, dimensions, focal point); the full redirect table as
  `redirects.csv`. Output is deterministic so re-running against
  an unchanged DB yields a byte-identical tree, and posts
  round-trip through `cms import hugo` per source_id: import to
  export to re-import creates no new rows. Closes the
  "static-site rebuild" trigger noted in MEMORY.md against the
  deferred JSON API thread.
- **Chronological archive at `<post_index>/archive/` (#144).** Three
  levels, each rendering a flat list of the next level's counts:
  the archive index shows years (descending) with post counts; a
  year page shows months (January through December) with counts;
  a month page shows the posts published in that bucket in
  chronological order (oldest first, journal-style). Drafts are
  excluded from counts and listings. Out-of-range months
  (`13`/`00`), non-integer segments, and empty buckets all 404.
  Each level attaches the standard `ETag` + `Last-Modified`
  validators (the per-row `updated_at` folds into `Last-Modified`;
  a sentinel `ARCHIVE_ETAG_VERSION` lets a future markup change
  bust caches without waiting for content edits). Routes are
  peeled by the page plugin's catch-all dispatcher, so the URL
  prefix follows whatever `post_index` page the site has
  configured. Lives in `bragi.contrib.page.archive` next to the
  other post-index renderers.
- **`cms backup` CLI: one-file DB + attachments tarball (#143).**
  `flask --app bragi.apps.admin cms backup [--output PATH]`
  runs `VACUUM INTO` against the SQLite DB (a consistent
  snapshot that includes the WAL state with no -wal / -shm
  companion files), then tars the snapshot plus the contents of
  `Settings.attachments_root` into a `.tar.gz`. Default output
  is `bragi-backup-YYYYMMDD-HHMMSS.tar.gz` in the CWD. No paired
  `restore` subcommand on purpose: the documented restore step
  is "extract the tarball, drop into a fresh deployment,
  restart"; a CLI that overwrites live state is a big risk for
  not much help.
- **Auto-generated table of contents on multi-section posts
  (#142).** Posts whose rendered HTML carries two or more
  qualifying headings (h2 / h3 by default) now render an
  `<aside class="toc-wrapper">` above the body with a nested
  `<ol class="toc">` linking to each heading's anchor. Single-
  section posts render no TOC at all; the "multiple headings =
  wants a TOC" rule is the author's natural signal of intent.
  Builder lives in `bragi.core.render.toc` (regex on the already
  -anchored HTML, no BeautifulSoup dep). Default theme styles
  the aside as a contained card.
- **KaTeX-compatible math syntax + Mermaid code fences (#141).**
  `markdown_extras` now bundles two more parser additions:
  `mdit-py-plugins.dollarmath` for `$...$` (inline) and
  `$$...$$` (block) math, and a fence-rule override that
  preserves ` ```mermaid ` blocks under
  `<pre class="mermaid">`. Math wrappers use the standard
  `<span class="math inline">` / `<span class="math block">`
  shapes that KaTeX's auto-render extension finds out of the box.
  Operators add KaTeX / Mermaid `<script>` tags to their theme's
  `base.html` to enable rendering (no JS bundles shipped in v1;
  CDN dependency or theme-controlled inclusion is left to the
  operator). `allow_digits=False` on dollarmath so `$5` in
  prose still renders as text rather than turning into math.
- **Per-tag Atom feeds (#140).** New endpoint
  `<post_index>/<tag_segment>/<tag-slug>/feed.xml` lets focused
  subscribers track a single topic. Same Atom 1.0 envelope as the
  site-wide `/feed.xml`, filtered to posts carrying the tag.
  Tag-listing template surfaces both the site-wide and per-tag
  feeds via `<link rel="alternate" type="application/atom+xml">`
  for browser / reader auto-discovery. Site-wide feed discovery
  is also added to the default theme's `base.html` so every page
  now exposes it. Atom-builder logic moved to `bragi.core.feed`
  so the seo plugin's site-wide feed and the page plugin's
  per-tag feed share one entry-XML helper (no plugin-to-plugin
  imports).
- **Related posts at end of article (#139).** Each post page now
  renders a "You may also like" aside below the body listing up
  to N same-site published posts ranked by tag-overlap count
  (more shared tags wins), tie-broken by `published_at` desc.
  Posts with no tags or no overlapping siblings render no aside
  at all. Default `N` is 3; per-site override via
  `Site.extra_settings["related_posts_count"]`. The query is a
  single GROUP BY against the `post_tags` junction, so the
  feature costs one extra SELECT per post render.
- **Post-page chrome: author byline, reading time,
  updated-date, optional bio (#138).** The post template now
  carries the meta line a modern blog reader expects: "by Ada
  Lovelace", "5 min read", and an "Updated YYYY-MM-DD" line that
  appears only when the edit is meaningfully after the first
  publish (`updated_at - published_at >= 1 day`, suppressing
  typo-fix noise). Optional `User.bio` text renders as an "About
  the author" aside below the post body when set. Reading-time
  helper lives in `bragi.core.render.reading_time`
  (220 WPM, rounded up so short posts say "1 min read"). Alembic
  migration `ad0c0c05ef40` adds `users.bio` (nullable Text). No
  admin UI for editing `bio` in v1; operators set it via DB
  direct, account-settings admin tracked as a follow-up.
- **Open Graph and Twitter Card meta tags (#137).** Post, page,
  and post-index templates now emit `og:title` / `og:type` /
  `og:url` / `og:description` / `og:site_name` / `og:image`
  plus the matching `twitter:card` / `twitter:title` /
  `twitter:description` / `twitter:image` so social shares
  render rich previews instead of bare links. Image source
  resolves through the chain `post.og_image_id` (or
  `page.og_image_id`) -> `Site.default_og_image_id` -> omitted;
  when no image is present the twitter card falls back to
  `summary` from `summary_large_image`. New `core.seo.og_image_url_for`
  helper builds the absolute URL from `site.canonical_url` +
  `attachment.storage_key`. Admin edit forms on Post, Page, and
  Site grew a numeric `og_image_id` (and `default_og_image_id`)
  input gated by a same-site attachment check; cross-site ids
  are rejected with an error message. Alembic migration
  `22e5570ca7f5` adds `pages.og_image_id` and
  `sites.default_og_image_id` (both FK to `attachments` with
  `ON DELETE SET NULL`); `Post.og_image_id` was already on the
  schema from an earlier migration.
- **Footnote markdown syntax (#136).** New built-in plugin
  `bragi.contrib.markdown_extras` wires
  `mdit-py-plugins`' `footnote_plugin` into the app-bound
  markdown renderer, so post and page bodies accept the standard
  `text[^id]` reference + `[^id]: body` definition syntax.
  Refs render as `<sup class="footnote-ref">` inline; the
  collected list lands in a `<section class="footnotes">` at
  the bottom of the document. Default theme picks up matching
  CSS for the inline brackets and back-references. Comment the
  `markdown_extras` line in `pyproject.toml`'s
  `bragi.plugins` block to disable.
- **Kind toggle and home_page_id changes now insert redirects
  (#130).** Promoting one page to `post_index` while demoting
  another (the swap path) now fires `on_post_updated` for the
  demoted page too; the redirects plugin reads the
  before/after `kind` and inserts a PREFIX 301 from the old
  index URL to the new. Setting / clearing / changing a site's
  `home_page_id` is handled inline in the sites admin save
  handler: an EXACT 301 (STATIC home) or PREFIX 301 (POST_INDEX
  home) is inserted alongside the site update, and the previous
  redirect is deactivated atomically. New `RedirectSource` labels
  `kind-change` and `home-change` distinguish these rows from
  slug-renames in the redirects admin. Demotion with no
  replacement `post_index` still leaves posts orphaned (410-per
  -post deferred).
- **Demotion-confirmation banner for the only POST_INDEX page
  (#131).** Demoting a site's only `post_index` page to `static`
  now re-renders the edit form with a warning that quantifies
  the impact (number of published posts that will lose their
  public URL). The form ships an implicit `acknowledge_demotion`
  field on the retry that lets the demotion through. Parallels
  the existing promotion-swap confirmation (`acknowledge_swap`).
  The check is skipped when another `post_index` exists for the
  site (which can only happen as a defensive corner today) so
  cleanup saves don't loop on the banner.
- **Configurable tag-segment word per site (#132).** Sites can
  override the URL segment used for tag listings via
  `Site.extra_settings["tag_segment"]` (same shape as
  `posts_per_page`). Default `"tag"`; setting it to e.g.
  `"category"` makes the dispatcher accept
  `<post_index_url>/category/<slug>/` and `tag_url_for()` emit
  the same. Non-string, empty, or non-slug values fall back to
  `"tag"` defensively. No admin UI in v1; operators edit via
  CLI / DB, matching `posts_per_page`.
- **Static homepage per site (#124).** Each Site grew a new
  `home_page_id` column (nullable FK to `pages`, `ON DELETE SET
  NULL`). When set on the site edit form, `/` renders the
  referenced Page instead of the recent-posts index; clearing
  the selection reverts to the index without any code change.
  The new `resolve_home(site)` hookspec (`firstresult=True`)
  arbitrates this: the page plugin ships a `tryfirst` impl that
  serves the configured static page, and the post plugin ships
  the default-priority impl that returns the paginated index as
  the fallback. The `/` route itself is owned by the core
  delivery dispatcher; the post plugin no longer registers a
  Blueprint for it. Note: when a static homepage is configured
  the recent-posts list is no longer addressable; track that as
  a follow-up if a real site needs both.
- **Per-site landing page at `/`.** The delivery app's `/` is no
  longer a scaffold stub: each site now serves a paginated list
  of its recent published posts, newest first. Page size is
  configurable per site via `Site.extra_settings.posts_per_page`
  (default 10); navigation uses `?page=N`. Drafts, scheduled, and
  archived posts never appear, and posts are strictly scoped to
  the resolved site. The route ships from `bragi.contrib.post`
  (Blueprint `post_index_delivery`), so disabling the post plugin
  removes the landing page along with the per-post views.
  Configurable static homepages (Page-as-home), featured / pinned
  posts, and additional themes remain out of scope; tracked in
  follow-up issues.
- **`/healthz` liveness endpoint on both apps.** GET returns
  200 + `ok` after a `SELECT 1` round-trip; 503 + a logged
  exception when the DB ping fails. The example
  `compose.yml` healthcheck stanza on `admin` / `delivery`
  watches it via stdlib `urllib` (no extra image dep) so a
  wedged worker (process up, DB unreachable) flips to
  unhealthy and the `restart: unless-stopped` policy kicks
  in. Admin's auth guard now exempts `_healthz` so a probe
  doesn't bounce through `/auth/login`. Delivery's
  site-resolver tolerates the unknown `Host: 127.0.0.1` so
  the probe answers regardless of site state.
- **`compose.yml` documents `BRAGI_ENV`, `BRAGI_MAX_REQUEST_BYTES`,
  `BRAGI_ATTACHMENTS_MAX_BYTES`, plus the three new scheduler
  cadences** (`EMBEDS_RERENDER_EVERY`, `WEBMENTIONS_SEND_EVERY`,
  `ACTIVITYPUB_SEND_EVERY`). The example sets `BRAGI_ENV=production`
  on the web services so the dev-`SECRET_KEY` boot check fires
  when an operator forgets to set `BRAGI_SECRET_KEY` outside
  the compose-enforced `${VAR:?...}` shape.

### Changed
- **Posts now live under a per-site `Page` of kind `post_index`.**
  The hardcoded `/posts/` and `/tags/` URL spaces are gone. Each
  site has at most one `post_index` page (enforced by a partial
  unique index); post URLs become `<post_index_url>/<post-slug>/`
  and tag URLs become `<post_index_url>/tag/<tag-slug>/`. The
  alembic migration auto-creates a `slug="posts"` post_index
  page on every existing site, so legacy `/posts/<slug>/` URLs
  keep resolving without operator intervention. The new-site
  admin form ships with a "Create default /blog/ page" checkbox
  (default on) so greenfield sites get a post index without
  extra steps. Pages, the post listing, and individual posts now
  all flow through the page plugin's catch-all dispatcher; the
  post plugin no longer owns a delivery Blueprint or a
  `resolve_home` impl. Sites with no post_index page have no
  public post URLs (admin can still write/edit posts; they're
  not reachable until a `post_index` page exists).
- **Slug-rename redirects extend to pages.**
  `bragi.contrib.redirects` now discriminates Post vs Page on
  `on_post_updated`. Renaming a static page inserts an EXACT
  301 from old slug-path to new; renaming a `post_index` page
  inserts a PREFIX 301 that covers the index, every post URL,
  and the tag listings in one rule. (#130 follow-up extends the
  same hook to cover kind toggles and home_page_id changes; see
  the "Added" section.)
- **#124 update.** The post plugin's `resolve_home` fallback
  (the recent-posts list at `/`) has been removed. The page
  plugin's `tryfirst` impl still handles `home_page_id`; new:
  `bragi.contrib.theme_default` ships a `trylast` welcome-stub
  impl that guarantees a Response at `/` even when nothing else
  claims it. Visitors see "Welcome to <site>" with
  `Cache-Control: no-store` and a noindex robots meta until the
  admin sets a real home; the per-site dashboard surfaces a
  banner pointing at the fix.
- **Page admin: Kind selector with swap confirmation.** The page
  edit form gets a "Kind" dropdown (Static / Post index).
  Promoting a page to `post_index` while another exists on the
  site shows an intermediate confirmation; saving again with
  the implicit `acknowledge_swap` field demotes the previous
  post_index back to static in the same transaction.
- **`ContentTypeSpec.url_for` may return `None`.** Reflects the
  reality that posts have no public URL when the site has no
  post_index page. Sitemap and feed filter `None` entries; the
  internal-link rewriter renders the broken-link class for
  posts without a public URL.
- **JSON API now fires post lifecycle hooks.** `POST /admin/api/sites/<slug>/posts/`,
  `PATCH /admin/api/sites/<slug>/posts/<id>/`, and
  `POST /admin/api/sites/<slug>/posts/<id>/publish` previously
  skipped `on_post_updated` / `on_post_published` /
  `on_cache_purge`. The redirects plugin's slug-change auto-301,
  ActivityPub fanout, outbound webmention send, sitemap rebuild,
  search index, and post-cache invalidation all listen on those
  hooks. The API now dispatches them in the same shape the admin
  view does (snapshot before/after, `on_post_published` only on
  the actual draft -> published transition, `on_cache_purge`
  always). API-driven workflows now reach every subscriber an
  admin-UI edit reaches.
- **API list endpoint paginates.** `GET /admin/api/sites/<slug>/posts/`
  now accepts `limit` (default 50, max 100) and `offset` (default
  0) query params; response carries `total`. Previously returned
  the full post set in one payload, which would be slow + heavy
  on a site with thousands of posts.
- **FTS5 search pushes pagination to SQL.** The mixed post + page
  search previously loaded every matching row from both FTS
  tables, sorted in Python by bm25, then sliced to the requested
  page. On a paginated UI that only ever renders 10 to 20 hits,
  the matching set growing into the thousands was a steady-state
  waste. The two SELECTs are now a single UNION ALL with
  `ORDER BY rank ASC LIMIT :limit OFFSET :offset`; `total` comes
  from two MATCH-only `COUNT(*)` queries that skip the snippet()
  expansion and per-row hydration.
- **Search reindex commits once, not per row.** `_reindex_all`
  walked every published post and page and called the per-row
  `_index` helper, which opened its own `SessionLocal` and
  committed each upsert. On a corpus of N rows that meant 1
  DELETE pass + N transactions (so N fsyncs of the WAL and N
  busy-timeout windows for any concurrent reader). All FTS
  writes for the reindex now stage in one session with a single
  trailing commit; the per-row API stays single-transaction for
  the lifecycle-hook path.
- **Redirect PREFIX resolution pushes the match to SQL.** The
  resolver previously loaded every active PREFIX rule for the
  site into memory, then walked them longest-first looking for a
  startswith match. The predicate is now
  `substr(:path, 1, length(source_path)) = source_path` with
  `ORDER BY length(source_path) DESC LIMIT 1`, so SQLite returns
  the single winning row directly. `substr` was picked over LIKE
  so source_path strings containing `%` or `_` do not need
  escaping.
- **Session `last_seen_at` writes are throttled to one per
  minute per session.** Every admin page render fires N htmx
  subrequests; without the throttle each one wrote
  `UPDATE sessions SET last_seen_at = ...` on the same row. The
  new threshold (`LAST_SEEN_BUMP_INTERVAL = 60s`) matches what
  the "Last seen" column is actually for (operator visibility,
  not per-request precision) and keeps SQLite WAL pressure off
  during burst editing.
- **`AuditAction.TOKEN_USED` is downsampled to at most one row
  per token per 60s per worker.** A high-QPS bearer integration
  (a poller hitting the JSON API every few seconds) previously
  wrote one indistinguishable audit row per request. The new
  per-process map keys on token id with a 60s window; the first
  use in any window writes, subsequent uses are silent. Map is
  bounded at 4096 entries so a fuzzer presenting many distinct
  ids cannot grow it without limit. Per-request provenance still
  lives in the access log; the audit row's signal is "this
  token was active around time T".

### Fixed
- **Post admin's `published_at` stamps route through
  `naive_utcnow()`.** Two write sites in `post/admin.py`
  (`create_post`, `edit_post` first-publish transition) still
  used `datetime.now(UTC)` after the PR2 datetime-convention
  sweep, persisting a tz-aware value into a naive `Mapped[datetime]`
  column. SQLAlchemy + SQLite silently drop the tz on the way
  in, but reads-back-after-write across cached attribute access
  saw inconsistent shapes. Both sites now go through the
  centralised helper alongside every other timestamp emitter.
- **`ON DELETE` actions extended across the rest of the model
  graph.** The first FK-ondelete migration only touched the
  federation tables (#162-#166). This migration covers the other
  14 tables that hard-FK into `users` / `sites` / `posts` /
  `pages` / `attachments`. Cascade rules:
  - **CASCADE on user delete**: `user_identities.user_id`,
    `local_credentials.user_id`, `sessions.user_id`,
    `user_site_roles.user_id`. A future `cms user delete`
    sweeps the dependent rows in one statement.
  - **CASCADE on site delete**: `user_site_roles.site_id`,
    `redirects.site_id`, `site_aliases.site_id`, `tags.site_id`,
    `attachments.site_id`, `analytics_events.site_id`,
    `posts.site_id`, `pages.site_id`. Removing a site no longer
    leaves orphan tags / redirects / analytics rows behind.
  - **SET NULL on user delete** (history preservation):
    `audit_log.actor_user_id`, `audit_log.site_id`,
    `analytics_events.user_id`, `attachments.uploaded_by`,
    `page_revisions.editor_user_id`,
    `post_revisions.editor_user_id`. The forensic value of an
    audit row or page revision outlasts the user; nullable FKs
    drop attribution rather than the row.
  - **SET NULL on attachment delete**:
    `posts.featured_image_id`, `posts.og_image_id`,
    `pages.og_image_id`, `sites.default_og_image_id`. Removing
    an image leaves the post / page / site row in place with no
    media; the delivery template falls back to the site default.
  - **SET NULL on parent page delete (self-ref)**:
    `pages.parent_id`. Removing a parent page promotes children
    to root rather than cascading the delete subtree.
  Deliberately UNCHANGED (RESTRICT default): `posts.author_id`,
  `pages.author_id`, `sites.owner_user_id`. Deleting a user who
  still authors posts / owns sites must be blocked until the
  operator reassigns them; this is a policy decision, not a
  technical one.
- **Webmention moderation requires Editor role**, not just site
  membership. An "author" can write their own posts but shouldn't
  decide what other authors' posts surface as mentions
  (publication-surface decision, not authoring). Mirrors the
  post / page admin's editor-role gate.
- **SQLite `busy_timeout = 5000` now set on every connection.**
  The sidecar + admin + delivery workers all write to one
  SQLite file under WAL; without a busy_timeout, a write
  contention raised `database is locked` immediately. Five
  seconds matches the typical request timeout budget so a
  brief contention waits rather than 500's.
- **`scheduled-publish` no longer abandons the queue on a single
  failing post.** A hook failure on post N stopped commits for
  posts N+1..N; operators only noticed when a follow-up tick
  accidentally re-picked the same row. Each iteration now
  rolls back on failure and the loop continues; the summary
  reports `M published, K failed`.
- **`_ACTOR_CACHE` and `_ReplayCache` overflow eviction now hold
  a `threading.Lock`.** Under gunicorn threaded workers two
  concurrent overflows could race the `min(...)` + `pop`
  sequence (the looser worst-case is a KeyError; the lock
  removes the branch entirely).
- **Importers target the site's actual post URL, not hardcoded
  `/posts/<slug>/`.** Hugo / Ghost / WordPress importers built
  every redirect target as `/posts/<slug>/`. On a migrated v1.10.x
  site whose alembic auto-create kept the legacy "posts" slug the
  output happened to resolve; on a brand-new site (where the
  admin form's default seeds `slug="blog"`) every imported alias
  pointed at a URL the delivery app would 404. Importers now
  resolve through `post_url_for(site, slug, db=...)` (and
  `page_url_for(page, db=...)` for WordPress pages); when the
  target site has no POST_INDEX page yet the redirect emission is
  skipped entirely (post URLs are unreachable until one exists
  anyway). `post_url_for` and `post_index_page_for` grew an
  optional `db=` so importers can reuse their open session
  without the nested SessionLocal under SQLite's
  SingletonThreadPool rolling back pending writes.
- **Hugo aliases tolerate query / fragment suffixes.** `_normalise_alias`
  in the Hugo importer now strips a trailing `?...` and `#...`
  before normalising slashes, so an alias of
  `/old/?ref=tw#section` matches the redirect resolver's
  path-only compare.

### Security
- **JSON API enforces the admin's author-or-editor gate.**
  `PATCH /admin/api/sites/<slug>/posts/<id>/` and
  `POST /admin/api/sites/<slug>/posts/<id>/publish` previously
  only checked site membership: any token holder with `author`
  role + `post:write` scope could mutate another author's post,
  retract their published content, or trigger ActivityPub fanout
  / auto-301s on their behalf. The admin UI's `edit_post` gate
  (`(is_own and author) or editor+`) is now applied to the API
  surface via a new `_require_post_write_access(post)` helper.
  `GET /admin/api/sites/<slug>/posts/` similarly scopes the list
  to the caller's own posts when they hold only `author` rank;
  editor+ continues to see every author's posts. Multi-author
  deployments running v1.10.x with a minted token in `author`
  hands should treat this as a privilege-escalation fix.
- **CSRF exemption now requires a verified bearer token, not
  just an `Authorization` header.** The previous logic skipped
  CSRF whenever `Authorization: bearer …` appeared, on the
  theory that the header is CORS-restricted. That holds for
  cross-origin browsers, but a logged-in session POST with a
  smuggled junk Authorization header (via a misconfigured CORS
  proxy or any future middleware bug) would have bypassed CSRF
  while still authenticating via the cookie. `csrf.py` now
  gates exclusively on `g.api_csrf_exempt`, which
  `bragi.contrib.api_tokens.auth` sets only after a successful
  `verify()`. `register_csrf` moved to run after plugin
  `on_app_init` so the bearer middleware's `before_request`
  fires first.
- **HTTP signature verifier hardened against scope downgrade,
  body tamper, and replay.** The fediverse inbox accepted any
  signature whose listed `headers=` covered only a subset of the
  real request: a captured signature over a stale Date header
  could authenticate any path / method / body. Now requires the
  full minimum set (`(request-target)`, `host`, `date`, `digest`)
  in the signed coverage; rejects empty / missing `algorithm`;
  always verifies `Digest` against the body for POSTs regardless
  of whether the signer chose to list it; and a new module-level
  `_ReplayCache` (5-minute TTL, 4096-entry bound) drops
  duplicate `(keyId, signature)` presentations within the skew
  window.
- **SSRF guard on every outbound fetch driven by remote input.**
  The webmention inbox (`POST /webmentions`) and outbox sender
  fetched arbitrary URLs supplied by remote actors, and the
  ActivityPub inbox fetched the signer's actor document and
  posted to its declared inbox. Without guards, unauthenticated
  POSTs pivoted the delivery container into RFC 1918, loopback,
  and 169.254.169.254 (cloud IMDS) targets. New `bragi.core.http`
  module exposes `safe_get` / `safe_head` / `safe_post` +
  `is_public_url` that reject non-`http(s)` schemes and any host
  resolving to a private / loopback / link-local / multicast /
  reserved address (re-checked on every redirect). Rewired all
  six callsites (`webmentions/receiver.py:_fetch_source`,
  `webmentions/sender.py` HEAD/GET/POST,
  `activitypub/views.py:_fetch_actor`,
  `activitypub/sender.py:send_one`). The AP Follow handler now
  validates the remote's `inbox` and `endpoints.sharedInbox`
  URLs against the same guard before persisting the row, so any
  row in `activitypub_followers` is one we're willing to POST to.
- **Hard request-body cap (`MAX_CONTENT_LENGTH`) on both apps.**
  New `Settings.max_request_bytes` (default 1 MiB) wired into
  `bragi-admin` and `bragi-delivery`. Prevents a streaming-body
  attack on the public federation inboxes from OOMing a worker.
- **ActivityPub Accept now fires for cold Follow requests.**
  `_queue_accept` previously looked up the remote actor's inbox
  from `_ACTOR_CACHE`, which could be empty when called from a
  fresh Follow. The Accept got silently dropped; Mastodon
  retried to no avail. Now passes the already-fetched
  `remote_actor` dict directly.
- **`Undo Follow` requires the inner actor to match the signing
  actor.** Previously, any signed remote could send
  `Undo { object: Follow { actor: "https://victim/" } }` and
  delete a different remote's follower row. Now `inner.actor`
  must equal `outer.actor`; mismatch is logged and ignored.
- **`_ACTOR_CACHE` is bounded.** A fuzzing inbox attacker could
  grow the process-wide cache without limit. Caps at 1024
  entries and evicts the oldest on overflow.
- **App init refuses to boot in production with the development
  `SECRET_KEY`.** New `Settings.env` (default `development`) drives
  a startup check in both `create_admin_app` and
  `create_delivery_app`: if `env="production"` and `secret_key`
  is still the bundled dev sentinel, the factory raises. In
  development mode the same situation logs a loud WARNING and
  starts so `make dev` is unaffected. `compose.yml` continues to
  enforce `BRAGI_SECRET_KEY` via the `${VAR:?...}` shape; this
  check is the in-process backstop for bare `docker run` / k8s /
  podman deployments outside that gate.
- **SSRF guard now covers oEmbed / Bluesky / YouTube / IndexNow
  outbound calls.** The four embed-style providers issued bare
  `requests.get` / `requests.post` against allowlisted hosts and
  so bypassed the `_GuardedAdapter` redirect re-validation: a
  trusted endpoint that 302'd into RFC 1918 was followed
  silently. All four now route through `bragi.core.http.safe_*`.
  `safe_get` grew a `params=` kwarg for callers that build
  query strings against a fixed URL.
- **Admin `MAX_CONTENT_LENGTH` raised to admit attachment
  uploads.** The body cap landed at `Settings.max_request_bytes`
  (1 MiB) for both apps; `attachments_max_bytes` defaults to 20
  MiB, so any upload above 1 MiB was silently 413'd by Flask
  before the attachment view ran. Admin now takes
  `max(max_request_bytes, attachments_max_bytes + 64 KiB)` to
  cover the documented upload cap plus multipart overhead.
  Delivery's cap stays at `max_request_bytes` (only handles
  federation-inbox JSON bodies).
- **Pillow upload path bounds decompression-bomb risk.** Default
  `MAX_IMAGE_PIXELS` lowered to 50 megapixels (covers 8K source;
  rejects > 50 MP synthetic input regardless of file size), and
  `Image.DecompressionBombError` is now caught alongside
  `OSError` / `UnidentifiedImageError` so an oversized image
  produces a clean "could not probe" outcome instead of a 500.
- **`auth_local` login closes the unknown-user timing leak.**
  The wrong-password branch runs argon2 verify (~100 ms); the
  unknown-user branch short-circuited and so leaked email
  existence to anyone diffing response times. New
  `dummy_verify()` runs the same cost on the no-user path so
  both branches are timing-equivalent.
- **Federation tables now declare `ON DELETE` correctly so
  removing a Site / Post / User no longer leaves orphan rows.**
  `webmentions`, `webmention_outbox`, `site_keypairs`,
  `activitypub_followers`, `activitypub_outbox`, and
  `personal_access_tokens` shipped without explicit `ondelete`,
  so deleting an account or retiring a site left behind rows
  the admin UI could not surface or clean up, including
  outbound delivery queues that would have kept POSTing to
  remote inboxes after the site was gone. Cascade rules:
  `webmentions.site_id`, `webmention_outbox.{site_id,post_id}`,
  `site_keypairs.site_id`, `activitypub_followers.site_id`,
  `activitypub_outbox.{site_id,post_id,follower_id}`, and
  `personal_access_tokens.user_id` all `CASCADE`;
  `webmentions.post_id` is `SET NULL` to preserve moderation
  history when a post is removed. Migration
  `2026_05_18_0900_add_fk_ondelete` rebuilds the six tables in
  place (SQLite cannot ALTER a foreign-key constraint, so each
  table is recreated with the new constraints, rows are copied,
  and the old table is dropped).

## [1.10.0] - 2026-05-16

### Added
- **TipTap admin picker for internal links.** New
  "Internal link" toolbar button in the shared TipTap editor
  (used by post and page edit) opens an htmx-driven dialog that
  searches the active site's posts and pages by title or slug.
  Picking a card emits the `[text](post:<id>)` (or
  `[text](page:<id>)`) shape into the markdown body via a TipTap
  link mark, so authors don't have to type the typed-prefix form
  by hand. Clicking the button while the cursor is inside an
  existing internal link opens the dialog with the current
  target pre-highlighted, and picking a different target swaps
  the link mark's href while leaving the link text untouched.
  New picker endpoint
  `GET /admin/sites/<slug>/internal-links/picker` returns the
  fragment; v1 surfaces Post + Page (the two in-tree opt-in
  content types). Closes #115.

- **Internal links resolved at delivery time
  (`bragi.contrib.internal_links`).** New built-in plugin lets
  authors write `[text](post:my-slug)` or `[text](post:42)` in
  markdown bodies. At save time the link is normalised to its
  stable id and persisted as
  `<a href="/posts/<slug>/" data-bragi-link="post:<id>">`. A
  new Jinja filter `internal_link_rewrite` (piped over
  `body_html` in the post / page delivery templates) keeps the
  `href` in sync with the target's current slug at delivery
  time, so renaming a target updates every inbound link without
  re-editing or re-rendering the source posts. Targets that no
  longer resolve (deleted, slug-renamed away from both the
  author's typed key and the persisted id) render with the
  `bragi-link--broken` class instead of a stale href. Same-site
  only; cross-site resolution is a deferred v2 concern. The
  existing slug-change auto-301 covers CDN-cached stale renders
  during the `Cache-Control` window, so no fanout or backlinks
  table is needed for correctness. Closes #117.
- **`ContentTypeSpec.internal_link_prefix` and
  `ContentTypeSpec.resolve_internal_link`.** Optional public-API
  fields a content-type plugin sets to opt into the new
  `[text](<prefix>:<key>)` resolution path. Post and Page
  populate them in-tree; third-party content types do the same
  without touching `bragi.contrib.internal_links`.

## [1.9.1] - 2026-05-16

### Fixed
- **Task-runner sidecar's `cms` invocations now actually run.**
  `docker/scheduler.sh` was dispatching
  `flask --app bragi.apps.admin cms ...` since 1.8.0, but Flask's
  CLI autodiscovery only resolves factories named `create_app` or
  `make_app`, not `create_admin_app`. Every tick silently exited
  rc=2 with "No such command 'cms'"; scheduled-publish, pending
  embed rerenders, and the daily / weekly SQLite maintenance
  (`db analyze`, `db vacuum`) never ran in prod. Hotfix uses the
  explicit `module:factory` form
  (`flask --app 'bragi.apps.admin:create_admin_app' cms ...`)
  everywhere it was misspelled: `docker/scheduler.sh`,
  `compose.yml` comment, and `CLAUDE.md` operational docs.
  New `tests/core/test_cli_resolution.py` guards against
  regression. The `bragi-admin` gunicorn invocation was already
  correct (uses the explicit factory form).

## [1.9.0] - 2026-05-16

### Changed
- **Default site shell is now a registered theme
  (`bragi.contrib.theme_default`).** The `delivery/base.html`
  template moved out of `bragi/templates/delivery/` and into the
  new `theme_default` contrib package, registered under slug
  `"default"` via `register_theme` (the same hook a third-party
  `bragi-theme-foo` package would use). `ThemeAwareLoader` now
  resolves `Site.theme=NULL` to slug `"default"`, and also falls
  back to `"default"` when a site references an uninstalled slug
  rather than rendering an unstyled page. The site-edit theme
  dropdown's empty option is relabeled "Default theme" (was
  "Default (no theme)") and skips the redundant explicit
  `default` entry. No data migration: `Site.theme` stays
  nullable and NULL keeps its meaning ("use the implicit
  default"). Closes #111.

### Removed
- **Empty namespace-only packages `bragi.core.auth` and
  `bragi.core.content`.** Both contained only a docstring
  describing future structure with zero callers; reserving
  namespace for hypothetical future requirements is the pattern
  the tightened KISS rule warns against. Re-create when the
  shape is driven by real callers.

## [1.8.0] - 2026-05-16

### Added
- **External-content embeds plugin (`bragi.contrib.embeds`).**
  New markdown directive `::: embed <url> :::` resolves a URL
  at save time, dispatches to a provider, and inlines the
  rendered HTML into `body_html`. Readers never hit external
  services; the resolved HTML is cached in the body. v1 ships
  three providers: YouTube (click-to-load thumbnail by default,
  no Google network call on read until the reader clicks Play;
  iframe mode available via `BRAGI_EMBED_YOUTUBE_MODE=iframe`),
  Bluesky (official oEmbed), and a generic allowlisted oEmbed
  fallback for Vimeo, SoundCloud, and a handful of Mastodon
  instances. Failed save-time renders fall back to a styled
  `bragi-embed--pending` link card; the new
  `cms embeds rerender-pending` CLI (invoked every
  `EMBEDS_RERENDER_EVERY` seconds, default 600s, by the
  task-runner sidecar from #103) retries each with a more
  patient timeout and replaces the card in `body_html` on
  success. Per-call and aggregate save-time timeouts are tunable
  (`BRAGI_EMBED_OEMBED_TIMEOUT_PER`,
  `BRAGI_EMBED_OEMBED_TIMEOUT_AGGREGATE`). The
  `register_markdown_extension` hook is now wired end-to-end on
  both admin and delivery apps; the renderer reads its
  app-bound `MarkdownIt` from `app.extensions["markdown_renderer"]`
  when available and falls back to the bare CommonMark
  instance outside an app context (CLI scripts, importers).
  Click-to-load adds ~300 bytes of inline JS per page, injected
  by an HTML transform only when a CTO embed is rendered.
  Closes #104.
- **Task-runner sidecar container (`bragi-tasks`).** Replaces the
  one-shot `migrate` service in `compose.yml`. Owns
  `alembic upgrade head` on start, touches a `/data/.migrated`
  sentinel, then enters a sleeper loop dispatching `flask --app
  bragi.apps.admin cms ...` commands at configured cadences:
  `scheduled-publish` (default 60s, flips posts whose
  `scheduled_for` has elapsed from `scheduled` to `published`,
  firing the same `on_post_published` lifecycle hook the admin
  fires), `db analyze` (daily), `db vacuum` (weekly). The admin
  and delivery services gate their start on the sidecar's
  healthcheck (`test -f /data/.migrated`) rather than the prior
  one-shot's `service_completed_successfully`. Same image as
  `bragi-admin` since the `cms` CLI is registered there; ops
  surface is one extra service, no broker, no job queue. See
  `docker/scheduler.sh`. Closes #103.
- **`cms scheduled-publish` CLI command** (under the post plugin).
  Picks up posts with `status=scheduled` and `scheduled_for <=
  now()`, sets `status=published` and `published_at` (preserving
  any existing `published_at`), and dispatches
  `on_post_published` plus `on_cache_purge` so the search
  index, audit log, sitemap rebuilders, and any third-party
  subscribers stay in step with manual publishes from the
  admin. `--dry-run` lists what would change without writing.
  Idempotent.
- **`cms db analyze` and `cms db vacuum` CLI commands** (core).
  Thin wrappers around SQLite's `ANALYZE` and `VACUUM` (plus
  `PRAGMA wal_checkpoint(TRUNCATE)` after vacuum), invoked by
  the task-runner sidecar on a long cadence. Safe to run ad-hoc.
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
