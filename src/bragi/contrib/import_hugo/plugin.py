"""import_hugo plugin hook implementations."""

from __future__ import annotations

import click

from bragi.api import ImporterSpec, hookimpl
from bragi.contrib.import_hugo.cli import hugo_command
from bragi.contrib.import_hugo.importer import apply, detect, plan


@hookimpl
def register_importer() -> ImporterSpec:
    return ImporterSpec(
        name="hugo",
        description="Hugo (markdown + frontmatter)",
        detect=detect,
        plan=plan,
        apply=apply,
    )


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Wire `bragi import hugo ...` onto the top-level `bragi` group.

    The `import` subgroup is created on first registration; later
    importers add their own command under it without a coordination
    point. Click groups deduplicate, but for clarity we look up the
    existing one rather than blindly re-creating it.
    """
    import_group: click.Group | None = group.commands.get("import")  # type: ignore[assignment]
    if import_group is None:
        import_group = click.Group("import", help="Content import commands.")
        group.add_command(import_group)
    import_group.add_command(hugo_command)
