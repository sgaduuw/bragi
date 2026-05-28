"""Local-credential authentication plugin.

Bootstrap auth path: argon2id-hashed password stored per User in
the `local_credentials` table. Most users will eventually
authenticate via OAuth (`bragi.contrib.auth_github`); this plugin
exists so the admin app is usable before OAuth is wired and as an
in-case-of-fire backup when the OAuth provider is unreachable.

Contributes:
- `register_admin_blueprint`: /auth/login and /auth/logout views.
- `register_auth_method`: AuthMethodSpec with `bootstrap=True`.
- `register_cli_command`: `bragi user create` for the bootstrap path.
- `on_app_init`: installs the auth guard on the admin app (anon
  hits get redirected to /auth/login).

Sessions use Flask's signed cookies for v1. Server-side
session storage (in the `sessions` table) is reserved for a
follow-up commit when logout-invalidates-everywhere matters.
"""
