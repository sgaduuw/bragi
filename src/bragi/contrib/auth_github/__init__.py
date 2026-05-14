"""GitHub OAuth plugin for bragi.

Builds an Authlib OAuth2 client against GitHub's authorize / token
/ user endpoints, registers it as an OAuthProviderSpec, and ships
two routes:

- `/auth/github/login`  — initiates the OAuth dance.
- `/auth/github/callback` — consumes the code, fetches the
  profile, finds-or-creates the User+UserIdentity, sets the
  session.

GitHub does OAuth2 + a `/user` API call rather than full OIDC,
but Authlib abstracts the difference; a later swap to Authentik
or Keycloak (real OIDC providers) reuses the same shape.
"""
