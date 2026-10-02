# CONTEXT.md

Background and rationale for design decisions in bragi. `AGENTS.md`
says what to do; this file says why, and is the companion to read
before making a non-trivial change.

**Tracked in the repo since 2026-10-02**, pre-emptively rather than
after an incident. Sibling project mimir learned this the expensive
way: its rationale file was gitignored, so the defect class it
documents as the project's most recurring one was invisible to every
tool but one, and an agent that could not read it promptly shipped
two fresh instances. Design rationale is not workspace state, and
with several agents being trialled here the cost of it being
unreadable is the same cost, just not yet paid.

Two consequences of being public:

- **No operator surface here.** Hosts, addresses, deployment
  coordinates and the post-deploy smoke live in the untracked
  `_claude/CLAUDE.md`. This file was audited before being tracked
  and carried none of that, which is why it could ship nearly
  unchanged.
- **Some pointers lead outside the repo.** References to
  `_claude/MEMORY.md` (often written as just `MEMORY.md`),
  `_claude/specs/` and `_claude/plans/` point at local, untracked
  working notes, kept because they are accurate provenance for a
  decision. If you are reading from a clone and cannot find one, you
  are not missing a rule, only the story behind it.

## Goals and shape

bragi is a multisite content management system that ships as two
containers: an admin app (write API, editor UI) and a delivery app
(read-only public renderer). Both share one database and one plugin
system; only the middleware stacks and the set of registered
Blueprints differ. The deploy target is a single host running both
containers behind a reverse proxy (Caddy or nginx), with admin
mounted at its own subdomain.

The intended primary use:

- Replace the author's Hugo-based personal blogs (hugo-eng, hugo-nld)
  with a single multisite deployment.
- Provide a CMS that handles Ansible / Python / Terraform code
  blocks well (SEO-friendly server-side highlighting via Pygments).
- Stay small enough to be operable solo, with extensibility through
  plugins rather than forks.

What bragi is NOT:

- Not a SaaS / multi-tenant cloud product. Single-operator, multiple
  sites under that operator.
- Not a real-time / collaborative editor. One author at a time per
  post.
- Not multilingual at the post level. See "Single locale per site"
  below.
- Not a block-tree editor with custom block schemas. Markdown is the
  source of truth.

## Why Python / Flask (not Go, not Django)

The author evaluated Go (with templ, sqlc, chroma), Elixir / Phoenix
(LiveView as htmx's philosophical cousin), and Python. Final pick
was Python / Flask, matching the existing portfolio shape (mimir,
johnny). Reasoning:

- **Continuity with the portfolio.** mimir and johnny are already
  Flask + SQLAlchemy 2.0 + Pydantic. Reusing the stack means
  tooling, ops, lint config, and patterns transfer cleanly. A new
  CMS is not the place to also be learning a new language.
- **htmx pairs naturally with server-rendered Python.** Jinja2 +
  Flask views + htmx partials is a well-trodden path. Go would
  have worked; Phoenix LiveView would have been the most
  paradigm-aligned. Neither was worth the re-learning cost.
- **Plugin extensibility.** pluggy + entry points is a clean,
  battle-tested Python pattern (pytest, datasette). No Go or
  Elixir equivalent is as mature for the "ship a small core,
  extend via packages" model that bragi wants.

Django was specifically rejected: admin / i18n / ORM free out of
the box, but the ORM and request pipeline are heavier than this
project needs, and the portfolio Flask shape is the more
comfortable fit.

## htmx as the render strategy

All HTML is rendered server-side. htmx is used for partial swaps
on form submission, navigation within the admin, and small UI
flourishes on the public side. The data flow is:

1. First request from any client (browser, crawler, feed reader)
   gets a full HTML page. Crawlers always see the complete
   document.
2. htmx-initiated requests carry `HX-Request: true`; views
   dispatch to a partial template that returns just the swapped
   fragment.
3. No SPA-style routing on the public side. Every URL is a real
   URL with a real server-rendered response.

This is what "SEO as a first-class citizen" means in practice. No
JavaScript SPA shell to hydrate, no client-side router to confuse
crawlers, no separate "prerender" step.

The editor in the admin is the only place that ships meaningful
JavaScript (TipTap + markdown serializer). The delivery app keeps
JavaScript minimal: htmx for partial swaps, a CDN-lazy `vega-embed`
shim for the datasets feature's charts (loaded only when a
`::: dataset format=chart :::` block is present, with a no-JS table
fallback), and small per-feature enhancement scripts (the
pinned-posts carousel, future gallery / lightbox enhancements). The line we hold is "no SPA framework, no
client-side router, every URL renders server-side"; small
progressive-enhancement scripts that bolt onto a working no-JS
baseline are fine.

### Per-side JS posture

The "JS is progressive enhancement" framing above applies to the
**delivery** side. The **admin** side has a stricter posture: JS
is *required*. Operators who disable JavaScript get a degraded
admin experience and that is accepted. The line shifted there
because:

- Every admin mutation already routes through htmx partials, so
  there is no rendered no-JS alternative for an htmx-only flow to
  fall back to. The redirect-on-non-htmx branches that lingered in
  some views (`pin_toggle`, the attachments alt-text update) gave
  the *appearance* of no-JS support without delivering it: the
  surrounding page still required JS for any further interaction.
- Inline-edit cells, picker dialogs, and image-picker tabs are JS-
  only by construction. Once those landed, the partial no-JS
  paths became cosmetic vestiges.
- New admin features land without a no-JS fallback by default.
  Mutating routes return the partial unconditionally; htmx
  dispatch in *list views* still distinguishes full-page (cold
  load / direct nav / bookmark) from partial swap (htmx-driven
  pagination, filter changes). That is the canonical full-page
  contract, not a no-JS courtesy.

The delivery side keeps its full no-JS commitment: crawlers,
feed readers, and JS-disabled visitors all see complete,
server-rendered pages. The admin/delivery split here mirrors the
auth-and-attack-surface split already documented under "Admin on
its own host".

## Admin Content-Security-Policy and self-hosted assets (1.51.0)

The admin host sends a CSP; delivery deliberately does not (operator
content pulls arbitrary external images / YouTube embeds and needs its
own, looser policy, deferred). The design and its non-obvious
constraints:

