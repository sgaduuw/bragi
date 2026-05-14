"""Auth service, password hashing, permissions.

OAuth flows are owned by the auth_github plugin (and any other
auth_* plugins). This module provides the User <-> UserIdentity
linking, server-side session creation, and the argon2id wrapper
used by the local-bootstrap plugin.
"""
