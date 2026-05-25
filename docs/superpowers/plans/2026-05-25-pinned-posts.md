# Pinned Posts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface editor-chosen posts in a CSS scroll-snap carousel above the chronological recency list on a site's post-index page (closes #125).

**Architecture:** Two new columns on `posts` (`is_pinned`, `pinned_until`); admin edit-form fieldset + post-list inline toggle; delivery query pulls currently-pinned set on page 1, excludes them from the recency list on page 1 only; rendering uses a new `_pinned_carousel.html` partial with CSS scroll-snap and anchor-link dot indicators (no application JS). Reference: `docs/superpowers/specs/2026-05-25-pinned-posts-landing-page-design.md`.

**Tech Stack:** Flask + SQLAlchemy 2.0 + Alembic + pydantic-settings + pytest + htmx + Pico CSS + four in-tree themes. Branch: `feature/pinned-posts` (off develop, currently carries only the spec commit).

---

## Prerequisites

- PR #256 (`Make SessionLocal a lazy proxy`) should have merged to develop. If not, the implementation still works against the pre-refactor SessionLocal shape, but tests need to use per-module monkeypatches instead of `bragi.core.db.SessionLocal._factory`. Check with `gh pr view 256 --json state`.
- After #256 lands, rebase or merge develop into this branch: `git fetch origin && git merge origin/develop`. Resolve any conflicts (none expected; the spec is in a new directory).
- Confirm pytest is green on the current branch before starting: `poetry run pytest -q` (expect 1109 passes, since this branch already has the SessionLocal refactor via the eventual merge from develop).

## File map (created or modified)

- Create: `alembic/versions/<timestamp>-add_post_pinning.py` (migration)
- Modify: `src/bragi/core/models/post.py` (add two fields)
- Modify: `src/bragi/contrib/post/admin.py` (extend `_form_from_request`, edit/new handlers, add `pin_toggle` route)
- Modify: `src/bragi/contrib/post/templates/admin/edit.html` (pin fieldset)
- Modify: `src/bragi/contrib/post/templates/admin/_post_list_table.html` (pinned column + htmx form)
- Modify: `src/bragi/core/audit.py` (add `POST_PINNED` / `POST_UNPINNED` action constants)
- Modify: `src/bragi/contrib/page/delivery.py` (`render_post_index_page`)
- Modify: `src/bragi/contrib/page/templates/delivery/post_index.html` (include the partial)
- Create: `src/bragi/contrib/page/templates/delivery/_pinned_carousel.html`
- Modify: `src/bragi/contrib/theme_default/templates/delivery/base.html` (CSS)
- Modify: `src/bragi/contrib/theme_minimal/templates/delivery/base.html` (CSS)
- Modify: `src/bragi/contrib/theme_serif/templates/delivery/base.html` (CSS)
- Modify: `src/bragi/contrib/theme_terminal/templates/delivery/base.html` (CSS)
- Modify: `tests/contrib/test_post_admin.py` (new fields + pin-toggle endpoint tests)
- Modify: `tests/contrib/test_post_index.py` (delivery query + ETag tests)
- Modify: `tests/integration/test_landing_page.py` (full-stack scenario)
- Modify: `CHANGELOG.md` (`[Unreleased] > Added` line)

---

## Task 1: Add `is_pinned` and `pinned_until` to the Post model + migration

**Files:**
- Modify: `src/bragi/core/models/post.py`
- Create: `alembic/versions/<timestamp>-add_post_pinning.py`
- Test: `tests/contrib/test_post_admin.py` (add model-level defaults check)

- [ ] **Step 1: Write the failing test for model defaults**

