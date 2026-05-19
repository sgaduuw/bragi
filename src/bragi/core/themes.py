"""Per-request theme-aware Jinja loader.

The delivery app wraps its existing loader chain with
`ThemeAwareLoader`, which on every template lookup resolves the
active site's theme slug (`g.site.theme`, or `"default"` when
NULL) through the registry and tries the theme's
`template_loader` first. Misses fall through to the fallback
chain (plugin templates, then `bragi/templates/`), so a theme
that overrides only one or two templates still picks up
everything else.

The in-tree `bragi.contrib.theme_default` package registers the
`"default"` slug and owns the canonical site shell. A site with
`theme=NULL` is treated as "use the default" rather than "no
theme dispatch"; disable `theme_default` via entry-points to
test the truly-unthemed path.

The admin app does NOT wrap its loader: only delivery is
themable in v1. The admin's job re: themes is exposing the
per-site dropdown, not theming itself.

## Cache invalidation when the active theme changes

Jinja's `Environment` keeps a process-wide compiled-template
cache keyed on template name. Once `delivery/base.html` is
compiled for site A (theme="alpha"), the next request from
site B (theme="beta") asks Jinja for the same template name
and Jinja returns the cached alpha compile without consulting
this loader. The compose-time `delivery_app` factory pairs
this loader with `app.jinja_env.auto_reload = True` so Jinja
honours the `uptodate` callable each `get_source` returns;
this loader's `uptodate` then forces a reload whenever the
active site's effective theme slug differs from the one that
produced the cached source. Without that pairing, switching
themes in the admin appears to do nothing.
"""

from __future__ import annotations

from collections.abc import Callable
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

    def _active_theme_slug(self) -> str | None:
        """Return the effective theme slug for the current request.

        Mirrors the resolution `_theme_loader` does so cache keys
        line up with whichever loader actually produced the
        source. Returns the registered slug (either the site's
        own pick or the `"default"` fallback) or None when no
        theme dispatch is in play (no site on the request, or no
        theme registered at all).
        """
        try:
            site = g.get("site")
        except RuntimeError:
            return None
        if site is None:
            return None
        try:
            registry = current_app.extensions.get("registry")
        except RuntimeError:
            return None
        if registry is None:
            return None
        slug = getattr(site, "theme", None)
        spec = registry.theme(slug) if slug else None
        if spec is None:
            spec = registry.theme("default")
        if spec is None:
            return None
        return cast(str, spec.slug)

    def _theme_loader(self) -> jinja2.BaseLoader | None:
        # `g` and `current_app` raise RuntimeError outside a request
        # / app context; that's a normal "no theme dispatch" state.
        try:
            site = g.get("site")
        except RuntimeError:
            return None
        if site is None:
            return None
        try:
            registry = current_app.extensions.get("registry")
        except RuntimeError:
            return None
        if registry is None:
            return None
        # Resolve to a theme in this order: the site's explicit slug,
        # then the implicit "default" slug. The fallback covers both
        # `Site.theme=NULL` (the common case: "use the default") and
        # `Site.theme="<uninstalled-slug>"` (the operator uninstalled
        # a theme without first un-picking it on every site). Only
        # when "default" itself isn't registered do we give up and
        # fall through to the chain (i.e. theme_default was disabled
        # via entry-points and no replacement was installed).
        slug = getattr(site, "theme", None)
        spec = registry.theme(slug) if slug else None
        if spec is None:
            spec = registry.theme("default")
        if spec is None:
            return None
        return cast(jinja2.BaseLoader, spec.template_loader)

    def _wrap_uptodate(
        self,
        loaded_slug: str | None,
        inner: Callable[[], bool] | None,
    ) -> Callable[[], bool]:
        """Compose Jinja `uptodate` callable with a theme-change check.

        Returns False (force reload) whenever the request's active
        theme differs from the one that loaded the cached source.
        Otherwise defers to whatever the underlying loader returned
        (e.g. PackageLoader checks the file mtime), or True if the
        underlying loader didn't supply one.
        """

        def uptodate() -> bool:
            if self._active_theme_slug() != loaded_slug:
                return False
            if inner is None:
                return True
            return inner()

        return uptodate

    def get_source(
        self, environment: jinja2.Environment, template: str
    ) -> tuple[str, str | None, Any]:
        active_slug = self._active_theme_slug()
        theme_loader = self._theme_loader()
        if theme_loader is not None:
            try:
                source, filename, inner = theme_loader.get_source(environment, template)
            except jinja2.TemplateNotFound:
                pass
            else:
                return source, filename, self._wrap_uptodate(active_slug, inner)
        source, filename, inner = self.fallback.get_source(environment, template)
        # The fallback path also needs a theme-aware uptodate:
        # the first request that exercises the fallback (no theme
        # registered, or template not in the theme) caches the
        # compile under the active slug. A later theme registration
        # or a per-site theme that DOES override the template must
        # invalidate that cache entry, not return the fallback
        # forever.
        return source, filename, self._wrap_uptodate(active_slug, inner)

    def list_templates(self) -> list[str]:
        # The list is used by Jinja's autoescape / introspection
        # paths, not by rendering. Returning the fallback's list is
        # accurate enough: themes shadow names that already exist.
        return self.fallback.list_templates()
