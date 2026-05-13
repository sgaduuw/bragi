"""bragi: a multisite CMS with htmx, plugins, and SEO baked in."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("bragi")
except PackageNotFoundError:  # pragma: no cover - not installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
