"""Scaffold smoke test.

Confirms the package imports and exposes a version string. Replaced
by real tests as plugins ship.
"""

from __future__ import annotations

from bragi import __version__


def test_version_exposed() -> None:
    assert isinstance(__version__, str)
    assert __version__
