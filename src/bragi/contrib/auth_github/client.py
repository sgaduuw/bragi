"""Authlib OAuth2 client factory + profile fetcher for GitHub.

Kept in its own module so the plugin entrypoint (`plugin.py`)
stays small and the test suite can monkey-patch the HTTP layer
without touching the Blueprint code.
"""

from __future__ import annotations

from typing import Any

from authlib.integrations.flask_client import OAuth

from bragi.api import ExternalUser
from bragi.settings import settings

# Module-level OAuth instance: Authlib's Flask integration is happy
# to be a singleton, and we never need to re-register the client
# (the client_id / secret are pulled from `Settings` at
# `build_github_client` time).
_oauth = OAuth()
_REGISTERED = False


def build_github_client() -> Any:
    """Return the Authlib OAuth client for GitHub.

    Registers the client on first call; subsequent calls reuse the
    same registration. Caller is responsible for ensuring
    `settings.github_client_id` / `_secret` are set; we don't
    raise here because the registration itself works fine with
    None, the failure surfaces at the actual /authorize call.
    """
    global _REGISTERED
    if not _REGISTERED:
        _oauth.register(
            name="github",
            client_id=settings.github_client_id,
            client_secret=settings.github_client_secret,
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user user:email"},
        )
        _REGISTERED = True
    return _oauth.github


def fetch_user_info(token: dict[str, Any]) -> ExternalUser:
    """Use the access token to fetch /user (and /user/emails if the
    public email is None) and return an `ExternalUser`."""
    client = build_github_client()
    profile_resp = client.get("user", token=token)
    profile = profile_resp.json()
    email: str | None = profile.get("email")
    if email is None:
        # GitHub returns null when the user keeps email private,
        # but the user:email scope unlocks /user/emails which
        # always lists at least the primary one.
        emails_resp = client.get("user/emails", token=token)
        emails: list[dict[str, Any]] = emails_resp.json()
        for entry in emails:
            if entry.get("primary") and entry.get("verified"):
                email = entry.get("email")
                break
        if email is None:
            for entry in emails:
                if entry.get("verified"):
                    email = entry.get("email")
                    break
    return ExternalUser(
        provider="github",
        provider_user_id=str(profile["id"]),
        provider_username=profile.get("login") or "",
        email=email,
        avatar_url=profile.get("avatar_url"),
        raw=profile,
    )
