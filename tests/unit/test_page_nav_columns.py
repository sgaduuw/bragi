"""Page rows expose show_in_nav and menu_order with safe defaults."""

from __future__ import annotations

from sqlalchemy.orm import Session

from bragi.core.models.page import Page, PageKind, PageStatus
from tests.conftest import make_test_site, make_test_user


def test_page_show_in_nav_defaults_true(db_session: Session) -> None:
    site = make_test_site(db_session, slug="t", hostname="t.example", title="t")
    user = make_test_user(db_session)
    page = Page(
        site_id=site.id,
        slug="about",
        title="About",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
    )
    db_session.add(page)
    db_session.commit()
    assert page.show_in_nav is True


def test_page_menu_order_defaults_zero(db_session: Session) -> None:
    site = make_test_site(db_session, slug="t2", hostname="t2.example", title="t2")
    user = make_test_user(db_session)
    page = Page(
        site_id=site.id,
        slug="about",
        title="About",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
    )
    db_session.add(page)
    db_session.commit()
    assert page.menu_order == 0


def test_page_nav_columns_explicit_values_persist(db_session: Session) -> None:
    site = make_test_site(db_session, slug="t3", hostname="t3.example", title="t3")
    user = make_test_user(db_session)
    page = Page(
        site_id=site.id,
        slug="hidden",
        title="Hidden",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
        show_in_nav=False,
        menu_order=42,
    )
    db_session.add(page)
    db_session.commit()
    db_session.refresh(page)
    assert page.show_in_nav is False
    assert page.menu_order == 42
