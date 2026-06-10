"""import_ghost plugin hook implementations."""

from __future__ import annotations

import click
from flask import Blueprint

from bragi.api import ImporterAdminTile, ImporterSpec, hookimpl
from bragi.contrib.import_ghost.admin import bp as ghost_admin_bp
from bragi.contrib.import_ghost.cli import ghost_command
from bragi.contrib.import_ghost.importer import apply, detect, plan

__all__ = [
    "register_admin_blueprint",
    "register_cli_command",
    "register_importer",
    "register_importer_admin_tile",
]


@hookimpl
def register_importer() -> ImporterSpec:
    return ImporterSpec(
        name="ghost",
        description="Ghost JSON export (HTML bodies converted to markdown)",
        detect=detect,
        plan=plan,
        apply=apply,
    )


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Hook `bragi import ghost ...` onto the shared `import` group.

    The `import` group is created lazily by whichever importer
    plugin registers first; subsequent ones reuse it.
    """
    import_group: click.Group | None = group.commands.get("import")  # type: ignore[assignment]
    if import_group is None:
        import_group = click.Group("import", help="Content import commands.")
        group.add_command(import_group)
    import_group.add_command(ghost_command)


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the Ghost upload/review Blueprint on the admin app."""
    return ghost_admin_bp


@hookimpl
def register_importer_admin_tile() -> ImporterAdminTile:
    """Contribute the Ghost importer tile to the admin import index."""
    return ImporterAdminTile(
        label="Ghost",
        slug="ghost",
        description=(
            "Import posts, pages, redirects, and featured images from a Ghost JSON or ZIP export."
        ),
        start_endpoint="ghost_admin.upload",
    )
