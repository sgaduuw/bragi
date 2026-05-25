---
name: bragi-add-plugin-hook
description: Use when adding a new hookspec to bragi.api or bragi.hookspecs, or changing the signature of an existing one. Drives the required contrib test, two-step deprecation policy, and stability-contract docstring update. The hook surface is bragi's most load-bearing abstraction — regressions silently break every dependent plugin.
---

# Adding or changing a bragi plugin hook

The hook surface is bragi's most load-bearing abstraction (per bragi/CLAUDE.md "Verifying changes"). Regressions silently break every dependent plugin, including the in-tree ones in `bragi/contrib/`. Treat hook changes with the same rigor as schema changes.

## Where things live

- `src/bragi/api.py` — **PUBLIC contract**. Plugin authors import only from here. The module docstring (top of file, "Stability boundary") documents what's covered and the two-step deprecation policy.
- `src/bragi/hookspecs.py` — **INTERNAL**. Plugins do NOT import from this module. May be reshuffled between patch versions.
- `src/bragi/contrib/X/` — reference implementations of every hook the core needs from a plugin (per the "built-ins are plugins by registration" rule).
- `tests/contrib/test_<plugin>.py` — one file per built-in plugin, exercising its hookimpls with a minimal app fixture.

## When adding a NEW hook

1. **Add the hookspec** to `src/bragi/hookspecs.py` with a full docstring: what it does, when it's called in the request/lifecycle, the return contract (including what `None` means).

2. **If the spec dataclass is part of the plugin's public contract**, re-export it from `src/bragi/api.py`. Then add it to the "What's covered" list in the top-of-file docstring under "Stability boundary".

3. **Add a contrib test** in `tests/contrib/test_<plugin>.py` (or create one if this is the first hookimpl for the plugin). Exercise the new surface via a minimal app fixture registering only the plugin under test plus its hard dependencies. The test is what catches "the hookspec changed but I forgot to update the in-tree plugin."

4. **Update or add the in-tree hookimpl** in `bragi/contrib/X/` if a built-in needs the new hook. Built-ins are plugins by registration; they ARE the reference implementation.

5. **Add an `## [Unreleased]` line to `CHANGELOG.md`** describing the new hook in operator-facing terms ("Plugins can now register custom search backends via `register_search_backend`").

6. **Document the hook in the `bragi.api` docstring** if there's a usage pattern that isn't self-evident from the signature.

## When CHANGING an existing hook (signature, return shape, behaviour)

The deprecation policy from `bragi/api.py`'s top docstring is the rule:

> Best-effort: hook signatures and spec fields will not be removed within a minor version. Additions are always safe (new optional fields, new hookspecs, new spec types). When a removal becomes necessary it lands across two minor versions:
>
> 1. The field/hook is marked deprecated in the docstring with the target removal version, and a runtime warning logged on use if the deprecation surface is reachable.
> 2. Removal in the named release, with the CHANGELOG entry pointing back at the deprecation notice.

In practice:

- **Adding** a new optional field, a new hookspec, or a new spec type: SAFE. No deprecation needed. Add a CHANGELOG line.
- **Renaming** a field or hook: deprecate the old name across one minor version, accept both, remove in the next minor. Update CHANGELOG at both steps.
- **Changing the return shape** (e.g. tuple → dataclass): same two-step. Translate old returns to the new shape with a deprecation warning during the bridge release.
- **Removing** a field, hook, or spec: only after a deprecation cycle. The CHANGELOG entry MUST point back at the deprecation notice in the prior release.

For every change, also:

- Update the stability-boundary docstring in `api.py` if the change touches what's covered.
- Update the contrib test for the hook (the old test now tests the new shape).
- Update every in-tree contrib plugin that implements the hook.

## Verification before commit

```sh
# Contrib test for the affected plugin
poetry run pytest tests/contrib/test_<plugin>.py

# Type-check across the tree; catches accidental breakage in other in-tree plugins
poetry run mypy src/

# Full plugin discovery still works
poetry run flask --app 'bragi.apps.admin:create_admin_app' cms plugins list
```

The `cms plugins list` output should show the changed plugin with its hookimpl count, confirming the new/changed hook is registered correctly.

## Reminders

- **`bragi.hookspecs` is internal.** If plugin authors need a type to implement the hook, re-export from `bragi.api`. Never tell a plugin author to "import from `bragi.hookspecs`."
- **The contrib boundary still applies.** A test that exercises hook X may register multiple plugins, but a contrib plugin's source code may NOT import from a sibling `bragi.contrib.*`. The `plugin-boundary-auditor` agent catches this.
- **No automated deprecation enforcement.** The rule is discipline-on-author. The `cms plugins list` introspection surface helps operators grep what's installed before bumping bragi.
