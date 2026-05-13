"""Day-one hookspec surface test.

Asserts that the full hook surface is registered on the
PluginManager. Hookimpls themselves land with each contrib plugin;
this test only verifies the contract.
"""

from __future__ import annotations

from bragi.plugins import create_plugin_manager

DAY_ONE_HOOKS: frozenset[str] = frozenset(
    {
        # Lifecycle
        "on_app_init",
        "register_cli_command",
        "register_template_globals",
        # Content types
        "register_content_type",
        # Markdown / HTML rendering
        "register_markdown_extension",
        "register_markdown_transform",
        "register_html_transform",
        # Importers
        "register_importer",
        # Auth
        "register_oauth_provider",
        "register_auth_method",
        "on_user_login",
        # Redirects
        "resolve_redirect",
        # Admin UI
        "register_admin_blueprint",
        "register_admin_nav",
        # Content lifecycle
        "on_post_published",
        "on_post_updated",
        "on_post_deleted",
        # Analytics
        "record_analytics_event",
    }
)


def test_day_one_hookspecs_registered() -> None:
    """Every day-one hookspec is reachable on `pm.hook`."""
    pm = create_plugin_manager()
    missing: list[str] = []
    for name in sorted(DAY_ONE_HOOKS):
        if not hasattr(pm.hook, name):
            missing.append(name)
            continue
        if not callable(getattr(pm.hook, name)):
            missing.append(f"{name} (not callable)")
    assert not missing, f"missing or broken hookspecs: {missing}"


def test_resolve_redirect_is_firstresult() -> None:
    """`resolve_redirect` uses firstresult semantics; first non-None wins."""
    pm = create_plugin_manager()
    spec = pm.hook.resolve_redirect.spec
    assert spec is not None
    assert spec.opts.get("firstresult") is True
