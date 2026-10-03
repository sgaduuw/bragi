"""bragi.contrib.api_tokens (#146): bearer-token auth for the admin app.

Public-facing surface:

- `Authorization: Bearer brg_<public_id>_<secret>` authenticates
  the registered JSON API as the token's owner. Only those requests
  bypass CSRF. HTML admin routes reject bearer headers and require
  the normal browser session and CSRF token.
- `/admin/account/tokens/` lists, creates, and revokes the
  current user's tokens. Plaintext token is shown exactly once,
  on create.
- `/admin/api/sites/<slug>/posts/...` is the JSON REST surface
  for programmatic post management. Scope-gated by `post:write`.

Lives as a plugin because the surface is opt-in: an operator who
prefers session-only access can disable the plugin in
`pyproject.toml` without touching core.
"""
