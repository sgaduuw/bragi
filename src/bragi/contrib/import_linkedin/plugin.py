"""import_linkedin plugin hook implementations."""

from __future__ import annotations

import click
from flask import Blueprint

from bragi.api import ImporterSpec, hookimpl
from bragi.contrib.import_linkedin.admin import bp as linkedin_admin_bp
from bragi.contrib.import_linkedin.cli import linkedin_command
from bragi.contrib.import_linkedin.importer import apply, detect, plan


@hookimpl
def register_importer() -> ImporterSpec:
    return ImporterSpec(
        name="linkedin",
        description="LinkedIn data export (ZIP)",
        detect=detect,
        plan=plan,
        apply=apply,
    )


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Add `linkedin` under the shared `import` subgroup."""
    import_group: click.Group | None = group.commands.get("import")  # type: ignore[assignment]
    if import_group is None:
        import_group = click.Group("import", help="Content import commands.")
        group.add_command(import_group)
    import_group.add_command(linkedin_command)


@hookimpl
def register_admin_blueprint() -> Blueprint:
    return linkedin_admin_bp


@hookimpl
def register_resume_admin_template() -> str:
    """Inject the upload widget into the resume page edit form."""
    return "admin/_linkedin_import_widget.html"
