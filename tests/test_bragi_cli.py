"""Top-level `bragi` CLI entry-point smoke tests.

The `bragi` click group is the standalone console script that
replaces `flask --app 'bragi.apps.admin:create_admin_app' cms <x>`.
These tests pin two contracts:

1. `bragi --help` lists every major subgroup that plugins
   register_cli_command -- a regression here means a plugin's
   wiring silently dropped.
2. A real subcommand (`bragi plugins list`) executes end-to-end
   through FlaskGroup -> _create_app_for_cli -> create_admin_app ->
   app.cli.invoke, exercising the with_appcontext decorator.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker


def test_bragi_cli_entry_point_exposes_all_subgroups(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """bragi --help lists every major subgroup so a plugin's
    register_cli_command being silently dropped from wiring shows
    up as a missing slug here. The subgroup list is the slug
    namespace plugins claim through `group.add_command(...)`;
    extend the list when a new in-tree plugin adds one."""
    from click.testing import CliRunner

    from bragi.cli import bragi

    result = CliRunner().invoke(bragi, ["--help"])
    assert result.exit_code == 0, result.output

    expected_subgroups = [
        "plugins",
        "session",
        "db",
        "user",
        "media",
        "webmentions",
        "activitypub",
        "embeds",
        "indexnow",
        "import",
        "internal-links",
    ]
    for slug in expected_subgroups:
        assert slug in result.output, f"missing subgroup {slug!r}: {result.output}"


def test_bragi_cli_invokes_plugins_list_end_to_end(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """`bragi plugins list` smoke-tests the FlaskGroup -> factory ->
    app-context chain. Read-only and safe to run in the test DB.
    A non-zero exit or an empty output here means the new entry
    point isn't actually building the admin app + plugin manager
    correctly."""
    from click.testing import CliRunner

    from bragi.cli import bragi

    result = CliRunner().invoke(bragi, ["plugins", "list"])
    assert result.exit_code == 0, result.output
    # Plugins list always emits at least a header + a row per
    # registered plugin. The exact format may vary; the smoke
    # check is "did we get something plugins-shaped back?"
    assert "plugin" in result.output.lower(), result.output
