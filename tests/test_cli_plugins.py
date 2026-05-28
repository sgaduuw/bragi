"""Tests for `bragi plugins list` (#190).

Exercises the plugin-platform introspection command: prints every
registered plugin, its origin (in-tree vs distribution), and the
count of hookspecs it participates in. The command reads the
live plugin manager attached to the admin app at boot.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker


def test_plugins_list_includes_in_tree_plugins(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """A handful of well-known in-tree plugins surface in the output."""
    from click.testing import CliRunner

    from bragi.cli import bragi

    result = CliRunner().invoke(bragi, ["plugins", "list"])
    assert result.exit_code == 0, result.output
    out = result.output
    # Header columns present.
    assert "plugin" in out
    assert "origin" in out
    assert "hooks" in out
    # Spot-check: every in-tree plugin we know ships at least one
    # hookimpl should appear with an `in-tree` origin label.
    for name in ("post", "page", "redirects", "auth_local", "internal_links"):
        assert name in out, f"missing plugin row for {name!r}: {out!r}"
    assert "in-tree" in out


def test_plugins_list_shows_positive_hook_counts(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """Every row has a numeric hook count that's non-negative (and at
    least one plugin in the bragi.contrib set has >0)."""
    from click.testing import CliRunner

    from bragi.cli import bragi

    result = CliRunner().invoke(bragi, ["plugins", "list"])
    assert result.exit_code == 0, result.output

    # Parse the body lines: skip header + separator.
    lines = [line for line in result.output.splitlines() if line.strip()]
    body = [line for line in lines if not line.startswith(("plugin", "---"))]
    assert body, f"no plugin rows in output: {result.output!r}"
    counts: list[int] = []
    for line in body:
        # Trailing token is the hook count.
        token = line.split()[-1]
        counts.append(int(token))
    assert all(c >= 0 for c in counts)
    assert any(c > 0 for c in counts)
