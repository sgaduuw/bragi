"""Datasets plugin hook implementations.

Occupies the datasets surface reserved in CONTEXT.md "Deferred
surfaces" (#42). The `register_dataset_source` hookspec for
plugin-supplied remote sources stays reserved; v1 is upload-only.
"""

from __future__ import annotations

from collections.abc import Callable

import click
from flask import Blueprint
from markdown_it import MarkdownIt

from bragi.api import NavItem, hookimpl
from bragi.contrib.datasets.admin import bp as dataset_admin_bp
from bragi.contrib.datasets.cli import datasets_group
from bragi.contrib.datasets.delivery import bp as datasets_delivery_bp
from bragi.contrib.datasets.directive import configure_datasets
from bragi.contrib.datasets.transforms import inject_chart_loader
from bragi.core.render.transforms import TransformRegistry


@hookimpl
def register_markdown_extension() -> Callable[[MarkdownIt], None]:
    """Register the `::: dataset :::` block directive."""
    return configure_datasets


@hookimpl
def register_html_transform(registry: TransformRegistry) -> None:
    """Append the chart-loader script once per rendered body.

    Priority 310 puts it after the embeds click-to-load injector
    (300) at the late end of the pipeline, so no later transform
    can strip the chart markers it keys on.
    """
    registry.add(inject_chart_loader, name="dataset-chart-loader", priority=310)


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    """Serve the chart shim at /static/datasets/."""
    return datasets_delivery_bp


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the datasets admin Blueprint at /admin/sites/<slug>/datasets."""
    return dataset_admin_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add a Datasets entry to the admin nav (Write section)."""
    return [
        NavItem(
            label="Datasets",
            endpoint="dataset_admin.list_datasets",
            section="write",
            weight=40,
            scope="site",
        ),
    ]


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Add `bragi datasets rerender`."""
    group.add_command(datasets_group)


__all__ = [
    "register_admin_blueprint",
    "register_admin_nav",
    "register_cli_command",
    "register_delivery_blueprint",
    "register_html_transform",
    "register_markdown_extension",
]
