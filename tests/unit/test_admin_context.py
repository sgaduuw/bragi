"""Unit tests for the admin chrome's nav-grouping helper.

`_group_nav_by_section` takes a list of NavItems and returns a list
of (section_name, items_in_weight_order) tuples, ordered by
`_SECTION_RANK` (write -> reach -> manage for site groups; platform
for the lone global group; unknown sections fall to rank 1, sorted
alphabetically among that rank). Within a section, items keep input
order (callers pre-sort by weight, so input order IS weight order).
"""

from __future__ import annotations

from bragi.api import NavItem
from bragi.apps.admin import _group_nav_by_section


def _ni(label: str, section: str, weight: int = 100) -> NavItem:
    return NavItem(label=label, endpoint=f"x.{label}", section=section, weight=weight)


def test_empty_in_empty_out() -> None:
    assert _group_nav_by_section([]) == []


def test_single_section() -> None:
    items = [_ni("Posts", "write", 10), _ni("Pages", "write", 20)]
    groups = _group_nav_by_section(items)
    assert groups == [("write", items)]


def test_site_groups_ordered_write_reach_manage() -> None:
    """The shipped site-section order is the load-bearing assertion:
    write first, manage last, reach between (regardless of input order)."""
    items = [
        _ni("Site settings", "manage", 10),
        _ni("Analytics", "reach", 10),
        _ni("Posts", "write", 10),
    ]
    groups = _group_nav_by_section(items)
    assert [name for name, _ in groups] == ["write", "reach", "manage"]


def test_unknown_section_sorts_among_rank1() -> None:
    """An unknown section falls to rank 1 (alongside reach) and sorts
    alphabetically there: write(0) < {misc, reach}(1) < manage(2)."""
    items = [
        _ni("Site settings", "manage", 10),
        _ni("Analytics", "reach", 10),
        _ni("Posts", "write", 10),
        _ni("Something", "misc", 10),
    ]
    groups = _group_nav_by_section(items)
    assert [name for name, _ in groups] == ["write", "misc", "reach", "manage"]


def test_within_section_preserves_input_order() -> None:
    """Callers pre-sort by weight; the helper preserves that order."""
    items = [
        _ni("Posts", "write", 10),
        _ni("Pages", "write", 20),
        _ni("Media", "write", 30),
        _ni("Datasets", "write", 40),
    ]
    groups = _group_nav_by_section(items)
    assert len(groups) == 1
    section_name, in_order = groups[0]
    assert section_name == "write"
    assert [i.label for i in in_order] == ["Posts", "Pages", "Media", "Datasets"]
