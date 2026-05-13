"""Top-level CLI group registered on the Flask admin app.

Plugins extend this group via the `register_cli_command` hook
(landing in a later commit). Invoke with
`flask --app bragi.apps.admin cms ...`.
"""

from __future__ import annotations

import click


@click.group()
def cms() -> None:
    """bragi management commands."""
