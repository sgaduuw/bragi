"""Plugin layout hygiene checks (#189).

Walks every in-tree `bragi.contrib.*` package and asserts that
templates ship under a namespaced directory rather than at the
top level of the package's `templates/`. Two plugins shipping
`templates/detail.html` would shadow each other unpredictably
(Flask's Jinja loader chain resolves by blueprint registration
order, which depends on pluggy discovery order plus the loop in
`bragi.apps.admin`). Theme-over-plugin shadowing is intentional
and documented in `bragi/core/themes.py`; plugin-over-plugin is
not.

Allowed top-level entries under a contrib package's
`templates/`:

- The plugin's own package basename (e.g. `bragi.contrib.post`
  may ship `templates/post/...`). Future third-party plugins
  picking their distribution name surface here.
- `admin/` — the shared admin-side namespace; templates under
  this prefix are served from the admin app and conventionally
  scoped via subfolders matching the plugin's concern
  (e.g. `admin/webmentions/list.html`).
- `delivery/` — the shared public-renderer namespace, same
  shape.

Anything else fails the test with the specific path + a hint to
move it under the plugin's slug or one of the shared prefixes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import bragi.contrib

_SHARED_PREFIXES = frozenset({"admin", "delivery"})


def _contrib_package_roots() -> list[Path]:
    """Every immediate subdirectory of `bragi.contrib` that has an
    `__init__.py` (i.e. is a real package, not a stray directory)."""
    contrib_root = Path(bragi.contrib.__file__).parent
    return sorted(p for p in contrib_root.iterdir() if p.is_dir() and (p / "__init__.py").exists())


@pytest.mark.parametrize(
    "package_root",
    _contrib_package_roots(),
    ids=lambda p: p.name,
)
def test_templates_are_namespaced(package_root: Path) -> None:
    """Each contrib plugin's `templates/` directory may carry only
    namespaced entries at the top level: the plugin's own slug, or
    one of the shared prefixes (`admin`, `delivery`)."""
    templates_dir = package_root / "templates"
    if not templates_dir.exists():
        return  # plugins without templates are fine
    allowed = _SHARED_PREFIXES | {package_root.name}
    offenders = [entry.name for entry in templates_dir.iterdir() if entry.name not in allowed]
    assert not offenders, (
        f"plugin {package_root.name!r} has un-namespaced template(s) at the top "
        f"level of `templates/`: {offenders!r}. Move them under "
        f"`templates/{package_root.name}/`, `templates/admin/`, or "
        f"`templates/delivery/` so two plugins shipping the same template "
        f"name can't shadow each other unpredictably."
    )