- **All admin CSS/JS ships as static files under `/admin/static/`**, so
  `script-src` can carry **no `'unsafe-inline'`** (an injected inline
  `<script>` is the #1 XSS vector and is blocked). This is what the whole
  1.51.0 externalization arc (PRs #497-#503) was for; caching was the
  secondary win. `esm.sh` is the ONE allowed external script source: the
  TipTap editor and flatpickr import as ES modules with a dependency
  graph, and self-hosting them needs a JS bundler, which the project
  deliberately avoids (no build step). htmx, flatpickr's CSS, and the
  dataset-chart Vega bundles are self-hosted (vendored); esm.sh is the
  residue.

- **The config-island pattern for externalizing Jinja-coupled JS.** The
  editor's per-page config (textarea id, picker URLs, attachment prefix)
  used to be Jinja-interpolated into an inline `<script>`. To move the
  script to a static file without losing the config, the template emits a
  `<script type="application/json" id="...-config">{{ cfg|tojson }}</script>`
  island and the static module `JSON.parse`s it. A `type="application/json"`
  script is DATA, not executed, so CSP `script-src` does not police it,
  which is what makes this CSP-safe. This is the reusable recipe for any
  static-JS-with-per-request-config need.

- **The CSP is NOT maximally strict, and can't be, because of htmx.**
  `script-src` must include `'unsafe-eval'`: htmx 1.x evaluates
  `hx-trigger` filter expressions (e.g. `keyup[key=='Enter']`, used by the
  inline-edit cells) and `hx-on` handlers via `new Function()`. There is no
  htmx 1.x config to avoid this short of dropping those features; the eval
  sinks are author-controlled `hx-*` attribute values, not user-injected,
  so the residual risk is low. Dropping `'unsafe-eval'` would require an
  htmx 2 upgrade (its own migration) or rewriting every trigger filter.
  Separately, inline `onsubmit="return confirm(...)"` handlers (the
  destructive-action confirmations across ~20 admin templates) are allowed
  via a scoped `script-src-attr 'unsafe-inline'` (event-handler ATTRIBUTES
  only; inline `<script>` ELEMENTS stay blocked by the strict `script-src`).
  `style-src` keeps `'unsafe-inline'` for the inline `style="..."`
  attributes some templates still use. So the honest win is "no injected
  `<script>` executes," not "no unsafe-* anywhere."

- **Report-only by default, operator flips to enforce.** `BRAGI_ADMIN_CSP`
  (`report-only` | `enforce` | `off`, default `report-only`) sends
  `Content-Security-Policy-Report-Only` so violations log to the browser
  console without blocking; the operator clicks through the admin, confirms
  clean, then sets `enforce`. The Report-Only rollout is what surfaced the
  htmx-eval + inline-confirm + jsdelivr-flatpickr + resume-module findings
  that the code review and grep had missed (see MEMORY 2026-07-11).

## Headless reconciliation: deferred to v2 (or never)

The original design (recorded here as the dropped-but-reasoned
option) was "every read path has a JSON twin", so bragi would be
both an htmx app and a headless API at once. After the v1 content
surface settled it became clear this is the wrong shape for
bragi's actual situation:

- The htmx delivery app renders Jinja from SQLAlchemy rows
  directly; it does NOT consume a JSON view of itself. So the
  "dogfooding keeps the API honest" argument doesn't fire, because the
  API would have zero live consumers.
- The plausible future consumers (mobile reader, external Hugo
  rebuild, aggregator) don't actually want per-resource JSON.
  They want either the rendered HTML (which already exists) or
  the corpus in a form they can ingest (markdown + frontmatter,
  i.e. the inverse of the importers).
- Maintaining a speculative JSON contract with no consumer to
  enforce it ossifies fast: real consumers, when they show up,
  invariably want fields or shapes the speculative version didn't
  anticipate.

Concrete machine-readable surfaces that DO exist in v1:

- `/feed.xml` for feed-reader consumers.
- `/sitemap.xml` for crawlers.
- `/robots.txt`, `/.well-known/security.txt` for the rest of the
  infra side of SEO.

The corpus-export half of that story has since shipped, but as a
**CLI, not an HTTP endpoint**: `bragi export` (`bragi.core.export`)
writes each site's corpus as a Hugo-shaped tree of markdown +
frontmatter, the inverse of `bragi.contrib.import_hugo`. That covers
migrate-off and static-rebuild (run it, get the corpus). What remains
deferred is a *networked* read API (a per-resource JSON surface or an
export *endpoint*); the CLI is the right shape for the actual need
(own-your-data, one-shot export) and doesn't ossify a speculative wire
contract. If a live mobile-reader / aggregator consumer ever
materialises, revisit an endpoint then.

## Branded error pages, with a render that can't itself fail

The delivery 404 / 410 / 500 responses render through the active site's
theme (chrome, nav, footer) via `bragi.core.errors.render_error`, which
renders `delivery/error.html` (a core default that `{% extends
"delivery/base.html" %}`, so every theme brands the error page for free;
a theme ships its own `delivery/error.html` only to change the markup).
Two decisions:

- **Themed via the same loader as everything else, not a special path.**
  `render_error` just calls `render_template("delivery/error.html")`;
  the `ThemeAwareLoader` resolves `base.html` to the active theme exactly
  as it does for a post or page. No parallel theming mechanism for errors.
- **The error path can never itself error.** The themed render is wrapped
  in try/except with a minimal self-contained HTML fallback, and is
  skipped entirely when no site is resolved (an unresolved Host has no
  theme and no `base.html` to extend). So the three ways a themed error
  render could blow up, a broken theme, an unresolved Host, or the very
  failure that triggered a 500 (e.g. the DB down, making the nav query
  re-raise), all degrade to a plain 404/410/500 page rather than a nested
  exception. Error pages carry `noindex` so they stay out of search
  indexes (and the analytics sink already skips `status >= 400`).

## Multisite via Host header

One database serves many sites. Each site is a row in the `sites`
table with a `hostname` (e.g. `blog.example.com`) and zero or more
`aliases`. Every content table has a `site_id` FK.

Resolution at the WSGI edge: an early middleware reads `Host` from
the request, looks up the matching `Site` (or alias) with a small
in-process cache, and attaches `request.site` for downstream
handlers. Failed lookups return 404 immediately so misconfigured
DNS doesn't surface random sites.

Multisite via a `site_id` column on every row was chosen over
schema-per-site because the operational complexity of N SQLite
files (or N Postgres schemas) outweighs the row-level discipline.
The discipline is: every query joins on `site_id`; every integration
test runs against a multisite fixture so cross-tenant leaks fail
loudly. The Site is resolved once per request and threaded through.

## Admin on its own host

The admin app runs at its own hostname (e.g.
`admin.bragi.example`), not as a `/admin` prefix on a public site.
This isolates the admin's session cookies and auth posture from
any public site by construction. A theoretical XSS on a public
site cannot reach admin credentials because the cookies are
scoped to a different host.

The two apps share the database and plugin code, but session
state, CSRF tokens, and OAuth callbacks all live on the admin
domain.

## Single locale per site (multilingual deferred)

Each `Site` row has a `locale` field driving `<html lang>`, date
and number formatting, and default sort order. One locale per site
is the contract.

Multilingual was explicitly dropped after weighing complexity vs
the author's stated needs. The author maintains content in two
languages already (hugo-eng, hugo-nld) and prefers separate sites
per language over per-post translation tables. The decision is
reversible at the schema level: a `Post.locale` field can be added
later without disturbing existing rows if the call ever changes.

What is NOT supported as a result:

- `<link rel="alternate" hreflang>` cross-references between locales.
- A single post existing in N translations under the same slug.
- Locale switcher UI on the public site.

## Markdown source of truth, not block tree

Three editor options were considered:

1. Plain markdown editor (CodeMirror 6 + preview). Body persisted
   as markdown text.
2. Rich markdown editor (TipTap + markdown serializer). Body
   persisted as markdown text; admin authoring feels Ghost-like.
3. Block tree (TipTap / Lexical with JSON persistence). Body
   persisted as a JSON tree.

Chosen: option 2.

Rationale:

- **Hugo import is trivial.** Drop the `.md` in, parse frontmatter,
  no conversion.
- **Diff-friendly.** Plain text bodies survive in `git diff`,
  rsync, backups, dumps.
- **Migrate-off-friendly.** If bragi turns out to be wrong for the
  user, the bodies are already in the format every other CMS
  understands.
- **Editor-independent schema.** If TipTap turns out to be a poor
  fit, swap to CodeMirror with zero data migration.

The cost: per-block metadata (gallery layouts, alignment options)
isn't a first-class concept. Recovered via directive syntax
(`::: figure src=... :::` etc.) in the markdown, expanded by the
markdown-it-py parser.

Ghost's editor was the author's reference for "feels right". Ghost
1.x was markdown source-of-truth; Ghost 2.0+ (the Koenig editor)
is block tree but accepts markdown shortcuts. The author's intuition
that "Ghost is markdown" matches the 1.x experience; bragi makes
that intuition correct again.

## Why TipTap, not Lexical or Editor.js

- **TipTap** is built on ProseMirror, has a maintained Markdown
  extension, and is the most ergonomic to embed in a non-React
  admin. Picked.
- **Lexical** is what current Ghost uses. It's harder to embed
  standalone, less documented for our shape, and ties more
  tightly to React in practice.
- **Editor.js** is simpler but less polished; the slash-menu and
  inline-formatting UX is rougher.

A spike on TipTap is cheap. The data model doesn't change if the
spike fails: body is still markdown, just authored via a
different client.

## Plugin architecture and "built-ins are plugins"

The plugin system uses `pluggy` (pytest's plugin framework) for
hookspecs and hookimpls, and Python `entry_points` under the
`bragi.plugins` group for discovery. Each plugin is a small
Python package (or in-tree sub-package) that implements one or
more hooks.

The rule "built-ins are plugins by registration, not just by name"
is load-bearing: Post, Page, Tags, GitHub auth, redirects, the
importers, analytics, code highlighting, sitemap / robots /
security.txt generation, and the TipTap editor frontend are ALL
registered via the same `bragi.plugins` entry-point group third
parties use.

To disable a built-in, comment its line in `pyproject.toml`. There
is no internal-only fast path. This enforces:

- Every hook the core needs from a plugin is reachable by third
  parties; if it isn't, the API is incomplete.
- Building a new feature as a plugin produces working examples to
  read in `bragi/contrib/`.

Hot-load is intentionally not supported: plugins are discovered at
process startup. Install, restart, available. True hot-reload buys
little in CMS land and costs a lot in complexity.

The plugin boundary is enforced architecturally: each
`bragi/contrib/X/` may import only from `bragi.api`,
`bragi.core.models`, and `bragi.core` utilities. Never from a
sibling `bragi.contrib.*`. This keeps each plugin liftable into
its own PyPI package later without untangling cross-plugin
imports.

`bragi.api` is the public surface; `bragi.hookspecs` is internal.
Plugin authors only ever import from `bragi.api`. The hookspec
module is implementation detail, free to be reshuffled. The
full stability contract (what's covered, what isn't, and the
two-step deprecation policy) lives in `bragi/api.py`'s
top-of-module docstring (#190); the introspection surface
`bragi plugins list` prints every registered plugin with its
origin and hookimpl count, intended for operators triaging
"is this plugin even loaded?" and for plugin authors auditing
what they're shipping against.

## Split admin and delivery

Two app factories, two binaries, one database:

- `bragi-admin` (`bragi.apps.admin:create_admin_app`): write API,
  editor UI, OAuth callback handling, all admin Blueprints.
- `bragi-delivery` (`bragi.apps.delivery:create_delivery_app`):
  read-only public renderer. No write endpoints. Wires the
  resolve_redirect middleware and the analytics sink.

Both load the same plugin manager and call the subset of hooks
each app needs. The delivery app does NOT call
`register_admin_blueprint`; the admin app does. Same plugin code,
two contexts, different surfaces invoked.

Operational reasons for the split:

- **Blast radius.** A bug in the admin path (auth, editor, write
  flow) does not take down public reads.
- **Scaling shape.** Delivery is read-heavy and stateless beyond
  the database; horizontally scales. Admin is low-traffic and
  rarely the bottleneck.
- **Security posture.** Admin can sit behind a VPN, on a different
  network segment, or behind step-up auth without touching the
  public renderer.

In dev, `make dev` runs both (plus the dev-tasks loop) via an
in-repo Procfile supervisor at `scripts/run-procfile.py` with
colour-prefixed interleaved logs. We rolled our own after honcho
turned out to be unmaintained and broken on Python 3.14 (it imports
`pkg_resources`, removed from setuptools 81+).

## Auth: GitHub OAuth + Authlib + local bootstrap

Two auth paths:

- **GitHub OAuth** via Authlib. The author already has a GitHub
  account; the GitHub identity becomes the canonical link to a
  `User` row in bragi. Technically GitHub does OAuth 2.0 + a
  `/user` API call rather than full OIDC, but Authlib abstracts
  this; a later swap to Authentik or Keycloak (real OIDC
  providers) is a config change, not a rewrite.
- **Local bootstrap user.** Seeded via
  `bragi admin create-user`. Used to
  log in before OAuth is wired, and as an in-case-of-fire backup
  if the OAuth provider is unreachable. Stored in
  `local_credentials` with argon2id hashing.

A single `User` row can have N `UserIdentity` rows (one per linked
OAuth provider). Permissions live in `user_site_roles` so the same
user can be `admin` on one site and `author` on another.

Sessions are server-side: the cookie carries an opaque UUID; the
rest (CSRF token, flash messages, user_id) lives in the `sessions`
table. Logout invalidates immediately; admin can list and revoke
active sessions.

### Local-login brute-force throttle

The local-login POST is the one unauthenticated credential-guessing
surface (GitHub OAuth is provider-gated; PAT auth guesses a 256-bit
token). It already had constant-time verification (dummy-hash on the
no-user path) to deny email enumeration, but nothing capped guess
*rate*. `bragi.contrib.auth_local.throttle` adds a per-IP gate: a login
POST from an IP that has already logged `login_throttle_max_failures`
(default 5) `auth.login.failure` audit rows within
`login_throttle_window_seconds` (default 900) is rejected with 429 +
Retry-After before the credential check runs. Three decisions:

- **Keyed on IP only, not per-account.** A per-account lockout lets an
  attacker who knows an operator's email lock that account out
  (account-DoS). Per-IP stops the realistic threat (one host
  hammering) with no lockout weapon; a distributed attack on one
  account is left to argon2 + password strength. The cost is per-IP
  *collateral*: once an IP is throttled, even a correct password is
  refused until the failures age out (the gate runs before the
  check) - acceptable for a single-operator CMS.
- **Activates only when `trusted_proxy_hops > 0`.** The per-IP key is
  only safe when `remote_addr` is the real client. Behind the
  documented reverse-proxy deploy with hops=0, `remote_addr` is the
  *proxy's* address for every client, so a per-IP bucket collapses to
  one global bucket and an attacker's 5 failures would lock everyone
  out (a self-inflicted global-lockout weapon, the review's near-
  blocking finding). Rather than ship that as the default-on posture,
  the gate runs only when a trusted proxy is declared (or, in a
  direct-bind deploy, when the operator sets hops explicitly); the
  admin app logs a warning in production if the throttle is enabled
  while hops=0 so the operator knows it is inactive. This fails toward
  "no throttle" rather than "global lockout."
- **Counts only login-form failures.** `auth.login.failure` is also
  written by the change-password wrong-current-password path and the
  GitHub OAuth email-collision path; counting those would let a
  logged-in operator fumbling their current password throttle their
  own logins. The count filters on the login form's own
  `reason == "invalid-credentials"`, so only genuine login-form
  guesses feed the gate.
- **Approximate under concurrency (accepted).** Check-then-act isn't
  atomic and holds no lock, so a burst of near-simultaneous requests
  at the boundary can slip a few extra past the "5" before the IP
  locks. Bounded by worker count and argon2-slow per guess; not worth
  a lock. The same multi-worker blind spot the `:memory:` fixture
  can't reproduce.
- **Reuses the audit trail as the attempt store.** Every failed login
  already writes an `auth.login.failure` audit row with the IP and
  timestamp, so the throttle is a `COUNT` over those rows: no new
  table, no new migration. The one coupling is retention (keep audit
  rows at least as long as the window).
- **A blocked attempt records a distinct `auth.login.throttled`
  action, not another failure.** If throttled attempts counted, a
  persistent attacker would hold the window open forever; the distinct
  action means the block lifts once the real failures age out.

### GitHub sign-in: button, account linking, and no auto-link

GitHub OAuth landed early but only the protocol half (the login +
callback flow, the `UserIdentity` model). Making it first-class meant
adding the two front-door pieces, with a few decisions worth recording.

- **Generic over the registry, not GitHub-hardcoded.** The login-page
  buttons and the Connections page read the registered
  `OAuthProviderSpec`s from the registry and render whatever is
  present, so a future Authentik/Keycloak/Google provider lights up the
  same UI with no template change. Two fields were added to the spec to
  make that possible: `login_endpoint` (so the UI can `url_for` the
  provider's start route without knowing its name) and `is_configured`
  (so an unconfigured provider, whose login route 503s, is not offered
  as a button).
- **No auto-link by email, ever, resolved via an explicit link flow.**
  The callback deliberately refuses to bind a GitHub login to an
  existing local user just because their emails match (that was the
  SEC-H1 admin-takeover primitive). That left a dead-end: an operator
  whose GitHub email matched their local account could never combine
  the two. The Connections page is the sanctioned path: log in with
  your password, then link GitHub. The link intent is carried in the
  *session* (set only when a real user starts it), never a query param,
  so a crafted `?mode=link` callback can't silently attach an identity
  to someone. Linking refuses an identity already owned by a different
  bragi user (the `(provider, provider_user_id)` unique key is the
  guard).
- **Unlink can't lock you out.** Removing an identity is refused when it
  would leave the user with no password and no other identity. The last
  remaining sign-in method is always protected; the button is disabled
  in the UI and the rule is re-checked server-side.

## Federation: ActivityPub + Webmentions

Content distribution is in v1 via two built-in plugins:
`bragi.contrib.activitypub` (#148) for fediverse reach,
`bragi.contrib.webmentions` (#147) for IndieWeb mentions. Both
ride the standard `bragi.plugins` entry-point and can be disabled
by commenting their line in `pyproject.toml`.

### Why one ActivityPub actor per site

Each site becomes a follow-able actor addressable as
`@<slug>@<hostname>`. The multisite shape already maps one
hostname to one site; extending to one site to one actor keeps the
identity surface trivial: one keypair, one inbox, one outbox, one
follower set per site. A Mastodon user follows the site, not an
individual author.

Per-author actors were considered and rejected. The CMS's identity
surface is `User` + `user_site_roles`; attaching ActivityPub
identities to users would multiply keypair management, fork the
fanout path on cross-site author moves, and make the publish flow
author-aware in a way it currently isn't. Per-post actors don't
reduce to a real use case ("follow this one post" collapses to
following the site or a topic).

### v1 scope: publish out, Follow in, no replies

- **Outbound publish.** `on_post_published` queues a Create + Note
  per follower into `activitypub_outbox`, and scans the rendered
  HTML for outbound links to send webmentions to. The AP outbox
  is one row per follower so a single bad inbox retries without
  disturbing the rest.
- **Inbound AP.** Signed POSTs to `/actor/inbox` handle Follow and
  Undo Follow. Like, Boost, reply, DM are ACK'd and dropped. There
  is no comment system to thread inbound Create Note replies into,
  and federation does not change the "no comments" decision.
- **Inbound webmention.** `POST /webmentions` validates that the
  source actually links to the target (W3C §3.2.1), extracts an
  h-card subset for the author byline, persists pending. Display
  requires Editor approval via the admin; un-moderated mentions
  never reach the public surface.

Out of v1 (no namespace reservation): outbound Like / Boost /
Reply, multi-actor per author, DM-style ActivityPub, full
microformats2 parsing on the webmention receiver.

### Notification timing: debounce the crawl/mention pings, keep federation prompt

The three outbound notifiers split by freshness-sensitivity, which is
why two are debounced (1.37.0, #443/#447) and one is not:

- **IndexNow** (a stateless "re-crawl this URL" ping) and **outbound
  webmentions** (a "I linked to you" nudge the target re-fetches at
  leisure) have no freshness urgency, so they are taken off the request
  path and held in a leading-edge debounce window (`not_before`,
  default 300s, one row per URL / per `(post,target)`): N edits in the
  window collapse to one send, protecting publish latency and (for
  IndexNow) the submission quota. Webmentions additionally drop a
  PENDING row whose link was removed before the window closed (no
  send-then-retract). The `bragi-tasks` worker drains due rows.
- **ActivityPub stays prompt and is NOT debounced.** It only fanouts on
  the first-publish transition (`on_post_published`), not on edits, so
  there is no edit-spam to coalesce; and a federation `Create` should
  reach followers' feeds promptly, a hold-off window would just make a
  published post appear minutes late. Debouncing AP is the wrong
  tradeoff; a future "make these consistent" pass should not apply it.

### Discovery and security posture

WebFinger at `/.well-known/webfinger?resource=acct:@slug@host`
resolves the site handle to its actor URL; the actor document at
`/actor` advertises inbox, outbox, followers collection, and the
public key fragment. `<link rel="webmention">` is injected into
every delivery `<head>` so external senders find the inbox without
configuration.

Every remote fetch (webmention source validation, AP actor
resolution, mention endpoint discovery) routes through one
hardened fetcher: private-IP blocklist, scheme allowlist, redirect
chain cap, and a `MAX_CONTENT_LENGTH` cap set on both apps so the
federation inboxes can't be OOMed by an oversized POST.

## Redirects as a first-class subsystem

Redirects are not "a table the importer happens to write to." They
are a core subsystem owned by the `bragi.contrib.redirects`
plugin, with the `resolve_redirect` hookspec and resolution
middleware living in core.

Sources of redirect rows:

- Importers (Hugo aliases, Ghost permalink changes, WordPress slug
  rewrites): on import, every source URL becomes a redirect row.
  URL preservation is non-negotiable; importing without redirects
  destroys SEO on day one.
- Slug changes on existing Post / Page: an auto-301 from the old
  slug to the new is inserted automatically. Opt-out flag for the
  "fixing a typo in a draft" case.
- Manual admin entries; bulk CSV import / export.
- Plugins via `resolve_redirect` for dynamic computed redirects
  (legacy URL patterns too numerous to enumerate as rows).

Resolution order in the request pipeline:

1. Live content by path (Post / Page lookup). Content wins by
   default; otherwise renaming a slug could accidentally orphan
   a post.
2. Plugin `resolve_redirect()` hook; first non-None wins.
3. Redirects table: exact, then prefix (longest-first), then regex
   as last resort.
4. Follow chain up to 3 hops; loop detected gets logged and served
   as 500.
5. Still nothing: real 404. `status_code=410` short-circuits as
   "Gone".

Why first-class:

- Importers are blocked on it. Day-one shippable feature.
- Slug renames are common during authoring; a non-first-class
  redirects system makes "rename this draft" a fraught action.
- 301 and 410 are the cheapest SEO investment per line of code.

## 404 triage: detect, then act on dead URLs

`bragi.contrib.notfound` records the 404s the delivery app returns
and gives the operator a per-site admin page to act on them
(redirect, mark Gone, create the missing content, or dismiss). A few
decisions are worth recording.

**Why a separate plugin, not part of redirects.** Detecting and
triaging 404s is a distinct concern from the redirect subsystem
(which resolves and stores redirects). The notfound plugin *reads*
redirects and content and *creates* redirects/content by
deep-linking into their existing admin forms (by `url_for` endpoint
name), so it never imports a sibling contrib plugin and the
contrib boundary holds. Keeping it separate also means disabling 404
recording is one commented entry-point line, independent of
redirects.

**Why synchronous best-effort recording, not the debounce queue.**
The recorder is a delivery `after_request` that upserts one row per
404 (coalesced by `(site_id, path)` via SQLite `ON CONFLICT DO
UPDATE`). CONTEXT's "Database write concurrency" tier 2 says push
read-path writes to the `bragi-tasks` worker, and that remains the
escape hatch. But after the scanner blocklist drops the probe noise,
surviving 404 volume is low and every re-hit of a path is a single
in-place UPDATE, not a new insert. Delivery already writes on the
read path (the redirect hit-bump), so a best-effort UPSERT with
`run_with_write_retry` matches existing precedent for far less code
than standing up a `not_before` queue + a CLI drain. The recorder is
marked with a `ponytail:` comment naming the offload as the upgrade
path if 404-write contention ever shows. A DB failure logs and the
404 is served regardless. Known ceiling: the blocklist only catches
*known* scanner patterns, so a crawler probing many *distinct* novel
paths still inserts one row (and one write) each, unbounded. That is
acceptable at bragi's personal scale (the intended use); a busy public
deploy would want a per-site open-row cap or a stronger blocklist. Not
built now (a cap would cost a COUNT on the write path); the trigger to
build it is a real deploy seeing 404-row floods.

**Why the scanner blocklist filters before the write.** Public 404
traffic is dominated by vulnerability scanners hitting random paths.
Recording all of them would flood the table and bury the real dead
links. `Settings.notfound_blocklist` (fnmatch globs, case-folded,
env-overridable as JSON) drops those paths before the recorder
writes, so cost and cardinality stay bounded. `.well-known/*` is
never blocked (security.txt, webfinger live there).

**Why "handled" is computed, not a stored status.** A row's
lifecycle is only `open` -> `ignored` (dismiss). There is no
`resolved` state: the admin list hides any open row whose path an
active *exact* redirect now covers (a correlated `NOT EXISTS`,
pagination-correct). That is how a row disappears after you
deep-link-create its redirect, without threading state back through
the deep-link, and it keeps the redirects table the single source of
truth for what has been redirected. Prefix/regex redirects are
deliberately not consulted here (exact membership only); the
create-content case is handled by explicit dismiss.

**Why suggestions are leaf-slug matches only.** Per detected 404,
the view proposes at most one fix: an exact published-slug match at a
different URL (the wrong-prefix case), else an exact archived-slug
match (informational, "you archived this"), else a `difflib`
fuzzy-match against published slugs (typo/rename). All stdlib, no
dependency; the pure `suggest()` function takes pre-built candidates
so URL construction stays in the view. Full path-similarity and an
audit-log deep-match against hard-deleted content are deferred until
wanted.

## Auto-nav from the page tree

The public-side navigation menu is derived at render time from
the page tree: every published page with `show_in_nav=True`
appears as a nav item, ordered by `(menu_order, title)`,
capped at one level of children, with the page mapped to
`site.home_page_id` dropped.

**Why pages-as-menu instead of a separate `menus` table.** v1
is a single-operator CMS with a small page set per site; the
operator-facing complexity of "now you also have to set up a
menu and remember to add pages to it" out-weighs the
flexibility a separate menus table buys. The two per-page
columns (`show_in_nav`, `menu_order`) are forward-compatible
with a full menu builder: in a hypothetical v2,
`show_in_nav` becomes "show in default menu", and `menu_order`
stays as its sort key. The deferred surface is reserved in the
spec under "Deferred surfaces".

**Why home-page-promoted pages are auto-hidden.** The brand
link in every theme already routes to `/`, which resolves to
whatever page `site.home_page_id` points at. A nav item for
that same page would be a visible duplicate. The auto-hide
matches the project's pattern of "operator does the obvious
thing (promote a page to home), system handles the implication
(don't duplicate the link in the nav)". Same shape as the
auto-301 inserted on slug changes.

**Why one level deep.** Two levels (top + direct children) is
the sweet spot for header navigation UX in this kind of CMS.
Multi-level submenus introduce hover-vs-tap accessibility
fiddling and crowd the chrome. Grandchildren remain reachable
by URL and by their parent's body content; the nav simply does
not surface them.

## Admin chrome: left rail with a meaningful section taxonomy

The admin navigation (distinct from the public auto-nav above) is a
fixed left rail with labeled section groups, redesigned from a two-row
top nav in 1.38.0. The redesign was driven by an IA problem, not a
visual one: the old nav filed seven unlike destinations (posts, pages,
media, datasets, analytics, team, webmentions) under one `content`
bucket, so the grouping carried no meaning and the bar didn't predict
what was inside.

The fix is a taxonomy where each section name predicts its contents:
**write** (what you author), **reach** (outward/inbound signals),
**manage** (config + admin + tools) for site-scoped items, **platform**
for global ones, and the account menu for personal items. The rail
renders the section name as a typographic label, so the grouping does
the navigational work; that is why the section vocabulary is fixed
rather than free-form (a stray section name would render as its own
orphan group). Ordering is pinned in `_SECTION_RANK` so the
muscle-memory "day's work first, administration last" survives a contrib
adding a new item.

Why a rail and not a refined top bar: at ~11 site destinations a
horizontal bar can't show labeled groups without wrapping; a vertical
rail makes group labels legible for free and scales as features land.
Why it stays zero-JS (the switcher/account menu are `<details>`, the
mobile rail is a CSS-only off-canvas toggle): the admin already commits
to JS-required for *mutation* flows, but the chrome itself is the one
surface that should always render and navigate without it, and the
CSS-only pattern is less code than a JS drawer. The dark rail continues
bragi's existing chrome identity rather than rebranding.

## Data model choices

A few choices worth recording the reasoning on:

- **Post and Page are separate tables**, not joined inheritance.
  Divergent fields (`scheduled_for` semantics, `parent_id` on
  pages, future fields likely to skew) make inheritance more
  costly than helpful. Shared abstractions live in mixins
  (`PublishableMixin`, `SeoMixin`) and the content-type registry.
- **Per-content-type tag junctions** (`post_tags`, `page_tags`)
  rather than a polymorphic `taggings` table. Real FKs, real
  cascades, sane indexes. Plugin-added content types follow the
  same pattern.
- **`body_html` cached next to `body_markdown`**, regenerated on
  save. Avoids markdown parsing on every read. A "rerender all"
  admin command handles renderer-config changes.
- **`source_id` indexed on `posts`, `pages`, `attachments`** so
  importers are idempotent without a separate mapping table.
  Re-imports look up by `(site_id, source_id)`.
- **`extra_settings` as a JSON blob on `sites`** rather than a
  key-value table. We never query "sites with setting X"; we
  always read all settings for one site. JSON keeps the schema
  flat for plugin extensibility.
- **AuditLog target is polymorphic** via `(target_type, target_id)`,
  weakly referenced. SQL FK can't enforce across tables;
  trade-off acceptable for audit data and lets every plugin emit
  audit rows without schema changes.
- **No soft deletes.** Content has an `archived` status; redirects
  have `active=false`; users have `is_active=false`. Real deletes
  are real; audit_log records what was deleted.
- **AnalyticsEvent is one table for v1.** Rolling monthly tables
  (`analytics_events_2026_05`) become the upgrade path once
  events / month gets large enough that table-scoped vacuum and
  time-range queries hurt; the writer can be swapped to
  multi-table without touching the read API.

## PyPI publication as bragi-cms (from v1.27.0)

PyPI namespace `bragi` is held by The Managarm Project's IDL
(MIT-licensed). We ship as `bragi-cms` (Pillow precedent:
distribution name differs from import name). Operators write
`pip install bragi-cms==X.Y.Z`; the import path stays
`import bragi`. The `bragi`, `bragi-admin`, and `bragi-delivery`
console scripts install normally.

Publication happens automatically on every GitHub Release via
`.github/workflows/release.yml` using Trusted Publishers (OIDC;
no API token in GitHub secrets). v1.26.0 was the last git-tag-only
release. From v1.27.0 the canonical pip install path is:

```sh
pip install bragi-cms==X.Y.Z
```

The canonical Dockerfile shape becomes:

```dockerfile
FROM python:3.14-slim
RUN pip install --no-cache-dir bragi-cms==X.Y.Z
```

replacing the previous build-from-source `pip install --no-deps -e .`
pattern. The GHCR container images remain the primary deploy
artefact for operators who want pre-built images; PyPI broadens the
install surface to operators who prefer pip-based deploys.

**Migration path for existing deployments.** The Dockerfiles
(`docker/admin.Dockerfile`, `docker/delivery.Dockerfile`) copy
`alembic/` and `alembic.ini` from the source tree alongside the
editable install. From v1.27.0 the migrations ship inside the
wheel at `bragi/alembic/` and `alembic.ini` uses
`script_location = bragi:alembic` (package-relative); both
`bragi db upgrade head` and the bare `alembic upgrade head` (with
the project-root `alembic.ini`) continue to work.

## Container deploy

Two images on GHCR: `bragi-admin:vX.Y.Z` and
`bragi-delivery:vX.Y.Z`. Built by the `publish-docker` job in
`release.yml`, which runs sequentially after `publish-pypi` on
every GitHub Release. Images consume the PyPI-published
`bragi-cms` wheel via `pip install bragi-cms==${BRAGI_VERSION}`
(build-arg injected from the release tag); no source checkout
inside the image, no multi-stage poetry export. Base image is
`python:3.14-slim`. Image tag derives from the git tag, per
portfolio versioning convention; manifest `pyproject.toml`
`version` is the in-image record of what got shipped and surfaces
in the admin footer. The image is byte-equivalent to a direct
`pip install bragi-cms==X.Y.Z` on the same base.

`docker.yml` (which previously fired on tag push) is deleted from
v1.27.1; container builds now gate exclusively on the GitHub
Release event so PyPI propagation precedes the Docker `pip install`.

### Why the propagation gate probes with `pip download`, not curl

The `publish-docker` job's `Wait for PyPI propagation` step polls
with `python3 -m pip download --no-deps` against the version it
is about to install, not a `curl` against PyPI's JSON metadata
endpoint. The earlier curl probe (v1.27.0 through v1.27.5) ran
into a real CDN-state divergence: PyPI's JSON endpoint serves
200 for the new version well before pip's CDN node behind the
install path can resolve the wheel. v1.27.2, v1.27.4, and
v1.27.5 all saw the docker `pip install` race the propagation
even at a 120s budget (doubled from 60s in v1.27.3 after the
first occurrence). v1.27.6 observed the same lag from a fresh
data point: parallel matrix jobs against the same publish-pypi
completion picked different CDN nodes, one resolving on attempt
1 (~2s), the other on attempt 2 (~11s).

The probe is identity-typed with the install command (`pip
download` of the same wheel), which removes the JSON-endpoint
divergence. Budget raised from 120s to 600s (60 x 10s). The
general lesson holds: **for any "wait for external state" gate
in CI, probe what the next step will do, not a cheaper proxy.**

**Caveat learned on v1.33.0 (necessary, not sufficient):** the
gate runs `pip download` on the *runner*, but the real
`pip install` runs inside the *buildx build container*, a
different network egress that resolves through a different CDN
node. On v1.33.0 the runner-side gate went green yet buildx's
in-container `pip install bragi-cms==1.33.0` still failed with
"No matching distribution found (from versions: ...1.32.0)" ~1
minute later: the wheel had propagated to the runner's node but
not yet to buildx's. So a green gate reduces but does not
eliminate the race. The backstop is the operator re-run: once
PyPI has fully propagated (minutes later), `gh run rerun
<id> --failed` rebuilds only the failed `publish-docker` jobs
(publish-pypi is preserved, so no duplicate-version upload).
Do NOT treat a green propagation gate as proof the image build
will resolve; if publish-docker fails on "No matching
distribution," it is this race, and the fix is wait-and-rerun,
not a code change. A future hardening would move the probe
*inside* the same buildx context (or add a retry around the
build's `pip install`), so the gate and the consumer share a
CDN path.

## Deferred surfaces

Specific things explicitly OUT of v1, with namespace reserved:

- **Remote storage backends.** The local-disk storage backend
  is in-tree (`bragi.contrib.attachments`) and the media-library
  surface around it (renditions, bulk alt-text editing, TipTap
  embed picker, `<picture srcset>` at delivery) shipped along
  with it. The remaining gap is cloud storage: S3 / R2 / GCS
  ship as third-party plugins via the reserved
  `register_storage_backend` hook. Same shape for image
  processors via `register_image_processor` (Pillow ships
  in-tree; alternative processors land as plugins).
- **Datasets v1 shipped** (`bragi.contrib.datasets`). Per-site
  registry of uploaded data files (DuckDB, CSV, Parquet, SQLite)
  queried via DuckDB. Admin explore console (author-only) with
  saved queries; `::: dataset :::` markdown directive bakes tables,
  Vega-Lite charts, and scalars into post/page bodies at save time;
  re-upload re-renders referencing content synchronously. Sources
  are upload-only in v1: the operator uploads a file to the admin
  registry. `register_dataset_source` is reserved for
  remote/plugin-fed sources (a git-tracked file, an S3 key, a
  periodic fetch) and is NOT invoked in v1. The bake-and-rerender
  design was chosen over a delivery-time filter because `body_html`
  is already the render cache and the rerender pass (on save or on
  explicit `bragi datasets rerender`) is the invalidation path:
  adding a delivery-time query layer would introduce a second cache
  with no additional consumer. The "small notebook" in-editor
  authoring experience (running SQL in the editor, inserting the
  result interactively) is still deferred.
- **Notion / Substack / Medium importers.** Opportunistic, may
  appear as separate `bragi-import-*` packages once interest
  shows.
- **Block-tree authoring.** Discussed and rejected for v1. If
  someone ever wants it, the `register_block_type` namespace is
  reserved; adoption would require schema changes to
  `posts.body_*` and is non-trivial.
- **Real-time collaboration.** Not on the roadmap. Single-author
  editing per post.
- **Comments / discussion.** No in-tree comment system. AP
  inbound `Create Note` replies are dropped; webmentions display
  as mentions after Editor moderation, not threaded replies. See
  "Federation: ActivityPub + Webmentions" section above.
- **JSON content API.** Originally planned as a parallel
  read-side surface (see "Headless reconciliation: deferred to v2
  or never" above). Closed unresolved; the right v2 shape if it
  ever returns is a corpus-export endpoint, not per-resource
  JSON. The static `feed.xml` / `sitemap.xml` already cover the
  feed-reader / crawler cases.

## LinkedIn importer: why two-phase

The LinkedIn importer is the first bragi importer that does NOT
run as a single-shot "blast through the source". Plan-review-apply
is mandatory; there is no `--yes` flag. Two pressures drive this:

1. **The structural overwrite + narrative preserve heuristic
   silently misfires when the operator renames a job title in
   LinkedIn between imports.** `(company, role, start_date)` is
   the match key for Position; a renamed role becomes a
   `remove` + `add` proposal pair instead of an `update`,
   wiping the operator's `description_markdown` if applied
   blindly. The review surface lets the operator spot this
   case and reject the spurious change.

2. **Re-syncing a CV from LinkedIn is naturally a deliberate
   activity.** The operator runs it monthly or quarterly and
   wants to glance at every change before it lands. A
   "blast through" mode would be reached for by reflex on the
   second import and defeat the safety the review surface
   provides.

The plan file (CLI) and the stash directory (admin UI) are the
two persistence shapes for the plan between phases. Stable
proposal ids (sha256 of section + match key + payload) let the
operator's selection in either form survive editing.

The `ResumeData` schema was promoted from
`bragi.contrib.page.resume` to `bragi.api` so the LinkedIn
plugin (and any future resume-source plugin) can build
instances without crossing the contrib-to-contrib boundary.
This matches the existing precedent: `NavNode`, `Crumb`, and
the various spec dataclasses already live in `bragi.api`.

## Database write concurrency and the SQLite single-writer ceiling

bragi runs on SQLite (WAL mode) behind multi-worker gunicorn. The
hard constraint: **SQLite allows exactly one writer at a time** (WAL
only lets readers run concurrently *with* that one writer, not
multiple writers). A single content edit fans out into many writes:
the content row, FTS reindex, redirect rows, internal-links edges,
ActivityPub outbox fanout, webmention sends, analytics, redirect
hit-counters. Each is a potential writer, so write-lock contention is
inherent; the architecture's job is to keep load off the ceiling.

The classes of contention, worst first:

- **Intra-request cross-connection deadlock** (the v1.34.3 bug): two
  connections *inside one request* each hold a lock the other needs;
  `busy_timeout` can't resolve it (both wait forever). Eliminated by
  the **one-connection-per-request** rule: every write in a request
  or its lifecycle hooks goes through the supplied session, never a
  fresh `SessionLocal()`. This is now enforced (CLAUDE.md rule + a
  spy guard test); the redirects subscriber was the last violator.
- **Multi-worker overlap**: two requests writing at once. Mitigated,
  not eliminated, by a generous `busy_timeout` (wait, don't error)
  and a retry-on-`SQLITE_BUSY` wrapper.
- **Long-held write transactions**: holding a write open across slow
  work (rendering, federation HTTP, image processing) widens the
  contention window. Keep writes short; do slow work first, then
  open+commit.

The tiered response (the standing plan, least to most invasive):

1. **Discipline (cheap, ongoing).** One connection per request; a
   single commit per request (hooks flush, the caller commits once at
   the end, making content+index+redirect+edges one atomic
   transaction; this shipped in 1.36.2 as the fix for the dropped-write
   bug, issue #430, and is now a CLAUDE.md rule); WAL + busy_timeout +
   `synchronous=NORMAL` on every connect;
   a busy-retry wrapper.
2. **Offload write-heavy work to the `bragi-tasks` worker (the real
   SQLite-preserving lever).** Route non-critical, write-heavy hooks
   (FTS reindex, AP outbox fanout, webmention sends, analytics, and
   the redirect hit-count bump which writes on every *resolution*, a
   read path) off the request and onto the single background worker.
   Concurrent writers then collapse to "small request-path content
   writes + one serialized background writer."
3. **PostgreSQL is the ceiling's escape hatch, not the next step.**
   Postgres (MVCC, row-level locks) makes `database is locked`
   impossible by construction. SQLAlchemy 2.0 keeps the ORM portable;
   the cost is ops (run a server, losing the single-file simplicity
   this project deliberately chose) plus the SQLite-specific bits
   (FTS5 → `tsvector`, the `substr()` prefix-redirect query, pragmas,
   alembic dialect deltas). Adopt only when persistent request-path
   contention survives tiers 1-2, or a genuine concurrent-author /
   high-federation-traffic use case materialises. Document the
   trigger ahead of time so the call isn't made mid-outage.

**Precedent: sibling `mimir` walked this exact road and never needed
tier 3.** mimir hit the same symptoms (`SQLITE_BUSY`, dropped cache
writes under load) at far larger scale (measured on its production
database 2026-10-02: 203 inboxes, 17,681,882 `articles` rows and
29,523,972 `article_lists` rows, across web + tasks + broker
processes, multi-worker) and solved it entirely
within SQLite via the broker journey (v1.32 → 2.0.0). Concrete pieces
to lift rather than reinvent (see mimir's CONTEXT.md "Single-writer
invariant" + MEMORY.md broker entries):

- **Tier 1 pragmas**: copy `mimir/extensions.py::_sqlite_pragmas`
  (WAL + `synchronous=NORMAL` + `foreign_keys=ON` + `busy_timeout`
  + `analysis_limit`), and add `PRAGMA query_only=1` on every
  non-writer process (the **delivery** app, by construction
  read-only) so an errant write raises loud instead of corrupting or
  contending. Plus a `write_transaction()` context manager with a
  `BEGIN IMMEDIATE` event listener: takes the write lock upfront so a
  read-then-upgrade can't deadlock with `SQLITE_BUSY_SNAPSHOT` (a
  different deadlock class than the cross-connection one fixed in
  v1.34.3). Plus the `held=Nms` slow-write WARNING so the next
  contention is visible with a label, not a 500.
- **Tier 2 single-writer actor**: `mimir/broker/writes.py`
  (`WriterThread` / `WriteOp` / `WriteFuture`): one writable
  connection, bounded queue, BEGIN IMMEDIATE per op, futures for
  results. The `bragi-tasks` worker is the home. mimir's rollout
  playbook (one write surface per release, additive, direct-path
  fallback behind an env flag, remove fallbacks at a major) is the
  de-risking template.
- **Calibration mimir already paid for**: long writes chunk + release
  the lock between batches; VACUUM needs `SQLITE_TMPDIR` on the data
  mount (full-DB-sized temp file); the `:memory:` test conftest can't
  reproduce cross-connection lock semantics (the file-backed fixture
  is the real guard).
