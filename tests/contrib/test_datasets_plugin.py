"""Plugin wiring: hookimpls, transform injection, static shim (#42)."""

from __future__ import annotations

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.contrib.datasets.transforms import inject_chart_loader


@pytest.fixture
def admin_app(patched_session_locals: sessionmaker[Session]) -> Flask:
    del patched_session_locals
    from bragi.apps.admin import create_admin_app

    return create_admin_app()


@pytest.fixture
def delivery_app(patched_session_locals: sessionmaker[Session]) -> Flask:
    del patched_session_locals
    from bragi.apps.delivery import create_delivery_app

    return create_delivery_app()


def test_directive_registered_on_app_renderer(admin_app: Flask) -> None:
    md = admin_app.extensions["markdown_renderer"]
    assert "bragi_dataset" in md.renderer.rules


def test_admin_blueprint_mounted(admin_app: Flask) -> None:
    assert "dataset_admin" in admin_app.blueprints


def test_delivery_static_shim_served(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/static/datasets/dataset-charts.js")
    assert resp.status_code == 200
    assert b"bragi-dataset-chart" in resp.data


def test_chart_loader_injected_when_chart_present() -> None:
    html = '<div class="bragi-dataset-chart" data-vega-spec="{}"></div>'
    out = inject_chart_loader(html)
    assert "dataset-charts.js" in out
    # Idempotent: a second pass adds nothing.
    assert out == inject_chart_loader(out)


def test_chart_loader_not_injected_without_charts() -> None:
    html = "<p>plain</p>"
    assert inject_chart_loader(html) == html


def test_cli_registered(admin_app: Flask) -> None:
    assert "datasets" in admin_app.cli.commands
