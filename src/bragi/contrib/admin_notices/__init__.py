"""bragi.contrib.admin_notices: shared chrome for plugin-contributed
admin notices.

Hosts:
- The dismiss/snooze HTTP routes (writes to ``admin_notice_dismissals``).
- The shared admin templates (``_notice_card.html``,
  ``_notice_rail.html``, ``_notice_dots.html``).
- The in-tree welcome-fallback hookimpl (dogfoods the
  ``admin_notices`` hookspec).
"""
