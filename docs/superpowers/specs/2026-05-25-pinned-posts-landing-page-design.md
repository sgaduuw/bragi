# Pinned posts on the index landing page — design

Spec for closing issue #125. Status: brainstormed and approved
2026-05-25; awaiting writing-plans → implementation.

## Goal

Surface editor-chosen posts above the strict chronological
recency list on a site's post-index page. Multiple pinned posts
render as a CSS scroll-snap carousel with dot indicators. Single
pin renders as one card with no chrome. Behaviour gracefully
degrades to today's page when nothing is pinned.

## Decisions made (vs. open questions in #125)

1. **Schema:** boolean *and* optional datetime, not one or the
   other. `is_pinned` is the master switch; `pinned_until` is an
   optional auto-clear. "Currently pinned" = `is_pinned AND
   (pinned_until IS NULL OR pinned_until > now())`, evaluated at
   query time rather than mutated by a background job.
2. **Recency-list dedup:** pinned posts are removed from the
   recency list on page 1 only. They reappear in their natural
   `published_at` position on page 2 and beyond, so a reader
   paginating chronologically can still find them.
3. **Placement:** the pinned section sits between the post-index
   page's optional intro (`page.body_html`) and the recency list.
   Labelled "Pinned" via an `<h2>` inside a `<section
   aria-label>`.
4. **Carousel mechanics:** CSS `scroll-snap-type: x mandatory`,
   no application JS. Aligns with the CONTEXT.md posture
   ("delivery app ships no application JS at all; htmx + a tiny
   vega-embed shim are the only frontend deps").
5. **Carousel chrome:** dot indicators below the strip, rendered
   as anchor links to per-card `#pinned-<post_id>` fragments;
   active dot is coloured via the `:target` selector. Single-pin
   case omits the dot block entirely.
6. **Card composition:** stacked — image (when `featured_image`
   set) above title + date + excerpt. No-image fallback collapses
   to the text panel at full card height.
7. **Order of pins in the carousel:** `published_at DESC` among
   currently-pinned posts. No new `pinned_at` field; YAGNI on
   manual reorder until a real editorial pain shows up.
8. **Admin UX:** pin checkbox + optional `pinned_until` datetime
   input near the status select on the edit form (shown only
   when status is `published`); plus a per-row Pin/Unpin button
   on the admin post list backed by an htmx POST endpoint. Each
   toggle writes an AuditLog row.

## Schema

Two new columns on `posts`. Migration is additive; no row-level
backfill needed.

| Field | Type | Default | Index |
|---|---|---|---|
| `is_pinned` | `bool NOT NULL` | `False` (server_default `false`) | partial index `ix_posts_site_pinned (site_id, is_pinned) WHERE is_pinned` |
| `pinned_until` | `datetime NULL` | `NULL` | (no dedicated index) |

The partial index keeps lookups fast even when most rows have
`is_pinned=False`, which is the expected steady state.

`pinned_until` is naive UTC, matching the rest of the post
timestamps (`published_at`, `scheduled_for`).

### Currently-pinned predicate

```sql
is_pinned = TRUE
AND (pinned_until IS NULL OR pinned_until > :now)
```

Computed at query time. The `is_pinned` flag is never auto-flipped
by a background job; this preserves the editor's setting across
the expiry boundary so re-using the pin (extending or removing
the date cap) doesn't require re-toggling.

## Data model — `bragi/core/models/post.py`

```python
is_pinned: Mapped[bool] = mapped_column(default=False)
pinned_until: Mapped[datetime | None] = mapped_column(default=None)
```

No relationship changes. `PostRevision` already snapshots the
post's state; including the two new fields in revision
serialisation is part of the implementation work.

## Delivery rendering — `bragi/contrib/page/delivery.py`

`render_post_index_page(site, page)` gains a pinned-set lookup
gated on `page_n == 1`, exclusion of pinned IDs from the recency
query on page 1, and an extended ETag input that folds the
expiry boundary.

Sketch:

```python
from sqlalchemy import or_
from bragi.core.dates import naive_utcnow

pinned_posts: list[Post] = []
if page_n == 1:
    pinned_posts = db.execute(
        select(Post)
        .where(
            Post.site_id == site.id,
            Post.status == PostStatus.PUBLISHED,
            Post.is_pinned.is_(True),
            or_(Post.pinned_until.is_(None),
                Post.pinned_until > naive_utcnow()),
        )
        .order_by(Post.published_at.desc())
    ).scalars().all()

pinned_ids = {p.id for p in pinned_posts}

base = select(Post).where(
    Post.site_id == site.id,
    Post.status == PostStatus.PUBLISHED,
)
if pinned_ids:  # only non-empty when page_n == 1
    base = base.where(Post.id.notin_(pinned_ids))

total = db.execute(
    select(func.count()).select_from(base.subquery())
).scalar_one()
```

The downstream pagination math (`total_pages`, `has_next`) uses
this filtered `total` for page 1 and the unfiltered total for
page 2+. The page-of-N display in the template reads from a
context value already passed in (`total_pages`).

### ETag

Existing key: `f"{site.id}|{page.id}|{page_n}|{per_page}"` plus
`last_modified = max(updated_at over posts and the page row)`.

New inputs folded in:

- Pinned posts' `updated_at` already participates in `candidates`
  when they're rendered (which is exactly the page-1 case where
  they appear).
