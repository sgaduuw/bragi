"""Public plugin API for bragi.

Plugin authors import `hookimpl` and the spec dataclasses from
this module. It is the public contract; `bragi.hookspecs` is
internal implementation detail and may be reshuffled without
notice.

Spec dataclasses (ContentTypeSpec, ImporterSpec, OAuthProviderSpec,
AuthMethodSpec, NavItem, FieldSpec, RedirectTarget, AnalyticsEvent)
land here as the corresponding hookspecs are introduced.
"""

from __future__ import annotations

import pluggy

hookimpl = pluggy.HookimplMarker("bragi")

__all__ = ["hookimpl"]
