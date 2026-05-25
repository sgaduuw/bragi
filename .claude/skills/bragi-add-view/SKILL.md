---
name: bragi-add-view
description: Use when adding a new admin or delivery view (route handler) in src/bragi/apps/* or src/bragi/contrib/*/views.py. Drives the htmx dispatch convention so crawlers always see full pages and partial swaps work consistently against either response shape. Multisite scoping reminder included.
---

# Adding a new view in bragi

Views in bragi follow a specific dispatch convention (per bragi/CLAUDE.md "htmx dispatch on HX-Request"). Skipping it silently degrades SEO (crawler-blocked content) or breaks partial swaps (mis-targeted DOM ids).

## Convention

1. **Crawlers ALWAYS see the full page.** A cold request (no `HX-Request` header) returns the full HTML document with `<html>`, `<head>`, `<body>`. Partial swaps are a UX affordance, never a content gate.

2. **Dispatch on `is_htmx()`** from `bragi.core.htmx`. Do NOT inspect `request.headers.get("HX-Request")` directly — use the helper so the rule stays uniform (single point to change if behaviour ever needs to bypass htmx for staging, testing, etc.).

3. **Template pairing**: a partial template lives next to its full-page sibling with a `_` prefix.
   - Full page: `templates/admin/post_list.html`
   - Partial: `templates/admin/_post_list_table.html`

4. **Stable id wrapping**: the partial wraps its content in a fixed-id container so `hx-target` works against either the partial response or the full-page response.

5. **Full page `{% include %}`s the partial.** Markup is NOT duplicated. The full-page template wraps the partial with the surrounding chrome (header, nav, footer).

6. **Canonical reference**: post admin list. Read `src/bragi/contrib/post/` (views + templates) before deviating.

## Skeleton

### View

```python
from bragi.core.htmx import is_htmx
from flask import Blueprint, render_template

bp = Blueprint("posts", __name__)

@bp.route("/posts", methods=["GET"])
def list_posts():
    posts = _query_posts_for_site(g.site.id)
    template = (
        "admin/_post_list_table.html"
        if is_htmx()
        else "admin/post_list.html"
    )
    return render_template(template, posts=posts)
```

### Full page (`templates/admin/post_list.html`)

```html
{% extends "admin/_base.html" %}
{% block title %}Posts{% endblock %}
{% block body %}
  <header class="page-header">
    <h1>Posts</h1>
    {# action buttons, search, etc. #}
  </header>
  {% include "admin/_post_list_table.html" %}
{% endblock %}
```

### Partial (`templates/admin/_post_list_table.html`)

```html
<div id="post-list-table">
  {% if posts %}
    <table>
      {# rows #}
    </table>
  {% else %}
    <p class="empty-state">No posts yet.</p>
  {% endif %}
</div>
```

### htmx-triggering action

Any button/form that swaps the partial targets the same id:

```html
<button
  hx-get="{{ url_for('posts.list_posts') }}?filter=published"
  hx-target="#post-list-table"
  hx-swap="outerHTML"
>
  Show published only
</button>
```

The `outerHTML` swap replaces the entire `<div id="post-list-table">` (because the partial's root is that div), keeping the id present for subsequent swaps. Without the stable id, the second swap loses its target.

## Multisite reminder

If the view reads or writes content, it MUST scope by the resolved site. `request.site` (or `g.site`, depending on how middleware attaches it — read `src/bragi/core/middleware/` to confirm the canonical name on the current branch) is set by the WSGI-edge resolver.

**Every query joins on `site_id`.** Cross-tenant leaks are an integration-test concern (see `tests/integration/test_federation_e2e.py`, `tests/integration/test_landing_page.py` patterns) but write the query right the first time:

```python
posts = (
    session.execute(
        select(Post)
        .where(Post.site_id == g.site.id)  # <-- always
        .where(Post.status == PostStatus.PUBLISHED)
        .order_by(Post.published_at.desc())
    )
    .scalars()
    .all()
)
```

## Auth and CSRF

- **Admin views** require an authenticated session. Use the admin app's auth-required decorator (read `src/bragi/apps/admin/` for the canonical decorator name on the current branch).
- **Mutating views** (POST/PUT/PATCH/DELETE) require CSRF. The session table holds the CSRF token; the helper for verifying it lives in `bragi.core.security` or similar — confirm against the current branch before importing.
- **Delivery views** are unauthenticated public reads. They MUST NOT mount under `/admin` and MUST NOT trust any session state.

## Verification before commit

```sh
# Contrib test for the plugin owning the view
poetry run pytest tests/contrib/test_<plugin>.py

# Integration tests touching the route
poetry run pytest tests/integration/ -k <route-related-keyword>

# Manual: cold load
curl -i http://localhost:8001/posts

# Manual: htmx dispatch (should be smaller, no <html>)
curl -i -H "HX-Request: true" http://localhost:8001/posts

# Crawler reachability: no HX-Request, so full page
curl -A "Googlebot/2.1" http://localhost:8001/posts
```

The crawler curl should return the same full page as the unauthenticated cold load. If it 302s to login or 404s, the route is gating content on auth/htmx in a way crawlers can't follow — SEO blocker.

## When NOT to use htmx dispatch

Some views legitimately never need a partial:

- Static-shaped pages (`/about`, `/privacy`).
- API-shaped endpoints (`/feed.xml`, `/sitemap.xml`, `/.well-known/...`).
- Webhook receivers (`/webmentions`, `/actor/inbox`).

For these, just return the appropriate response directly. No `is_htmx()` branch, no partial template. The `htmx-dispatch-checker` agent treats full-page-only views as fine (it's the cold path).