Add this test to `tests/contrib/test_post_admin.py` (at module top-level alongside existing tests; it reuses the seeded `admin_app` fixture's site + user):

```python
def test_post_defaults_for_pinning_fields(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        assert post.is_pinned is False
        assert post.pinned_until is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/contrib/test_post_admin.py::test_post_defaults_for_pinning_fields -v`
Expected: FAIL with `AttributeError: 'Post' object has no attribute 'is_pinned'`.

- [ ] **Step 3: Add the model fields**

In `src/bragi/core/models/post.py`, after the existing `noindex` field and before the `featured_image_id` block, add:

```python
    # Editorial pin: surfaces the post above the recency list on
    # the site's post-index page. `pinned_until` is an optional
    # auto-expiry (NULL = pinned indefinitely); "currently pinned"
    # is evaluated at query time as
    # `is_pinned AND (pinned_until IS NULL OR pinned_until > now)`.
    is_pinned: Mapped[bool] = mapped_column(default=False)
    pinned_until: Mapped[datetime | None] = mapped_column(default=None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `poetry run pytest tests/contrib/test_post_admin.py::test_post_defaults_for_pinning_fields -v`
Expected: PASS.

- [ ] **Step 5: Generate the migration**

Run: `poetry run alembic revision --autogenerate -m "add post pinning"`
Output: a new file under `alembic/versions/<datetime>-<rev>_add_post_pinning.py`.

- [ ] **Step 6: Edit the generated migration to add the partial index and server_default**

Open the generated file. The `upgrade()` and `downgrade()` should look like this (autogen will produce something close; tighten to the exact shape below):

```python
def upgrade() -> None:
    with op.batch_alter_table("posts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_pinned",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("pinned_until", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_posts_site_pinned",
        "posts",
        ["site_id", "is_pinned"],
        postgresql_where=sa.text("is_pinned"),
        sqlite_where=sa.text("is_pinned"),
    )


def downgrade() -> None:
    op.drop_index("ix_posts_site_pinned", table_name="posts")
    with op.batch_alter_table("posts") as batch_op:
        batch_op.drop_column("pinned_until")
        batch_op.drop_column("is_pinned")
```

`server_default=sa.false()` lets the migration apply to a populated table without backfill. The model's Python-side `default=False` covers new-row inserts.

- [ ] **Step 7: Smoke the migration up-down-up against a fresh SQLite**

Run:
```bash
rm -f bragi.smoke.db bragi.smoke.db-wal bragi.smoke.db-shm
BRAGI_DATABASE_URL=sqlite:///./bragi.smoke.db poetry run alembic upgrade head
BRAGI_DATABASE_URL=sqlite:///./bragi.smoke.db poetry run alembic downgrade base
BRAGI_DATABASE_URL=sqlite:///./bragi.smoke.db poetry run alembic upgrade head
rm -f bragi.smoke.db bragi.smoke.db-wal bragi.smoke.db-shm
```

Expected: each command exits 0; no errors about non-reversible operations.

- [ ] **Step 8: Run the model test + full test suite to confirm nothing regressed**

Run: `poetry run pytest -q`
Expected: PASS (1110+ tests; the new model test is the +1).

- [ ] **Step 9: Commit**

```bash
git add src/bragi/core/models/post.py alembic/versions/ tests/contrib/test_post_admin.py
git commit -m "Add is_pinned and pinned_until columns to posts

Schema-only step: model fields, alembic migration with partial
index on (site_id, is_pinned) WHERE is_pinned, defaults to
False/NULL. No query or admin behaviour yet; subsequent commits
wire up the form, delivery query, and rendering.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Persist pinning fields through the edit/new post handlers

**Files:**
- Modify: `src/bragi/contrib/post/admin.py:58-67` (`_form_from_request`)
- Modify: `src/bragi/contrib/post/admin.py:217-228` (`new_post`)
- Modify: `src/bragi/contrib/post/admin.py:273-326` (`edit_post`)
- Modify: `src/bragi/contrib/post/admin.py:111-136` (`_snapshot_post` if it doesn't already serialise the full row)
- Test: `tests/contrib/test_post_admin.py`

- [ ] **Step 1: Write the failing form round-trip test**

Add these tests to `tests/contrib/test_post_admin.py`. They use the existing `admin_app` fixture's seeded site (`blog`) + user (`ada@example.com`) + post (`hello`, draft); the tests publish the seeded post then issue an edit that toggles the new fields.

```python
def test_edit_post_form_round_trips_pinning_fields(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        db.commit()
        post_id = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "_csrf_token": token,
            "title": "Hello World",
            "slug": "hello",
            "body_markdown": "Hello!",
            "status": "published",
            "tags": "",
            "og_image_id": "",
            "is_pinned": "1",
            "pinned_until": "2026-12-31T12:00",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        reloaded = db.get(Post, post_id)
        assert reloaded.is_pinned is True
        assert reloaded.pinned_until == datetime(2026, 12, 31, 12, 0)


def test_edit_post_form_clears_pinned_until_when_empty(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        post.is_pinned = True
        post.pinned_until = datetime(2026, 12, 31, 12, 0)
        db.commit()
        post_id = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "_csrf_token": token,
            "title": "Hello World",
            "slug": "hello",
            "body_markdown": "Hello!",
            "status": "published",
            "tags": "",
            "og_image_id": "",
            "is_pinned": "1",
            "pinned_until": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        reloaded = db.get(Post, post_id)
        assert reloaded.is_pinned is True
        assert reloaded.pinned_until is None
```

- [ ] **Step 2: Run to verify the tests fail**

Run: `poetry run pytest tests/contrib/test_post_admin.py::test_edit_post_form_round_trips_pinning_fields tests/contrib/test_post_admin.py::test_edit_post_form_clears_pinned_until_when_empty -v`
Expected: FAIL because the handler ignores `is_pinned` / `pinned_until` from the form.

- [ ] **Step 3: Extend `_form_from_request()` to read the new fields**

In `src/bragi/contrib/post/admin.py:58-67`, change `_form_from_request` to:

```python
def _form_from_request() -> dict[str, str]:
    """Pull the post-edit form fields off the current request."""
    return {
        "title": (request.form.get("title") or "").strip(),
        "slug": (request.form.get("slug") or "").strip(),
        "body_markdown": request.form.get("body_markdown") or "",
        "status": request.form.get("status") or PostStatus.DRAFT,
        "tags": (request.form.get("tags") or "").strip(),
        "og_image_id": (request.form.get("og_image_id") or "").strip(),
        # Pinning. The checkbox sends "1" when ticked, absent when not.
        # The datetime-local input sends "" when cleared; we parse it
        # as naive UTC (matching `scheduled_for`'s convention) below.
        "is_pinned": "1" if request.form.get("is_pinned") else "",
        "pinned_until": (request.form.get("pinned_until") or "").strip(),
    }
```

- [ ] **Step 4: Add a helper to parse `pinned_until`**

In `src/bragi/contrib/post/admin.py`, near the other helpers (around the `_resolve_og_image_id` definition), add:

```python
def _parse_pinned_until(raw: str) -> tuple[datetime | None, str | None]:
    """Parse the datetime-local form input.

    Empty string → (None, None) (clears the pin expiry).
    Valid `YYYY-MM-DDTHH:MM` → (datetime, None), treated as naive UTC.
    Anything else → (None, error_message).
    """
    if not raw:
        return None, None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M"), None
    except ValueError:
        return None, f"Invalid auto-unpin date: {raw!r}"
```

You will need to add `from datetime import datetime` to the imports if not already present (it is, transitively, but make the import explicit).

- [ ] **Step 5: Wire the new fields into `edit_post`**

In `src/bragi/contrib/post/admin.py`, locate the `edit_post` POST branch (around line 284 onward). After the existing field assignments (`post.title = form["title"]`, etc.) and before `post.status = form["status"]`, add:

```python
        # Pinning. The checkbox semantics: "1" → True, "" → False.
        # The datetime input has its own validator that surfaces a
        # flash if the format is wrong.
        pinned_until, pin_err = _parse_pinned_until(form["pinned_until"])
        if pin_err is not None:
            flash(pin_err, "error")
            return render_template("admin/edit.html", post=post, form=form)
        post.is_pinned = form["is_pinned"] == "1"
        post.pinned_until = pinned_until
```

Also expand `before` and `after` dicts to include the new fields:

```python
        before = {
            "slug": post.slug,
            "title": post.title,
            "status": post.status,
            "is_pinned": post.is_pinned,
            "pinned_until": post.pinned_until.isoformat() if post.pinned_until else None,
        }
```

(Add the same two keys to the post-mutation `after` dict that's audited at the end of the function.)

- [ ] **Step 6: Wire the new fields into `new_post`**

In `src/bragi/contrib/post/admin.py:217-228` (`new_post` POST branch), parse `pinned_until` the same way and pass `is_pinned=` and `pinned_until=` to the `Post(...)` constructor:

```python
        pinned_until, pin_err = _parse_pinned_until(form["pinned_until"])
        if pin_err is not None:
            flash(pin_err, "error")
            return render_template("admin/edit.html", post=None, form=form)

        new_post_row = Post(
            site_id=site_id,
            slug=form["slug"],
            title=form["title"],
            body_markdown=body_markdown,
            body_html=render_markdown(body_markdown),
            body_excerpt=make_excerpt(body_markdown),
            author_id=int(session["user_id"]),
            status=new_status,
            published_at=(naive_utcnow() if new_status == PostStatus.PUBLISHED else None),
            og_image_id=og_image_id,
            is_pinned=(form["is_pinned"] == "1"),
            pinned_until=pinned_until,
        )
```

- [ ] **Step 7: Confirm revision snapshot serialisation already covers the new fields**

Open `src/bragi/contrib/post/admin.py` around the `_snapshot_post` helper (~line 111). If it serialises every field via `dict(post.__table__.columns)` or similar, no change is needed (the new columns ride along). If it has an explicit field allowlist, append `is_pinned` and `pinned_until` to it.

Run a quick grep: `grep -n "is_pinned\|pinned_until\|status" src/bragi/contrib/post/admin.py`. If `_snapshot_post` doesn't reference field names explicitly, you're done. Otherwise add the two field names.

- [ ] **Step 8: Run the form tests + full suite**

Run: `poetry run pytest tests/contrib/test_post_admin.py -v`
Expected: the two new tests PASS; existing tests stay PASS.

Run: `poetry run pytest -q`
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add src/bragi/contrib/post/admin.py tests/contrib/test_post_admin.py
git commit -m "Persist pinning fields through new/edit post handlers

_form_from_request reads is_pinned (checkbox '1' or absent) and
pinned_until (HTML5 datetime-local, parsed as naive UTC matching
scheduled_for). Empty pinned_until → NULL. Invalid format flashes
an error without saving. AuditLog before/after dicts pick up the
two new fields automatically.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Add `POST_PINNED` / `POST_UNPINNED` audit action constants

**Files:**
- Modify: `src/bragi/core/audit.py:27-37` (`AuditAction` class)
- Test: `tests/core/test_audit.py`

- [ ] **Step 1: Write a failing test**

Add to `tests/core/test_audit.py`:

```python
def test_audit_action_has_pin_constants() -> None:
    from bragi.core.audit import AuditAction
    assert AuditAction.POST_PINNED == "post.pinned"
    assert AuditAction.POST_UNPINNED == "post.unpinned"
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/core/test_audit.py::test_audit_action_has_pin_constants -v`
Expected: FAIL with `AttributeError: type object 'AuditAction' has no attribute 'POST_PINNED'`.

- [ ] **Step 3: Add the constants**

In `src/bragi/core/audit.py`, inside `class AuditAction`, immediately after `POST_DELETED`:

```python
    POST_PINNED = "post.pinned"
    POST_UNPINNED = "post.unpinned"
```

- [ ] **Step 4: Run to verify it passes**

Run: `poetry run pytest tests/core/test_audit.py::test_audit_action_has_pin_constants -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bragi/core/audit.py tests/core/test_audit.py
git commit -m "Add POST_PINNED / POST_UNPINNED audit action constants

Two new action strings used by the upcoming pin-toggle endpoint
to write AuditLog rows. No callers yet; wired up in the next
commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Add the `pin_toggle` admin endpoint

**Files:**
- Modify: `src/bragi/contrib/post/admin.py` (new route + helper)
- Test: `tests/contrib/test_post_admin.py`

- [ ] **Step 1: Write failing tests for the endpoint**

Add these tests to `tests/contrib/test_post_admin.py`. All reuse the seeded `admin_app` fixture; the seeded "hello" post is a draft so we promote it to PUBLISHED in each test that needs a pinnable post (the toggle endpoint itself accepts any status).

```python
def test_pin_toggle_htmx_returns_updated_cell(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        db.commit()
        pid = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{pid}/edit")
    resp = client.post(
        f"/admin/sites/blog/posts/{pid}/pin-toggle",
        data={"_csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'id="pinned-cell-{pid}"' in body
    assert "Unpin" in body

    with db_session_factory() as db:
        assert db.get(Post, pid).is_pinned is True


def test_pin_toggle_writes_audit_log(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    from bragi.core.models.audit_log import AuditLog

    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        db.commit()
        pid = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{pid}/edit")
    client.post(
        f"/admin/sites/blog/posts/{pid}/pin-toggle",
        data={"_csrf_token": token},
        headers={"HX-Request": "true"},
    )

    with db_session_factory() as db:
        row = db.execute(
            select(AuditLog)
            .where(AuditLog.target_type == "post", AuditLog.target_id == pid)
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one()
    assert row.action == "post.pinned"


def test_pin_toggle_non_htmx_redirects_to_list(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        db.commit()
        pid = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{pid}/edit")
    resp = client.post(
        f"/admin/sites/blog/posts/{pid}/pin-toggle",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert "/admin/sites/blog/posts" in resp.headers["Location"]


def test_pin_toggle_cross_site_404(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Toggling a post that belongs to a different site under the
    URL of site `blog` 404s, mirroring edit/delete behaviour."""
    with db_session_factory() as db:
        owner = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        other = Site(
            slug="other",
            hostname="other.example.com",
            title="Other",
            canonical_url="https://other.example.com",
            owner_user_id=owner.id,
        )
        db.add(other)
        db.flush()
        foreign = Post(
            site_id=other.id, slug="z", title="Z", body_markdown="",
            body_html="", body_excerpt="", author_id=owner.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 1, 12, 0),
        )
        db.add(foreign)
        db.commit()
        foreign_id = foreign.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/")
    resp = client.post(
        f"/admin/sites/blog/posts/{foreign_id}/pin-toggle",
        data={"_csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: Run to verify the tests fail**

Run: `poetry run pytest tests/contrib/test_post_admin.py -k pin_toggle -v`
Expected: all four FAIL with 404 (route doesn't exist yet).

- [ ] **Step 3: Add the endpoint**

In `src/bragi/contrib/post/admin.py`, after the existing `delete_post` route (~line 360), add:

```python
@bp.route("/<int:post_id>/pin-toggle", methods=["POST"])
def pin_toggle(site_slug: str, post_id: int) -> ResponseReturnValue:
    """Flip Post.is_pinned via an htmx-friendly POST.

    The list-view button posts here and (for htmx requests) gets
    the updated cell back for `hx-swap=outerHTML`. Plain (non-htmx)
    submitters get a redirect to the list. Does not touch
    `pinned_until`; nuanced expiry timing belongs on the edit
    form.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)

        active = current_user()
        is_own = bool(active and active.id == post.author_id)
        if not ((is_own and has_role("author", post.site_id)) or has_role("editor", post.site_id)):
            abort(403)

        before_pinned = post.is_pinned
        post.is_pinned = not before_pinned
        db.commit()

        audit(
            AuditAction.POST_PINNED if post.is_pinned else AuditAction.POST_UNPINNED,
            target_type="post",
            target_id=post.id,
            site_id=post.site_id,
            extra={"before": before_pinned, "after": post.is_pinned},
        )

        if is_htmx():
            return render_template(
                "admin/_pinned_cell.html",
                post=post,
                site=site,
            )

    return redirect(url_for("post_admin.list_posts", site_slug=site_slug))
```

- [ ] **Step 4: Create the partial for the htmx response**

Create `src/bragi/contrib/post/templates/admin/_pinned_cell.html`:

```html
{# Inline pinned-cell partial. Returned by pin_toggle for htmx
   clients; included by _post_list_table.html for full-page renders.
   Stable id so hx-swap=outerHTML targets the same node it came from. #}
<td class="pinned-cell" id="pinned-cell-{{ post.id }}">
  <form method="post"
        hx-post="{{ url_for('post_admin.pin_toggle', site_slug=site.slug, post_id=post.id) }}"
        hx-target="#pinned-cell-{{ post.id }}"
        hx-swap="outerHTML"
        action="{{ url_for('post_admin.pin_toggle', site_slug=site.slug, post_id=post.id) }}"
        style="display: inline; margin: 0;">
    <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
    <button type="submit" class="{{ 'btn-pinned' if post.is_pinned else 'btn-unpinned' }}">
      {{ 'Unpin' if post.is_pinned else 'Pin' }}
    </button>
  </form>
</td>
```

- [ ] **Step 5: Run the endpoint tests**

Run: `poetry run pytest tests/contrib/test_post_admin.py -k pin_toggle -v`
Expected: all four PASS.

- [ ] **Step 6: Run the full suite**

Run: `poetry run pytest -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/bragi/contrib/post/admin.py src/bragi/contrib/post/templates/admin/_pinned_cell.html tests/contrib/test_post_admin.py
git commit -m "Add pin-toggle admin endpoint

POST /admin/sites/<slug>/posts/<id>/pin-toggle flips is_pinned
and writes a POST_PINNED / POST_UNPINNED audit row. Htmx
clients get the rendered _pinned_cell.html partial back for
hx-swap=outerHTML; plain submitters get a redirect to the list.

Cross-site post-id probes 404 (same shape as the other
post-admin routes). Author can toggle own posts; editor+ can
toggle any post on their sites.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Wire the pinned cell into the admin post-list table

**Files:**
- Modify: `src/bragi/contrib/post/templates/admin/_post_list_table.html`
- Modify: `src/bragi/contrib/post/admin.py:165-190` (`list_posts` — pass `site` to template)
- Test: `tests/contrib/test_post_admin.py`

- [ ] **Step 1: Write a failing test for the column rendering**

Add to `tests/contrib/test_post_admin.py`:

```python
def test_post_list_renders_pinned_column(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        owner = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        site_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        # Promote the seeded draft to published + pinned.
        pinned = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        pinned.status = PostStatus.PUBLISHED
        pinned.published_at = datetime(2026, 5, 1, 12, 0)
        pinned.is_pinned = True
        # Add a second published-but-unpinned row.
        unpinned = Post(
            site_id=site_id, slug="b", title="Unpinned B",
            body_markdown="", body_html="", body_excerpt="",
            author_id=owner.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 2, 12, 0),
        )
        db.add(unpinned)
        db.commit()
        pinned_id, unpinned_id = pinned.id, unpinned.id

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'id="pinned-cell-{pinned_id}"' in body
    assert f'id="pinned-cell-{unpinned_id}"' in body
    pinned_idx = body.index(f"pinned-cell-{pinned_id}")
    assert "Unpin" in body[pinned_idx:pinned_idx + 500]
    unpinned_idx = body.index(f"pinned-cell-{unpinned_id}")
    assert "Pin" in body[unpinned_idx:unpinned_idx + 500]
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/contrib/test_post_admin.py::test_post_list_renders_pinned_column -v`
Expected: FAIL because the partial doesn't render a `pinned-cell-<id>` element yet.

- [ ] **Step 3: Add the column to `_post_list_table.html`**

Update `src/bragi/contrib/post/templates/admin/_post_list_table.html` — modify the table head and each `<tr>` to include the pinned cell, like so:

```html
{% if posts %}
<table>
  <thead>
    <tr>
      <th>Title</th>
      <th>Slug</th>
      <th>Status</th>
      <th>Pinned</th>
      <th>Published</th>
      <th>Updated</th>
      <th></th>
    </tr>
  </thead>
  <tbody>
    {% for post in posts %}
    <tr>
      <td><a href="{{ url_for('post_admin.edit_post', post_id=post.id) }}">{{ post.title }}</a></td>
      <td><code>{{ post.slug }}</code></td>
      <td>{{ post.status }}</td>
      {% include "admin/_pinned_cell.html" %}
      <td>{{ post.published_at.strftime('%Y-%m-%d') if post.published_at else '(none)' }}</td>
      <td>{{ post.updated_at.strftime('%Y-%m-%d') }}</td>
      <td>
        <form method="post"
              action="{{ url_for('post_admin.delete_post', post_id=post.id) }}"
              onsubmit="return confirm('Delete this post?');"
              style="display: inline; margin: 0;">
          <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
          <button type="submit" class="danger">Delete</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
```

- [ ] **Step 4: Pass `site` to both list-view templates**

`_pinned_cell.html` uses `site.slug` for the `url_for(...)` build. The current `list_posts` doesn't pass `site` — it only passes `posts`. Update both `render_template` calls in `src/bragi/contrib/post/admin.py:165-189`:

```python
    if is_htmx():
        return render_template("admin/_post_list_table.html", posts=posts, site=site)
    return render_template("admin/list.html", posts=posts, site=site)
```

- [ ] **Step 5: Add the htmx script include in the admin base if it isn't already present**

Run: `grep -n "htmx" src/bragi/templates/admin/base.html src/bragi/contrib/*/templates/admin/base.html 2>/dev/null | head -5`. If htmx is already loaded for admin pages (it is, for the TipTap editor and others), no change. Otherwise add a `<script src="https://unpkg.com/htmx.org@..."></script>` matching the existing pattern. (For bragi, htmx is loaded admin-wide already.)

- [ ] **Step 6: Run the rendering test**

Run: `poetry run pytest tests/contrib/test_post_admin.py::test_post_list_renders_pinned_column -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run: `poetry run pytest -q`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/bragi/contrib/post/templates/admin/_post_list_table.html src/bragi/contrib/post/admin.py tests/contrib/test_post_admin.py
git commit -m "Add Pinned column to admin post-list table

New column between Status and Published renders the
_pinned_cell.html partial. Buttons post via htmx to the
pin_toggle endpoint added in the previous commit; the same
partial is the swap target.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Add the pin fieldset to the edit form

**Files:**
- Modify: `src/bragi/contrib/post/templates/admin/edit.html`
- Modify: `src/bragi/contrib/post/admin.py:273-282` (extend the `form` dict in the GET branch)
- Test: `tests/contrib/test_post_admin.py`

- [ ] **Step 1: Write a failing test for the edit-form rendering**

Add to `tests/contrib/test_post_admin.py`:

```python
def test_edit_form_shows_pin_fieldset_for_published_post(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        post.is_pinned = True
        post.pinned_until = datetime(2026, 12, 31, 12, 0)
        db.commit()
        pid = post.id

    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/posts/{pid}/edit")
    body = resp.get_data(as_text=True)
    assert 'name="is_pinned"' in body
    assert "checked" in body
    assert 'name="pinned_until"' in body
    assert "2026-12-31T12:00" in body


def test_edit_form_hides_pin_fieldset_for_draft_post(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    # The seeded "hello" post starts as DRAFT; verify the fieldset
    # is absent for that case.
    with db_session_factory() as db:
        pid = db.execute(select(Post.id).where(Post.slug == "hello")).scalar_one()

    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/posts/{pid}/edit")
    body = resp.get_data(as_text=True)
    assert 'name="is_pinned"' not in body
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/contrib/test_post_admin.py -k "edit_form_shows_pin or edit_form_hides_pin" -v`
Expected: both FAIL.

- [ ] **Step 3: Extend the GET-branch form dict in `edit_post`**

In `src/bragi/contrib/post/admin.py:273-282`, modify the GET branch to include the two new fields:

```python
        if request.method == "GET":
            form = {
                "title": post.title,
                "slug": post.slug,
                "body_markdown": post.body_markdown,
                "status": post.status,
                "tags": ", ".join(t.label for t in post.tags),
                "og_image_id": str(post.og_image_id) if post.og_image_id else "",
                "is_pinned": "1" if post.is_pinned else "",
                "pinned_until": (
                    post.pinned_until.strftime("%Y-%m-%dT%H:%M")
                    if post.pinned_until
                    else ""
                ),
            }
            return render_template("admin/edit.html", post=post, form=form)
```

- [ ] **Step 4: Add the fieldset to `edit.html`**

In `src/bragi/contrib/post/templates/admin/edit.html`, immediately after the existing status `<fieldset>` (around line 42) and before the tags fieldset, add:

```html
  {% if form.get('status') == 'published' %}
  <fieldset>
    <label style="display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.25rem;">
      <input type="checkbox" name="is_pinned" value="1"
             {% if form.get('is_pinned') == '1' %}checked{% endif %}>
      Pin on landing page
    </label>
    <label for="pinned_until" style="margin-left: 1.5rem;">
      Optional auto-unpin date:
      <input type="datetime-local" name="pinned_until" id="pinned_until"
             value="{{ form.get('pinned_until', '') }}">
    </label>
    <small style="margin-left: 1.5rem;">Naive UTC. Leave blank for an open-ended pin.</small>
  </fieldset>
  {% endif %}
```

- [ ] **Step 5: Run the form tests**

Run: `poetry run pytest tests/contrib/test_post_admin.py -k "edit_form" -v`
Expected: both new PASS; existing form tests stay PASS.

- [ ] **Step 6: Run the full suite**

Run: `poetry run pytest -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/bragi/contrib/post/templates/admin/edit.html src/bragi/contrib/post/admin.py tests/contrib/test_post_admin.py
git commit -m "Add pin fieldset to the post-edit form

Checkbox + optional datetime-local for pinned_until, rendered
only when the current status is 'published' (pinning a draft
isn't editorially useful; the list-view button still allows it
for the future-intent case). Pre-fills from the live post on
edit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Delivery query — pinned set + recency exclusion + ETag

**Files:**
- Modify: `src/bragi/contrib/page/delivery.py:138-207` (`render_post_index_page`)
- Modify: `src/bragi/contrib/page/templates/delivery/post_index.html` (include the partial, see Task 8)
- Test: `tests/contrib/test_post_index.py`

- [ ] **Step 1: Write failing tests for the query behaviour**

The existing `delivery_app` fixture in `tests/contrib/test_post_index.py` seeds `blog.example.com` with 12 published posts (`Published 00`..`Published 11`), a draft, an archived, and a scheduled. The POST_INDEX page is at `/posts/`. `posts_per_page` is 5. The tests below promote specific seeded posts to pinned status via `db_session_factory()`; some create additional pinned rows. Add at the bottom of `tests/contrib/test_post_index.py`:

```python
def test_post_index_page1_renders_pinned_section_and_excludes_from_recency(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    # Pin "Published 11" (the newest of the seeded 12). Page 1
    # (per_page=5) would normally show Published 11..07.
    with db_session_factory() as db:
        p = db.execute(select(Post).where(Post.slug == "published-11")).scalar_one()
        p.is_pinned = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()

    assert 'aria-label="Pinned posts"' in body
    assert "Published 11" in body
    # The pinned post is in the carousel, NOT in the recency list.
    recency_idx = body.index('class="post-list"')
    pinned_idx = body.index('aria-label="Pinned posts"')
    assert pinned_idx < recency_idx
    assert "Published 11" not in body[recency_idx:]
    # The next 5 recency items are Published 10..06.
    for i in range(6, 11):
        assert f"Published {i:02d}" in body[recency_idx:]


def test_post_index_page2_reinstates_pinned_in_recency(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        # Pin "Published 03" so it would otherwise sit on page 2.
        p = db.execute(select(Post).where(Post.slug == "published-03")).scalar_one()
        p.is_pinned = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/?page=2", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'aria-label="Pinned posts"' not in body  # only on page 1
    assert "Published 03" in body                    # back in date order


def test_post_index_drops_post_with_expired_pinned_until(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        p = db.execute(select(Post).where(Post.slug == "published-10")).scalar_one()
        p.is_pinned = True
        # In the past relative to the seeded `base` dates (2026-05-01+).
        p.pinned_until = datetime(2026, 5, 5, 0, 0)
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'aria-label="Pinned posts"' not in body  # expired -> not pinned
    assert "Published 10" in body                    # but still in recency


def test_post_index_excludes_archived_posts_from_pinned_section(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        # The seeded "Archived" post; mark it pinned too.
        p = db.execute(select(Post).where(Post.slug == "archived-1")).scalar_one()
        p.is_pinned = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'aria-label="Pinned posts"' not in body
    assert "Archived" not in body  # still hidden because status=archived


def test_post_index_no_pinned_posts_renders_baseline_unchanged(
    delivery_app: Flask
) -> None:
    """Without any pinned posts, the carousel section is absent and
    the page is byte-for-byte the same as it was pre-feature
    (modulo HTML whitespace tolerance handled by the absent
    aria-label assertion)."""
    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'aria-label="Pinned posts"' not in body
    # Sanity: usual recency content still renders.
    assert "Published 11" in body
```

- [ ] **Step 2: Run to verify the tests fail**

Run: `poetry run pytest tests/contrib/test_post_index.py -k "pinned or expired or archived" -v`
Expected: all FAIL (the pinned section isn't rendered yet).

- [ ] **Step 3: Modify `render_post_index_page` to query the pinned set**

In `src/bragi/contrib/page/delivery.py`, update the imports near line 64 to include `or_`:

```python
from sqlalchemy import func, or_, select
```

Replace the body of `render_post_index_page` from `with SessionLocal() as db:` (line 154) through the end of the function with:

```python
    with SessionLocal() as db:
        now = naive_utcnow()

        # Pinned set, scoped to site, only on page 1. Posts are
        # "currently pinned" when is_pinned AND no expiry has passed.
        pinned_posts: list[Post] = []
        if page_n == 1:
            pinned_posts = (
                db.execute(
                    select(Post)
                    .where(
                        Post.site_id == site.id,
                        Post.status == PostStatus.PUBLISHED,
                        Post.is_pinned.is_(True),
                        or_(
                            Post.pinned_until.is_(None),
                            Post.pinned_until > now,
                        ),
                    )
                    .order_by(Post.published_at.desc())
                )
                .scalars()
                .all()
            )
        pinned_ids = {p.id for p in pinned_posts}

        base = select(Post).where(
            Post.site_id == site.id,
            Post.status == PostStatus.PUBLISHED,
        )
        if pinned_ids:
            base = base.where(Post.id.notin_(pinned_ids))

        total = db.execute(
            select(func.count()).select_from(base.subquery())
        ).scalar_one()
        total_pages = max(1, (total + per_page - 1) // per_page)
        if page_n > total_pages and total > 0:
            abort(404)
        if total == 0 and page_n > 1:
            abort(404)

        posts = (
            db.execute(
                base.order_by(Post.published_at.desc())
                .limit(per_page)
                .offset((page_n - 1) * per_page)
            )
            .scalars()
            .all()
        )

        # ETag inputs include pinned_posts' updated_at (they're rendered
        # on page 1) plus a minute-truncated min(pinned_until) so the
        # cached response invalidates when an expiry passes.
        candidates = [p.updated_at for p in posts] + [page.updated_at]
        candidates.extend(p.updated_at for p in pinned_posts)
        last_modified = max(candidates)

        expiry_key = ""
        pinned_with_expiry = [p.pinned_until for p in pinned_posts if p.pinned_until]
        if pinned_with_expiry:
            min_exp = min(pinned_with_expiry)
            expiry_key = min_exp.strftime("%Y%m%d%H%M")

        etag = etag_for(
            "post_index",
            f"{site.id}|{page.id}|{page_n}|{per_page}|{expiry_key}",
            last_modified,
        )
        not_modified = maybe_304(request, etag=etag, last_modified=last_modified)
        if not_modified is not None:
            return not_modified

        body = render_template(
            "delivery/post_index.html",
            site=site,
            page=page,
            posts=posts,
            pinned_posts=pinned_posts,
            page_n=page_n,
            total_pages=total_pages,
            has_prev=page_n > 1,
            has_next=page_n < total_pages,
            meta_description=page.meta_description or page.body_excerpt or None,
            canonical_url=(
                f"{site.canonical_url}{page_url_for(page, db=db)}" if site.canonical_url else None
            ),
            og_image_url=og_image_url_for(item=page, site=site, db=db),
        )
        response = make_response(body)
        attach_validators(response, etag=etag, last_modified=last_modified)
        return response
```

- [ ] **Step 4: Run the query tests**

Run: `poetry run pytest tests/contrib/test_post_index.py -k "pinned or expired or archived" -v`

Expected: the `pinned_section_and_excludes_from_recency`, `page2_reinstates`, `drops_post_with_expired`, `excludes_archived`, and `no_pinned_posts_renders_identically_to_baseline` tests still fail their `aria-label` assertions (the template doesn't render the carousel yet). The query-only assertions (exclusion from recency, page 2 behaviour, no aria-label when no pins) should be in a usable state. Task 8 wires the template; defer the assertion failures involving rendered HTML until then.

It's OK to commit at this point if the query side is correct — Task 8 finishes the rendering.

- [ ] **Step 5: Run the full suite to make sure existing tests didn't regress**

Run: `poetry run pytest -q`
Expected: existing tests still PASS; the new tests partially fail on `aria-label` (carousel template missing). That's expected; Task 8 finishes them.

- [ ] **Step 6: Commit**

```bash
git add src/bragi/contrib/page/delivery.py tests/contrib/test_post_index.py
git commit -m "Query currently-pinned set on page 1; exclude from recency

render_post_index_page now resolves Posts with is_pinned AND
(pinned_until IS NULL OR pinned_until > now) on page 1, excludes
their IDs from the recency list, and folds min(pinned_until)
(truncated to the minute) plus pinned posts' updated_at into the
ETag. Page 2+ behaves unchanged — pinned posts reappear in their
natural date position.

Template integration (the actual carousel section) lands in the
next commit; the query is independently testable.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Pinned carousel template + theme CSS

**Files:**
- Create: `src/bragi/contrib/page/templates/delivery/_pinned_carousel.html`
- Modify: `src/bragi/contrib/page/templates/delivery/post_index.html`
- Modify: `src/bragi/contrib/theme_default/templates/delivery/base.html` (CSS)
- Modify: `src/bragi/contrib/theme_minimal/templates/delivery/base.html` (CSS)
- Modify: `src/bragi/contrib/theme_serif/templates/delivery/base.html` (CSS)
- Modify: `src/bragi/contrib/theme_terminal/templates/delivery/base.html` (CSS)
- Test: `tests/contrib/test_post_index.py`

- [ ] **Step 1: Add rendering-shape tests**

Append to `tests/contrib/test_post_index.py`:

```python
def test_pinned_section_html_shape_multi(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        for slug in ("published-11", "published-10", "published-09"):
            p = db.execute(select(Post).where(Post.slug == slug)).scalar_one()
            p.is_pinned = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'aria-label="Pinned posts"' in body
    assert 'class="pinned-strip"' in body
    assert body.count('class="pinned-card"') == 3
    assert 'class="pinned-dots"' in body
    assert body.count('href="#pinned-') == 3


def test_pinned_section_html_shape_single_no_dots(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        p = db.execute(select(Post).where(Post.slug == "published-11")).scalar_one()
        p.is_pinned = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'class="pinned-card"' in body
    assert 'class="pinned-dots"' not in body
```

- [ ] **Step 2: Run to verify they fail**

Run: `poetry run pytest tests/contrib/test_post_index.py::test_pinned_section_html_shape_multi tests/contrib/test_post_index.py::test_pinned_section_html_shape_single_no_dots -v`
Expected: FAIL.

- [ ] **Step 3: Create the carousel partial**

Create `src/bragi/contrib/page/templates/delivery/_pinned_carousel.html`:

```html
{# Pinned carousel section, rendered above the recency list on
   page 1 of the post-index page. CSS scroll-snap; dot indicators
   via anchor links and :target. Single-pin case omits the dots. #}
{% if pinned_posts %}
<section class="pinned" aria-label="Pinned posts">
  <h2 class="pinned-label">Pinned</h2>
  <div class="pinned-strip" role="region" tabindex="0">
    {% for post in pinned_posts %}
      <article class="pinned-card" id="pinned-{{ post.id }}">
        {% if post.featured_image_id %}
          <img src="{{ attachment_url(post.featured_image) }}" alt="" loading="lazy">
        {% endif %}
        <h3><a href="{{ url_for_post(post) }}">{{ post.title }}</a></h3>
        <time datetime="{{ post.published_at.isoformat() }}" class="meta">
          {{ post.published_at.strftime('%Y-%m-%d') }}
        </time>
        {% if post.body_excerpt %}<p class="excerpt">{{ post.body_excerpt }}</p>{% endif %}
      </article>
    {% endfor %}
  </div>
  {% if pinned_posts|length > 1 %}
    <nav class="pinned-dots" aria-label="Pinned carousel pagination">
      {% for post in pinned_posts %}
        <a href="#pinned-{{ post.id }}" aria-label="Go to pinned post {{ loop.index }}"></a>
      {% endfor %}
    </nav>
    {# Active-dot styling: per-card :target ~ sibling selector.
       Inlined here so single-pin pages don't carry the extra rules. #}
    <style>
      {% for post in pinned_posts %}
      #pinned-{{ post.id }}:target ~ .pinned-dots a[href="#pinned-{{ post.id }}"] {
        background: var(--dot-active, var(--fg, #475569));
      }
      {% endfor %}
    </style>
  {% endif %}
</section>
{% endif %}
```

- [ ] **Step 4: Include the partial from `post_index.html` and refine the empty-state message**

Modify `src/bragi/contrib/page/templates/delivery/post_index.html` to (a) include the partial between the intro div and the recency `<ul>`, and (b) reword the empty-state to "No more posts yet" when pinned posts ARE present and the recency list is empty:

```html
{% if page.body_html %}
<div class="intro">
  {{ page.body_html | internal_link_rewrite | safe }}
</div>
{% endif %}
{% include "delivery/_pinned_carousel.html" %}
{% if posts %}
<ul class="post-list">
```

And replace the existing `{% else %}<p>No posts yet.</p>{% endif %}` near the end of the template with:

```html
{% else %}
<p>{{ "No more posts yet." if pinned_posts else "No posts yet." }}</p>
{% endif %}
```

The partial's own `{% if pinned_posts %}` guard means it's a no-op when there are no pins; the empty-state message stays "No posts yet." in that case (unchanged behaviour).

- [ ] **Step 5: Add the carousel CSS to `theme_default`**

Inside the `<style>` block in `src/bragi/contrib/theme_default/templates/delivery/base.html`, append before `</style>`:

```css
    /* Pinned carousel on the post-index landing page. */
    .pinned { margin: 1.5rem 0; }
    .pinned-label {
      font-size: 0.875rem; text-transform: uppercase;
      letter-spacing: 0.05em; margin: 0 0 0.5rem 0;
      color: var(--muted);
    }
    .pinned-strip {
      display: flex; gap: 1rem;
      overflow-x: auto;
      scroll-snap-type: x mandatory;
      scroll-behavior: smooth;
    }
    .pinned-card {
      flex: 0 0 min(60%, 24rem);
      scroll-snap-align: start;
      border: 1px solid var(--rule);
      border-radius: 4px;
      overflow: hidden;
      background: var(--bg);
    }
    .pinned-card img { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }
    .pinned-card h3 { margin: 0.5rem 0.75rem 0.25rem; font-size: 1rem; }
    .pinned-card h3 a { text-decoration: none; }
    .pinned-card time { display: block; margin: 0 0.75rem; font-size: 0.85em; color: var(--muted); }
    .pinned-card p.excerpt { margin: 0.5rem 0.75rem 0.75rem; font-size: 0.9em; }
    .pinned-dots {
      display: flex; gap: 0.5rem; justify-content: center;
      margin-top: 0.75rem;
    }
    .pinned-dots a {
      width: 0.5rem; height: 0.5rem; border-radius: 50%;
      background: var(--rule);
      display: inline-block;
      transition: background 0.15s ease;
    }
```

- [ ] **Step 6: Add the same CSS block to the other three themes**

Repeat the same `<style>` insertion for the other three theme `base.html` files:

```bash
for theme in theme_minimal theme_serif theme_terminal; do
  echo "Adding to $theme — open and append the same CSS block before </style>"
done
```

Each theme already defines `--rule`, `--muted`, `--bg`, `--fg`, so the variables resolve. The structural CSS is identical; only typography choices differ per theme, and the carousel inherits.

- [ ] **Step 7: Run the rendering tests + the query tests from Task 7**

Run: `poetry run pytest tests/contrib/test_post_index.py -v`
Expected: ALL pinned-related tests PASS.

- [ ] **Step 8: Run the full suite**

Run: `poetry run pytest -q`
Expected: all PASS.

- [ ] **Step 9: Manual visual smoke**

Boot the delivery app and check the rendered page in a browser:

```bash
poetry run alembic upgrade head
poetry run bragi-delivery --port 8002 &
# create some posts via the admin app, pin a couple, then hit
# http://<your-test-site-host>:8002/ and visually confirm:
#  - section "Pinned" sits between intro and recency list
#  - cards scroll-snap horizontally
#  - dots show below the strip
#  - clicking a dot snaps to its card; the active dot colours
#  - single pin: card alone, no dots
```

- [ ] **Step 10: Commit**

```bash
git add src/bragi/contrib/page/templates/delivery/_pinned_carousel.html \
        src/bragi/contrib/page/templates/delivery/post_index.html \
        src/bragi/contrib/theme_default/templates/delivery/base.html \
        src/bragi/contrib/theme_minimal/templates/delivery/base.html \
        src/bragi/contrib/theme_serif/templates/delivery/base.html \
        src/bragi/contrib/theme_terminal/templates/delivery/base.html \
        tests/contrib/test_post_index.py
git commit -m "Render pinned carousel on the post-index page

New _pinned_carousel.html partial; included from post_index.html
between the intro and the recency list. CSS scroll-snap with
anchor-link dot indicators (active dot via :target selector,
generated per-pin so single-pin pages skip the dots block
entirely). Carousel CSS lands in all four in-tree themes; uses
existing --rule, --muted, --bg, --fg vars so theme typography
flows through unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Landing-page integration test + final-pass cleanup

**Files:**
- Modify: `tests/integration/test_landing_page.py`

- [ ] **Step 1: Add an integration scenario**

The existing `delivery_app` fixture in `tests/integration/test_landing_page.py` seeds `blog.example.com` + one POST_INDEX page (id stashed at `delivery_app.extensions["_test_blog_index_id"]`) + one published Post (`first`). To test pinned posts on the landing page, promote the POST_INDEX page to home (mirrors `test_root_renders_post_index_listing_when_home_set_to_blog_index`), add two pinned posts, and assert the carousel + dedup at `/`.

Append to `tests/integration/test_landing_page.py`:

```python
def test_root_renders_pinned_carousel_above_recency(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    blog_index_id = delivery_app.extensions["_test_blog_index_id"]
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.home_page_id = blog_index_id
        user_id = site.owner_user_id
        # Pin "First Post" (already seeded by the fixture as published).
        first = db.execute(select(Post).where(Post.slug == "first")).scalar_one()
        first.is_pinned = True
        # Add a second pinned post so the carousel renders dots.
        second = Post(
            site_id=site.id, slug="second", title="Second Post",
            body_markdown="x", body_html="<p>x</p>", body_excerpt="Second excerpt.",
            author_id=user_id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 13, tzinfo=UTC),
            is_pinned=True,
        )
        # Add an unpinned post to populate the recency list below.
        third = Post(
            site_id=site.id, slug="third", title="Third Post",
            body_markdown="x", body_html="<p>x</p>", body_excerpt="Third excerpt.",
            author_id=user_id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 12, tzinfo=UTC),
        )
        db.add_all([second, third])
        db.commit()
        first_id, second_id = first.id, second.id

    resp = delivery_app.test_client().get("/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()

    # Carousel section + per-card IDs + dots (>=2 pins)
    assert 'aria-label="Pinned posts"' in body
    assert f'id="pinned-{first_id}"' in body
    assert f'id="pinned-{second_id}"' in body
    assert 'class="pinned-dots"' in body

    # Page 1 recency list excludes pinned posts; "Third Post" remains
    recency_idx = body.index('class="post-list"')
    assert "First Post" not in body[recency_idx:]
    assert "Second Post" not in body[recency_idx:]
    assert "Third Post" in body[recency_idx:]
```

- [ ] **Step 2: Run it**

Run: `poetry run pytest tests/integration/test_landing_page.py -v`
Expected: PASS.

- [ ] **Step 3: Lint + format + mypy + full suite**

Run all the gates:

```bash
poetry run ruff check src/ tests/ alembic/
poetry run ruff format --check src/ tests/ alembic/
poetry run mypy src/
poetry run pytest -q
```

If `ruff format --check` fails on any of the touched files, run `poetry run ruff format $(git diff --name-only -- '*.py')` (scope-limited per the portfolio CLAUDE.md "Scope formatters" rule), then re-check.

Expected: all green.

- [ ] **Step 4: Reproduce the CI environment locally**

Move the dev DB aside and rerun pytest to confirm there's no hidden dependency on a pre-populated `bragi.db`:

```bash
mv bragi.db bragi.db.dev-backup
poetry run pytest -q
mv bragi.db.dev-backup bragi.db
rm -f bragi.db-wal bragi.db-shm  # only if they showed up after the run
```

Expected: same green result.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_landing_page.py
git commit -m "Integration test: pinned posts on the landing page

Full-stack scenario where the post-index page is the home page
and two posts are pinned. Asserts the section landmarks, per-
pin IDs, dots block, and that the recency list on page 1
excludes the pinned posts.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: CHANGELOG + PR

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add an entry to `[Unreleased]`**

In `CHANGELOG.md`, under `## [Unreleased]`, add an `### Added` subsection (or extend an existing one) with:

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "Add Pinned posts to CHANGELOG (#125)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 3: Push and open the PR**

```bash
git push origin feature/pinned-posts
gh pr create --base develop --title "Pinned posts on the landing page (#125)" --body "$(cat <<'EOF'
## Summary
- Adds `posts.is_pinned` and `posts.pinned_until` columns; alembic migration with a partial index on `(site_id, is_pinned) WHERE is_pinned`.
- Admin edit-form fieldset (only when status is `published`) + per-row Pin/Unpin button on the post list (htmx, audit-logged).
- Delivery query pulls the currently-pinned set on page 1; ETag folds `min(pinned_until)` so cached pages invalidate on auto-expiry.
- New `_pinned_carousel.html` partial; CSS scroll-snap with anchor-link dot indicators across all four in-tree themes. Single-pin case skips the dots.
- Pinned posts are removed from the page-1 recency list and reappear in date position on page 2+.

Closes #125. Design spec: `docs/superpowers/specs/2026-05-25-pinned-posts-landing-page-design.md`.

## Test plan
- [x] `poetry run ruff check src/ tests/ alembic/`
- [x] `poetry run ruff format --check src/ tests/ alembic/`
- [x] `poetry run mypy src/`
- [x] `poetry run pytest` — full suite green
- [x] `mv bragi.db bragi.db.dev-backup && poetry run pytest` — CI-like green
- [x] Alembic up-down-up smoke on a fresh SQLite
- [x] Manual visual smoke: pin a couple of posts, confirm carousel renders, dot navigation works, page-2 reinstatement works

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Acceptance criteria (from the spec)

These must all hold when the PR opens; verify before merging:

1. Migration applies cleanly on a fresh SQLite and against the `bragi.db.dev-backup` from a recent CI run.
2. Pinning a published post via the edit form makes it appear in the pinned section on the post-index page.
3. Pinning via the list-view button has the same effect, returns the updated cell via htmx, and writes an AuditLog row.
4. With one pin: a card renders with no dot block.
5. With multiple pins: the strip scroll-snaps, the dots are anchor-linked, `:target` colours the active one.
6. Pinned posts are excluded from the page-1 recency list and reinstated on page 2+.
7. `pinned_until` in the past silently drops the post from the pinned section; ETag changes at the boundary.
8. The page renders identically to today's output when no posts are pinned.
9. CHANGELOG, tests, lint, mypy, and migration up-down-up smoke all green.