- **`min(pinned_until)` over the pinned set**, truncated to the
  minute, appended to the ETag key. This way, when an auto-expiry
  passes, the ETag changes and cached responses invalidate
  without a manual cache buster.

The truncation to the minute avoids ETag churn from microsecond
differences while keeping invalidation latency bounded.

## Template — `bragi/contrib/page/templates/delivery/post_index.html`

A new `{% if pinned_posts %}` block sits between the existing
intro `<div class="intro">` and the `<ul class="post-list">`.
The block includes a partial:

`bragi/contrib/page/templates/delivery/_pinned_carousel.html`

```html
<section class="pinned" aria-label="Pinned posts">
  <h2 class="pinned-label">Pinned</h2>
  <div class="pinned-strip" role="region" tabindex="0">
    {% for post in pinned_posts %}
      <article class="pinned-card" id="pinned-{{ post.id }}">
        {% if post.featured_image_id %}
          <img src="{{ attachment_url(post.featured_image) }}"
               alt="" loading="lazy">
        {% endif %}
        <h3><a href="{{ url_for_post(post) }}">{{ post.title }}</a></h3>
        <time datetime="{{ post.published_at.isoformat() }}">
          {{ post.published_at.strftime('%Y-%m-%d') }}
        </time>
        {% if post.body_excerpt %}
          <p class="excerpt">{{ post.body_excerpt }}</p>
        {% endif %}
      </article>
    {% endfor %}
  </div>
  {% if pinned_posts|length > 1 %}
    <nav class="pinned-dots" aria-label="Pinned carousel pagination">
      {% for post in pinned_posts %}
        <a href="#pinned-{{ post.id }}"
           aria-label="Go to pinned post {{ loop.index }}"></a>
      {% endfor %}
    </nav>
  {% endif %}
</section>
```

The partial is invokable from other contexts if needed (themes),
but no current consumer beyond the post-index page is planned.

### Carousel CSS

Added to the existing delivery stylesheet (whichever theme is
active inherits via the standard cascade; carousel CSS is theme-
neutral structural styling).

```css
.pinned { margin: 1.5rem 0; }
.pinned-label { font-size: 0.875rem; text-transform: uppercase;
                letter-spacing: 0.05em; margin-bottom: 0.5rem; }

.pinned-strip {
  display: flex;
  gap: 1rem;
  overflow-x: auto;
  scroll-snap-type: x mandatory;
  scroll-behavior: smooth;
}

.pinned-card {
  flex: 0 0 min(60%, 24rem);
  scroll-snap-align: start;
  border: 1px solid var(--card-border, #e2e8f0);
  border-radius: 6px;
  overflow: hidden;
}

.pinned-card img {
  width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block;
}

.pinned-dots {
  display: flex; gap: 0.5rem;
  justify-content: center; margin-top: 0.75rem;
}
.pinned-dots a {
  width: 0.5rem; height: 0.5rem; border-radius: 50%;
  background: var(--dot-inactive, #cbd5e1);
}
/* Active-dot styling: generated per-pin via Jinja, see below. */
```

### Active-dot styling

Pure CSS `:target` only matches the targeted element itself, not
sibling navigation. To colour the matching dot, we render one
extra rule per card from Jinja:

```css
{% for post in pinned_posts %}
#pinned-{{ post.id }}:target ~ .pinned-dots a[href="#pinned-{{ post.id }}"] {
  background: var(--dot-active, #475569);
}
{% endfor %}
```

This is small (a handful of selectors for typical pin counts) and
inlined into the partial inside a `<style>` block scoped to the
section. The default "page loaded with no fragment" state has no
active dot; that's fine — the first card is visible by virtue of
being the natural scroll position.

### Accessibility

