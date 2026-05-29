"""Integration: auto-nav rendering across themes + multisite + home suppression.

Lives in the integration tier because it exercises the full
delivery stack: Host-header site resolution, plugin manager,
theme loader, Jinja env, partial inclusion.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.page import Page, PageKind, PageStatus
from tests.conftest import make_test_site, make_test_user, seed_blog_index


@pytest.fixture
def two_sites(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> dict[str, object]:
    """Seed two sites; site A has a small tree, site B has a single
    page. Used to confirm multisite scoping on the nav."""
    user = make_test_user(db_session)
    site_a = make_test_site(
        db_session,
        hostname="a.example",
        title="Site A",
        slug="a",
        theme="default",
        canonical_url="https://a.example",
        owner_user_id=user.id,
        commit=False,
    )
    site_b = make_test_site(
        db_session,
        hostname="b.example",
        title="Site B",
        slug="b",
        theme="default",
        canonical_url="https://b.example",
        owner_user_id=user.id,
        commit=False,
    )
    # Seed blog indexes so the welcome-fallback path has a working
    # site shell (the fallback extends base.html which includes the
    # nav partial; without the correct g.site the partial returns []).
    seed_blog_index(db_session, site_a, commit=False)
    seed_blog_index(db_session, site_b, commit=False)

    # Site A: Projects (top, order 1), About (top, order 10) with
    # Bio child, and one hidden page.
    about = Page(
        site_id=site_a.id,
        slug="about",
        title="About",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
        menu_order=10,
        show_in_nav=True,
    )
    db_session.add(about)
    db_session.flush()
    db_session.add_all(
        [
            Page(
                site_id=site_a.id,
                slug="projects",
                title="Projects",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
                kind=PageKind.STATIC,
                menu_order=1,
                show_in_nav=True,
            ),
            Page(
                site_id=site_a.id,
                slug="bio",
                title="Bio",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
                kind=PageKind.STATIC,
                parent_id=about.id,
                menu_order=0,
                show_in_nav=True,
            ),
            Page(
                site_id=site_a.id,
                slug="hidden",
                title="Hidden",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
                kind=PageKind.STATIC,
                menu_order=0,
                show_in_nav=False,
            ),
        ]
    )
    # Site B: a single page so we can prove it doesn't leak into A.
    db_session.add(
        Page(
            site_id=site_b.id,
            slug="leaky",
            title="Leaky",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.STATIC,
            menu_order=0,
            show_in_nav=True,
        )
    )
    db_session.commit()
    return {"a": site_a, "b": site_b, "about_id": about.id}


@pytest.fixture
def delivery_app(two_sites: dict[str, object]) -> Iterator[Flask]:  # noqa: ARG001
    """Delivery app created after DB is seeded and session is patched.

    `two_sites` is listed as a dependency even though the test doesn't
    receive it directly; this ensures the session factory is patched
    (via `patched_session_locals` in the `two_sites` fixture chain)
    before `create_delivery_app()` is called.
    """
    yield create_delivery_app()


@pytest.mark.parametrize("theme", ["default", "minimal", "serif", "terminal"])
def test_nav_renders_in_each_theme(
    two_sites: dict[str, object],
    delivery_app: Flask,
    theme: str,
    db_session: Session,
) -> None:
    site_a = two_sites["a"]
    site_a.theme = theme  # type: ignore[attr-defined]
    db_session.commit()
    resp = delivery_app.test_client().get("/", headers={"Host": "a.example"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '<nav class="site-nav"' in body, f"theme={theme}: nav element not found"
    # menu_order=1 (Projects) appears before menu_order=10 (About).
    assert body.find("Projects") < body.find("About"), f"theme={theme}: order wrong"
    # Bio is a child of About; it appears via the submenu.
    assert "Bio" in body, f"theme={theme}: child page Bio not found"
    # Hidden page (show_in_nav=False) must not appear.
    assert "Hidden" not in body, f"theme={theme}: hidden page leaked into nav"


def test_nav_does_not_leak_across_sites(
    two_sites: dict[str, object],
    delivery_app: Flask,
) -> None:
    client = delivery_app.test_client()
    body_a = client.get("/", headers={"Host": "a.example"}).get_data(as_text=True)
    body_b = client.get("/", headers={"Host": "b.example"}).get_data(as_text=True)
    assert "Leaky" not in body_a
    assert "About" not in body_b
    assert "Leaky" in body_b


def test_home_page_promoted_page_is_dropped(
    two_sites: dict[str, object],
    delivery_app: Flask,
    db_session: Session,
) -> None:
    site_a = two_sites["a"]
    # Promote `About` to be site A's home page. The nav plugin must
    # then drop it (and its Bio child) from the tree because the
    # brand link already covers the `/` URL.
    site_a.home_page_id = two_sites["about_id"]  # type: ignore[attr-defined]
    db_session.commit()
    body = delivery_app.test_client().get("/", headers={"Host": "a.example"}).get_data(as_text=True)
    # About is now `/`; it must not also appear as a nav link.
    # The Bio child is also dropped because its parent is gone.
    # Isolate the nav block so the article's own <h1>About</h1>
    # doesn't produce a false positive: the nav partial renders a
    # <nav class="site-nav" ...> block; search within that range.
    nav_start = body.find('<nav class="site-nav"')
    nav_end = body.find("</nav>", nav_start) if nav_start != -1 else -1
    assert nav_start != -1, "nav element not found at all"
    nav_block = body[nav_start:nav_end]
    assert "About" not in nav_block, "About must not appear as a nav link when promoted to home"
    assert "Bio" not in nav_block, "Bio must not appear as a nav link when its parent is promoted"
    # Projects (unchanged) still appears in the nav.
    assert "Projects" in nav_block
