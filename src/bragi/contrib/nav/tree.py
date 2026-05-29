"""Pure tree-building function for the auto-nav.

No Flask, no DB session, no request context. Callers pass an
already-fetched list of Page rows; the builder filters, sorts,
and assembles the two-level tree per the rules pinned in the
design spec at
`_claude/specs/2026-05-28-pages-auto-nav-design.md`.

Rules:

1. Only `status='published'` pages are considered.
2. Only pages with `show_in_nav=True` are considered.
3. Top-level (`parent_id IS NULL`) pages become NavNode roots,
   sorted by `(menu_order, title)`.
4. For each root, direct children (`parent_id == root.id`) become
   NavNode children, sorted the same way.
5. Grandchildren and below are never surfaced (one-level cap).
6. Hiding a parent (filter rule 1 or 2) hides the whole subtree;
   children are NOT promoted to top level.
7. After tree assembly, any NavNode whose `page.id == home_page_id`
   is removed from wherever it appears (top level or child list).
   The brand link covers `/` already.
"""

from __future__ import annotations

from bragi.api import NavNode
from bragi.core.models.page import Page, PageStatus


def build_nav_tree(pages: list[Page], *, home_page_id: int | None) -> list[NavNode]:
    """Build the auto-nav tree from an in-memory list of pages."""
    visible: dict[int, Page] = {
        p.id: p for p in pages if p.status == PageStatus.PUBLISHED and p.show_in_nav
    }

    def sort_key(p: Page) -> tuple[int, str]:
        return (p.menu_order, p.title or "")

    # Build root list: parent_id is None AND the page itself is
    # visible. (A None parent_id with a visible page is a candidate
    # root regardless of any siblings.)
    roots = sorted(
        (p for p in visible.values() if p.parent_id is None),
        key=sort_key,
    )

    nodes: list[NavNode] = []
    for root in roots:
        if root.id == home_page_id:
            continue
        children_pages = sorted(
            (p for p in visible.values() if p.parent_id == root.id),
            key=sort_key,
        )
        children = [NavNode(page=c, children=[]) for c in children_pages if c.id != home_page_id]
        nodes.append(NavNode(page=root, children=children))
    return nodes
