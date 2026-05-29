"""Unit tests for the auto-nav tree builder.

Pure-function shape: build a list of Page instances in-memory,
pass it to `build_nav_tree`, assert the returned tree matches.
No Flask, no DB session.
"""

from __future__ import annotations

from bragi.contrib.nav.tree import build_nav_tree
from bragi.core.models.page import Page, PageKind, PageStatus


def _page(
    *,
    id: int,
    slug: str,
    title: str,
    parent_id: int | None = None,
    status: str = PageStatus.PUBLISHED,
    show_in_nav: bool = True,
    menu_order: int = 0,
    kind: str = PageKind.STATIC,
) -> Page:
    """Build a Page instance without touching the DB."""
    p = Page(
        site_id=1,
        slug=slug,
        title=title,
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=1,
        status=status,
        kind=kind,
        parent_id=parent_id,
        show_in_nav=show_in_nav,
        menu_order=menu_order,
    )
    # SQLAlchemy doesn't autopopulate `id` until flush; set it
    # manually so the builder's parent/child resolution works.
    p.id = id
    return p


def test_empty_input_returns_empty_list() -> None:
    assert build_nav_tree([], home_page_id=None) == []


def test_only_published_pages_appear() -> None:
    pages = [
        _page(id=1, slug="a", title="A", status=PageStatus.PUBLISHED),
        _page(id=2, slug="b", title="B", status=PageStatus.DRAFT),
        _page(id=3, slug="c", title="C", status=PageStatus.ARCHIVED),
    ]
    tree = build_nav_tree(pages, home_page_id=None)
    assert [n.page.id for n in tree] == [1]


def test_show_in_nav_false_excluded() -> None:
    pages = [
        _page(id=1, slug="a", title="A", show_in_nav=True),
        _page(id=2, slug="b", title="B", show_in_nav=False),
    ]
    tree = build_nav_tree(pages, home_page_id=None)
    assert [n.page.id for n in tree] == [1]


def test_sort_by_menu_order_then_title() -> None:
    pages = [
        _page(id=1, slug="z", title="Z", menu_order=10),
        _page(id=2, slug="a", title="A", menu_order=10),
        _page(id=3, slug="m", title="M", menu_order=1),
    ]
    tree = build_nav_tree(pages, home_page_id=None)
    assert [n.page.id for n in tree] == [3, 2, 1]


def test_one_level_deep_children_populated() -> None:
    pages = [
        _page(id=1, slug="parent", title="Parent"),
        _page(id=2, slug="child-a", title="Child A", parent_id=1, menu_order=0),
        _page(id=3, slug="child-b", title="Child B", parent_id=1, menu_order=1),
    ]
    tree = build_nav_tree(pages, home_page_id=None)
    assert len(tree) == 1
    assert tree[0].page.id == 1
    assert [c.page.id for c in tree[0].children] == [2, 3]


def test_grandchildren_omitted_even_when_visible() -> None:
    pages = [
        _page(id=1, slug="parent", title="Parent"),
        _page(id=2, slug="child", title="Child", parent_id=1),
        _page(id=3, slug="grandchild", title="GrandChild", parent_id=2),
    ]
    tree = build_nav_tree(pages, home_page_id=None)
    assert len(tree) == 1
    assert [c.page.id for c in tree[0].children] == [2]
    # The grandchild is reachable by URL but does not appear in nav.


def test_hidden_parent_hides_subtree() -> None:
    pages = [
        _page(id=1, slug="parent", title="Parent", show_in_nav=False),
        _page(id=2, slug="child", title="Child", parent_id=1, show_in_nav=True),
    ]
    tree = build_nav_tree(pages, home_page_id=None)
    # Parent hidden; child does NOT get promoted to top level.
    assert tree == []


def test_home_page_dropped() -> None:
    pages = [
        _page(id=1, slug="home", title="Home"),
        _page(id=2, slug="about", title="About"),
    ]
    tree = build_nav_tree(pages, home_page_id=1)
    assert [n.page.id for n in tree] == [2]


def test_home_page_drop_works_for_child_too() -> None:
    # If the operator promoted a child page to home, the drop still
    # removes it from wherever it sits in the tree.
    pages = [
        _page(id=1, slug="parent", title="Parent"),
        _page(id=2, slug="featured", title="Featured", parent_id=1),
        _page(id=3, slug="other", title="Other", parent_id=1),
    ]
    tree = build_nav_tree(pages, home_page_id=2)
    assert len(tree) == 1
    assert [c.page.id for c in tree[0].children] == [3]


def test_orphan_child_pointing_at_missing_parent_omitted() -> None:
    # Defensive: a Page with parent_id pointing at a non-existent or
    # filtered-out page (e.g. parent is draft) does not get promoted
    # to top level. The page is still reachable by URL.
    pages = [
        _page(id=1, slug="parent", title="Parent", status=PageStatus.DRAFT),
        _page(id=2, slug="child", title="Child", parent_id=1),
    ]
    tree = build_nav_tree(pages, home_page_id=None)
    assert tree == []
