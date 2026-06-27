"""Unit tests for `profile_links._store.read_profile_links`.

Pure logic: no Flask app, no DB session. Exercises the defensive
read contract (valid → parsed; absent / None / malformed → []).
"""

from __future__ import annotations

import logging

from bragi.contrib.profile_links._store import read_profile_links
from bragi.core.models.site import Site


def _site(extra_settings: object) -> Site:
    """A transient Site carrying only the extra_settings under test."""
    return Site(extra_settings=extra_settings)


def test_valid_list_round_trips() -> None:
    links = read_profile_links(
        _site(
            {
                "profile_links": [
                    {"label": "GitHub", "url": "https://github.com/you"},
                    {"label": "Mastodon", "url": "https://hachyderm.io/@you"},
                ]
            }
        )
    )
    assert [link.label for link in links] == ["GitHub", "Mastodon"]
    assert str(links[0].url) == "https://github.com/you"


def test_absent_key_returns_empty() -> None:
    assert read_profile_links(_site({"posts_per_page": 10})) == []


def test_empty_extra_settings_returns_empty() -> None:
    assert read_profile_links(_site({})) == []
    assert read_profile_links(_site(None)) == []


def test_none_site_returns_empty() -> None:
    assert read_profile_links(None) == []


def test_malformed_entry_returns_empty_and_warns(caplog) -> None:
    # Missing the required `url` field on an entry.
    with caplog.at_level(logging.WARNING):
        result = read_profile_links(_site({"profile_links": [{"label": "GitHub"}]}))
    assert result == []
    assert any("malformed profile_links" in r.message for r in caplog.records)


def test_non_list_value_returns_empty() -> None:
    assert read_profile_links(_site({"profile_links": "not-a-list"})) == []
