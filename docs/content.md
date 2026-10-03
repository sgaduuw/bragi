# Features and content tools

[Back to README](../README.md)

[Publishing](#publishing-and-site-management) · [Importers](#importers) ·
[Export](#export-portability) · [Datasets](#datasets)

## Protecting unfinished writing

Post and page editors keep unsaved form content in this browser for seven days,
including resume sections. When you reopen the same editor, choose **Restore** to
recover a copy or **Discard** to remove it. Copies belong to your account, site and
editor; each tab keeps its own copy. Live editors and working copies are separate.
If the original post, page or working copy no longer exists, open **Browser recovery**
on the site's dashboard or another site admin page. The read-only copies let you
select and copy your writing, including resume fields, without publishing anything.
A stale Discard action refuses to remove a copy changed in another tab.

Recovery never publishes or saves content to the server. A successful save clears
its recovery copy; failed saves and conflicts keep it. Browser storage can be
cleared or unavailable, so this is not a backup or cross-device sync. The editor
warns when it cannot store your changes. Copies remain on this device after logout
until discarded or expired; use a private browser profile on shared computers.

If someone changes content after you open it, Bragi rejects your stale save and
keeps your submitted text visible. Open the latest version in another tab, compare
and copy your changes there, then save. Recovering an older browser copy retains
its original version check. Working-copy promotion also checks whether the live
content changed since staging. Restage from the latest live editor after comparing
your changes; working copies created before this protection need restaging too.

## Scheduling posts

In the post editor, choose **Scheduled**, enter a future **Publication time**,
and save. The time uses the site's displayed timezone, not the browser's timezone.
The saved schedule shows its UTC offset. Bragi stores the instant in UTC, so a later
site-timezone change only changes its display. Reload an editor opened before such
a change before saving a schedule.

Reopen the post to inspect or change its time. To cancel, choose **Draft** and save;
the inline status control can also cancel by selecting Draft. Scheduling does not
publish immediately. A post already public must be unpublished before scheduling;
working-copy promotion remains immediate.

New schedules cannot use a local time skipped by a daylight-saving change, or one
that occurs twice when clocks move back. Choose a time outside that skipped or
repeated interval. An existing schedule keeps its exact instant when its time is
unchanged, including overdue schedules.

Revisions do not store publication times. Restoring a Scheduled revision restores
its content as Draft and clears any current schedule. Choose a new time and select
Scheduled to schedule the restored content.

The list and editor distinguish future, overdue, and missing-time schedules.
**Overdue** means the time has passed but publication has not completed; a normal
polling delay can cause this briefly. Publication depends on the task runner.
If it persists, ask an administrator to check the runner and its logs using the
[operations guidance](operations.md#scheduled-publication). Existing Scheduled
posts without a time need a time entered, or cancellation to Draft.

## Publishing and site management

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
  partial swaps via the `HX-Request` header, plus boosted admin
  navigation (the rail swaps just the content column, no full
  reload). No SPA, no separate prerender step; crawlers and
  no-JavaScript clients get complete pages.
- **Markdown source of truth.** Post and Page bodies persist as
  markdown text with a cached HTML render alongside. TipTap (with
  its markdown serializer) is the admin editor; the data model is
  editor-independent. CommonMark + tables out of the box; the
  `markdown_extras` built-in plugin adds footnotes
  (`text[^id]` + `[^id]: body`), KaTeX-compatible math
  (`$x$` / `$$x$$`), and Mermaid code fences
  (` ```mermaid `). Plugins can register more extensions via
  the `register_markdown_extension` hookspec.
- **Post-page chrome.** Each post renders with an author byline
  (linked to the author's profile page when they have one),
  reading-time estimate (220 WPM, rounded up), and an
  "Updated YYYY-MM-DD" line that only appears when the edit is
  meaningfully after first publish. Optional profile data (avatar,
  pronouns, bio, rel="me" links) surfaces as an h-card "About the
  author" aside below the body (per-site optional), and enriches the
  post's JSON-LD author. A table of contents auto-renders for multi-section posts
  (h2 / h3 headings).
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
  moving a published page to a new parent inserts a 301 covering
  the page and its whole subtree; importers preserve source URLs
  as redirect rows; resolution middleware runs on every public 404.
  `410 Gone` for tombstoned content.
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
  delete enqueue a debounced ping so participating search engines
  (Bing, Yandex, Seznam, Naver, ...) hear about the change. The POST
  is off the request path: the lifecycle hook records the URL and the
  `bragi-tasks` worker (`bragi indexnow send-pending`) sends it once
  the hold-off window (`BRAGI_INDEXNOW_DEBOUNCE_SECONDS`, default
  300s) closes, so N edits to a URL within the window coalesce into
  one ping. Per-site key bootstrapped with `bragi indexnow setup
  --site <slug>`; the verification key file lives at `/<key>.txt` on
  the delivery app.
- **Programmatic posting via API tokens.** Personal access
  tokens at `/admin/account/tokens/` (list / create / revoke;
  plaintext shown once on create) authenticate scripts and bots
  via `Authorization: Bearer brg_<id>_<secret>`. The JSON REST
  surface at `/admin/api/sites/<slug>/posts/` covers GET list,
  POST create, PATCH update, and POST publish, scope-gated by
  `post:write`. Bearer tokens work only on this JSON API. HTML admin
  routes reject bearer headers, including requests that also carry a
  browser session cookie. Use the normal browser session and CSRF token
  for those routes. Tokens are Argon2id-hashed at rest; expiry is honoured
  and usage is recorded in the audit log.
- **Indieweb webmentions (send + receive).** Outbound: on
  publish or update, every external link in a post is queued
  behind a debounce hold-off window; edits within the window
  coalesce (the window starts at the first edit) and a link
  added then removed before the window closes never sends. The
  cron-driven `bragi webmentions send-pending` then performs W3C
  endpoint discovery (Link header, then
  `<link rel="webmention">`) and POSTs the mention once due.
  `BRAGI_WEBMENTION_DEBOUNCE_SECONDS` (default 300) tunes the
  window. Inbound:
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
  The actor's `summary` and `icon` come from the site owner's
  account-profile bio and avatar (empty when the owner has none set).

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
operators who prefer the browser. Hugo and WordPress are CLI-only.

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
client-side via a self-hosted `vega-embed` bundle; a `<noscript>` block
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

