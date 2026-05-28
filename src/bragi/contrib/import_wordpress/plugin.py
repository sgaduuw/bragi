"""import_wordpress plugin hook implementations."""

from __future__ import annotations

import click

from bragi.api import ImporterSpec, hookimpl
from bragi.contrib.import_wordpress.cli import wordpress_command
from bragi.contrib.import_wordpress.importer import apply, detect, plan


@hookimpl
def register_importer() -> ImporterSpec:
    return ImporterSpec(
        name="wordpress",
        description="WordPress eXtended RSS (WXR) export (HTML bodies converted to markdown)",
        detect=detect,
        plan=plan,
        apply=apply,
    )


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Hook `bragi import wordpress ...` onto the shared `import` group.

    The `import` group is created lazily by whichever importer
    plugin registers first; subsequent ones reuse it.
    """
    import_group: click.Group | None = group.commands.get("import")  # type: ignore[assignment]
    if import_group is None:
        import_group = click.Group("import", help="Content import commands.")
        group.add_command(import_group)
    import_group.add_command(wordpress_command)
