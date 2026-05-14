"""Per-request theme-aware Jinja loader.

Themes shadow templates for sites that opted into them. The
delivery app wraps its existing loader chain with
`ThemeAwareLoader`, which on every template lookup checks
`g.site.theme`, finds the matching `ThemeSpec` in the registry,
and tries the theme's `template_loader` first. Misses fall
through to the fallback chain (plugin templates, then the
bragi/templates default), so a theme that overrides only one or
two templates still picks up everything else.

The admin app does NOT wrap its loader: only delivery is
themable in v1. The admin's job re: themes is exposing the
per-site dropdown, not theming itself.
"""

from __future__ import annotations

from typing import Any, cast

import jinja2
from flask import current_app, g


class ThemeAwareLoader(jinja2.BaseLoader):
    """A Jinja loader that defers to the active site's theme.

    Wraps a fallback loader (the existing plugin + default
    chain). On every `get_source`, if the request has a site with
    a registered theme, the theme's template loader is consulted
    first; a `TemplateNotFound` from the theme falls back to the
    chain. Outside a request context (e.g. CLI rendering, tests
    that don't push a request) the loader behaves as the fallback
    alone, so admin commands and unit tests are unaffected.
    """

    def __init__(self, fallback: jinja2.BaseLoader) -> None:
        self.fallback = fallback

    def _theme_loader(self) -> jinja2.BaseLoader | None:
        # `g` and `current_app` raise RuntimeError outside a request
        # / app context; that's a normal "no theme dispatch" state.
        try:
            site = g.get("site")
        except RuntimeError:
            return None
        if site is None or not getattr(site, "theme", None):
            return None
        try:
            registry = current_app.extensions.get("registry")
        except RuntimeError:
            return None
        if registry is None:
            return None
        spec = registry.theme(site.theme)
        if spec is None:
            # Site references a theme slug that isn't installed;
            # fall through to the default chain rather than 500.
            return None
        return cast(jinja2.BaseLoader, spec.template_loader)

    def get_source(
        self, environment: jinja2.Environment, template: str
    ) -> tuple[str, str | None, Any]:
        theme_loader = self._theme_loader()
        if theme_loader is not None:
            try:
                return theme_loader.get_source(environment, template)
            except jinja2.TemplateNotFound:
                pass
        return self.fallback.get_source(environment, template)

    def list_templates(self) -> list[str]:
        # The list is used by Jinja's autoescape / introspection
        # paths, not by rendering. Returning the fallback's list is
        # accurate enough: themes shadow names that already exist.
        return self.fallback.list_templates()
