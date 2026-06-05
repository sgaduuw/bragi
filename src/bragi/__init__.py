"""bragi: a multisite CMS with htmx, plugins, and SEO baked in."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    # Distribution name is `bragi-cms` (the `bragi` name on PyPI is held
    # by an unrelated project). Import path and this package name stay
    # `bragi`; the distribution name change only affects pip install.
    __version__ = version("bragi-cms")
except PackageNotFoundError:  # pragma: no cover - not installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
