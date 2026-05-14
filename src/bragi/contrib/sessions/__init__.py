"""Sessions admin plugin.

Operates on the `Session` rows produced by
`bragi.core.middleware.sessions`. Two surfaces:

- `/admin/account/sessions`: the logged-in user's own active
  sessions, with per-row revoke and a 'revoke everywhere except
  this' action. Lets a user kick a stolen cookie off their
  account without an operator's help.
- `/admin/sessions`: superuser-only view of all active sessions
  across the system, same revoke shape. Useful for incident
  response.

The `Session` SQLAlchemy model itself lives in
`bragi.core.models.session`; this plugin only contributes the
admin Blueprint and the two nav entries.
"""
