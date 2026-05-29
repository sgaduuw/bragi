"""Unit tests for the admin chrome's nav-grouping helper.

`_group_nav_by_section` takes a list of NavItems and returns a
list of (section_name, items_in_weight_order) tuples, ordered
with `content` first and `system` last (other sections
alphabetical between). Within a section, items keep input order
(callers pre-sort by weight, so input order IS weight order).
"""

from __future__ import annotations

from bragi.api import NavItem
from bragi.apps.admin import _group_nav_by_section


def _ni(label: str, section: str, weight: int = 100) -> NavItem:
    return NavItem(label=label, endpoint=f"x.{label}", section=section, weight=weight)


def test_empty_in_empty_out() -> None:
    assert _group_nav_by_section([]) == []


def test_single_section() -> None:
    items = [_ni("Posts", "content", 10), _ni("Pages", "content", 20)]
    groups = _group_nav_by_section(items)
    assert groups == [("content", items)]


def test_content_pinned_first() -> None:
    items = [
        _ni("Audit log", "system", 30),
        _ni("Posts", "content", 10),
    ]
    groups = _group_nav_by_section(items)
    assert [name for name, _ in groups] == ["content", "system"]


def test_system_pinned_last() -> None:
    items = [
        _ni("Sites", "site", 10),
        _ni("Audit log", "system", 30),
    ]
    groups = _group_nav_by_section(items)
    assert [name for name, _ in groups] == ["site", "system"]


def test_other_sections_alphabetical_between_pins() -> None:
    items = [
        _ni("Audit log", "system", 30),
        _ni("Site settings", "site", 90),
        _ni("Posts", "content", 10),
        _ni("Something", "misc", 10),
    ]
    groups = _group_nav_by_section(items)
    assert [name for name, _ in groups] == ["content", "misc", "site", "system"]


def test_within_section_preserves_input_order() -> None:
    """Callers pre-sort by weight; the helper preserves that order."""
    items = [
        _ni("Posts", "content", 10),
        _ni("Pages", "content", 20),
        _ni("Attachments", "content", 20),
        _ni("Team", "content", 50),
    ]
    groups = _group_nav_by_section(items)
    assert len(groups) == 1
    section_name, in_order = groups[0]
    assert section_name == "content"
    assert [i.label for i in in_order] == ["Posts", "Pages", "Attachments", "Team"]
