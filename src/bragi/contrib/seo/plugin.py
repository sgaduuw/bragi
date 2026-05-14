"""SEO plugin hook implementations.

Four Blueprints contribute four distinct routes; pluggy collects
all hookimpls for the same hookspec, so we declare one impl per
sub-Blueprint rather than wrapping them into one combined
Blueprint. The `specname=` argument lets later functions bind to
`register_delivery_blueprint` under different Python names.
"""

from __future__ import annotations

from flask import Blueprint

from bragi.api import hookimpl
from bragi.contrib.seo.feed import bp as feed_bp
from bragi.contrib.seo.robots import bp as robots_bp
from bragi.contrib.seo.security_txt import bp as security_txt_bp
from bragi.contrib.seo.sitemap import bp as sitemap_bp


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    """Mount /sitemap.xml on the delivery app."""
    return sitemap_bp


@hookimpl(specname="register_delivery_blueprint")
def _register_robots_bp() -> Blueprint:
    """Mount /robots.txt on the delivery app."""
    return robots_bp


@hookimpl(specname="register_delivery_blueprint")
def _register_security_txt_bp() -> Blueprint:
    """Mount /.well-known/security.txt on the delivery app."""
    return security_txt_bp


@hookimpl(specname="register_delivery_blueprint")
def _register_feed_bp() -> Blueprint:
    """Mount /feed.xml on the delivery app."""
    return feed_bp