- `<section aria-label="Pinned posts">` for landmark navigation.
- `role="region"` + `tabindex="0"` on the strip lets keyboard
  users focus it and scroll with arrow keys.
- Each dot is a real `<a href>` with a unique `aria-label`.
- `alt=""` on the featured image: it's decorative; the card's
  link text carries the post title.

## Admin UX

### Edit form — `bragi/contrib/post/templates/admin/edit.html`

A small block added inside the status fieldset, shown only when
status is `published` (server-rendered conditional; client-side
toggling on status-select change is fine but not required for
v1, since saving the form re-renders).

```html
{% if post.status == 'published' %}
<fieldset>
  <label>
    <input type="checkbox" name="is_pinned"
           {% if post.is_pinned %}checked{% endif %}>
    Pin on landing page
  </label>
  <label class="sub">
    Optional auto-unpin:
    <input type="datetime-local" name="pinned_until"
           value="{{ post.pinned_until.strftime('%Y-%m-%dT%H:%M') if post.pinned_until else '' }}">
  </label>
</fieldset>
{% endif %}
```

POST handler in `bragi/contrib/post/admin.py` reads
`is_pinned = 'is_pinned' in request.form` and parses
`pinned_until` from the datetime-local input (empty string →
`None`). The string is treated as naive UTC, matching the
existing handling of `scheduled_for`. Both fields participate
in the existing `before` / `after` dict for the AuditLog
`post.update` row.

### Post list — `bragi/contrib/post/templates/admin/_post_list_table.html`

A new "Pinned" column between Status and Updated. Each cell:

```html
<td class="pinned-cell" id="pinned-cell-{{ post.id }}">
  <form hx-post="{{ url_for('post_admin.pin_toggle', site_slug=site.slug, post_id=post.id) }}"
        hx-target="#pinned-cell-{{ post.id }}"
        hx-swap="outerHTML">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <button type="submit"
            class="{% if post.is_pinned %}btn-pinned{% else %}btn-unpinned{% endif %}">
      {{ 'Unpin' if post.is_pinned else 'Pin' }}
    </button>
  </form>
</td>
```

### New route

`POST /admin/sites/<site_slug>/posts/<post_id>/pin-toggle` in
`bragi/contrib/post/admin.py`:

- Permission: `require_role(Role.AUTHOR)` scoped to the site (same
  shape as the existing edit endpoint).
- Toggles `post.is_pinned` (does *not* touch `pinned_until`).
- Writes an `AuditLog` row with `action=post.pin` or `post.unpin`,
  and `before`/`after` carrying `is_pinned`.
- Returns the rendered `<td>` partial with the new state, scoped
  by the stable `id` attribute for `hx-swap=outerHTML`.
- Plain (non-htmx) clients get the same partial wrapped in a
  redirect to the post-list page. Detection via `is_htmx()` from
  `bragi.core.htmx`, matching the project's htmx-dispatch
  convention.

Constraint: the endpoint allows toggling on any status (not just
`published`). The delivery query filters to `status=published`,
so pinning a draft is a no-op for the public page but lets an
operator set future-pin intent before publishing.

## Edge cases

| Case | Behaviour |
|---|---|
| Status flipped to `archived` / `draft` | Disappears from pinned section (status filter). `is_pinned` retained. Re-publish restores. |
| `pinned_until` passes | Drops from pinned section silently. Flag intact. ETag fold ensures cached pages invalidate. |
| Pinned post deleted | Pin disappears via FK cascade. Nothing to clean up. |
| Pinned post restored from revision | Revision snapshot includes `is_pinned` and `pinned_until`; restore reapplies. |
| Page 1 has only pinned posts | Render pinned section + empty-state note in place of recency list. Pagination chrome suppressed. |
| No pinned posts at all | `{% if pinned_posts %}` skips entirely. Page byte-identical to today (modulo ETag's stable empty-set fold). |
| Importer-created posts | Default to `is_pinned=False`, `pinned_until=NULL`. Importers don't set pins. |
| Pinning a scheduled or draft post | Allowed at the model layer; invisible on the public side until status flips and `published_at <= now`. Editorial intent preserved. |
| Multisite | All queries already filter by `site_id`; pins are per-site by construction. |
| RSS feed | Not affected. Pins are a landing-page affordance, not a syndication signal. |

## Testing

- **`tests/contrib/test_post_admin.py`**: edit-form pin round-trip
  (with and without `pinned_until`), pin/unpin via list-view
  endpoint, AuditLog row written with correct before/after,
  permission check (cross-site forbidden), datetime parsing edge
  cases (empty → None, malformed → 400).
- **`tests/contrib/test_post_index.py`** (or the test that owns
  the post-index render): page 1 carousel renders + recency list
  excludes the pinned IDs; page 2 reinstates pinned posts in date
  position; expired `pinned_until` excludes the post; archived
  post stays out; ETag changes on pin toggle; ETag changes when
  `pinned_until` passes (freeze time around the boundary); single-
  pin case omits the dots block.
- **`tests/integration/test_landing_page.py`**: scenario where the
  landing page is the post-index and pins exist — assert the
  `.pinned` section in rendered HTML, the recency-list exclusion,
  the page 2 reinstatement, and accessibility attributes
  (`aria-label`, `role="region"`).
- **`tests/contrib/test_seo_endpoints.py` / `test_jsonld.py`**:
  confirm sitemap, atom feed, and JSON-LD are unchanged by
  pinning.

No new fixtures. Existing `db_session_factory` +
`patched_session_locals` + the post fixture helpers cover the DB
side.

## Migration

```python
# alembic/versions/YYYY_MM_DD_HHMM-<rev>_add_post_pinning.py
def upgrade():
    with op.batch_alter_table("posts") as b:
        b.add_column(sa.Column("is_pinned", sa.Boolean(),
                               nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("pinned_until", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_posts_site_pinned",
        "posts",
        ["site_id", "is_pinned"],
        postgresql_where=sa.text("is_pinned"),
        sqlite_where=sa.text("is_pinned"),
    )

def downgrade():
    op.drop_index("ix_posts_site_pinned", table_name="posts")
    with op.batch_alter_table("posts") as b:
        b.drop_column("pinned_until")
        b.drop_column("is_pinned")
```

Server-default `false` lets the migration apply to a populated
table without a per-row backfill. The Python-side default is
`False` (no `server_default` exposed via SQLAlchemy model after
the migration; the column reads NOT NULL).

Up-down-up migration smoke is covered by the release-cut
checklist (already standard for bragi releases).

## Files touched (estimated)

- `alembic/versions/<new>.py` — migration.
- `src/bragi/core/models/post.py` — two new fields.
- `src/bragi/contrib/post/admin.py` — edit-form save handler
  extension; new `pin_toggle` route; AuditLog wiring.
- `src/bragi/contrib/post/templates/admin/edit.html` — pin
  fieldset.
- `src/bragi/contrib/post/templates/admin/_post_list_table.html`
  — pinned-cell column.
- `src/bragi/contrib/page/delivery.py` — `render_post_index_page`
  changes; ETag fold.
- `src/bragi/contrib/page/templates/delivery/post_index.html` —
  include block for the partial.
- `src/bragi/contrib/page/templates/delivery/_pinned_carousel.html`
  — new partial.
- Delivery stylesheet(s) — carousel CSS. Per-theme inheritance.
- `tests/contrib/test_post_admin.py`, `test_post_index.py`,
  `test_seo_endpoints.py`, `test_jsonld.py`, plus
  `tests/integration/test_landing_page.py` updates.
- `CHANGELOG.md` — under `[Unreleased]` (`Added`).

## Out of scope

- Tag-cloud / category sidebar on the index. Tracked separately.
- Manual carousel ordering UI. Falls out of the "newest published
  first" call.
- A "Featured" badge on the post-detail page itself. Pins are a
  landing-page affordance, not a per-post visual claim.
- JS-driven carousel chrome (auto-rotate, keyboard interception
  beyond what scroll-snap already provides). Out by CONTEXT.md
  posture.
- Bulk pin/unpin from the admin list. Single-row toggling covers
  the expected operational load.

## Acceptance criteria

The feature is done when:

1. Migration applies cleanly on a fresh SQLite and against the
   `bragi.db.dev-backup` from a recent CI run.
2. Pinning a published post via the edit form makes it appear in
   the pinned section on the post-index page.
3. Pinning via the list-view button has the same effect, returns
   the updated cell via htmx, and writes an AuditLog row.
4. With one pin: a card renders with no dot block.
5. With multiple pins: the strip scroll-snaps, the dots are
   anchor-linked, `:target` colours the active one.
6. Pinned posts are excluded from the page 1 recency list and
   reinstated on page 2+.
7. `pinned_until` in the past silently drops the post from the
   pinned section; ETag changes at the boundary.
8. The page renders identically to today's output when no posts
   are pinned.
9. CHANGELOG, tests, lint, mypy, and migration up-down-up smoke
   all green.
