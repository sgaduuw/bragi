"""Thin wrapper around bragi.core.http.safe_get for the Unsplash API.

Covers exactly the subset the picker needs: search, fetch photo
bytes, and the `download_location` ping the API guidelines require
whenever a photo is "used".
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from bragi.core.http import safe_get

LOG = logging.getLogger(__name__)

API_BASE = "https://api.unsplash.com"


class UnsplashUrls(BaseModel):
    full: str
    thumb: str


class UnsplashUserLinks(BaseModel):
    html: str


class UnsplashUser(BaseModel):
    name: str
    username: str
    links: UnsplashUserLinks


class UnsplashPhotoLinks(BaseModel):
    download_location: str


class UnsplashPhoto(BaseModel):
    """Subset of the Unsplash photo schema the plugin uses."""

    id: str
    alt_description: str | None = None
    width: int
    height: int
    color: str | None = None
    urls: UnsplashUrls
    user: UnsplashUser
    links: UnsplashPhotoLinks

    model_config = {"extra": "ignore"}


class SearchResults(BaseModel):
    total: int
    total_pages: int
    results: list[UnsplashPhoto]


class UnsplashClient:
    def __init__(self, *, access_key: str, app_name: str) -> None:
        self.access_key = access_key
        self.app_name = app_name

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Client-ID {self.access_key}"}

    def search_photos(self, *, query: str, page: int = 1, per_page: int = 24) -> SearchResults:
        """GET /search/photos. Returns parsed SearchResults."""
        resp = safe_get(
            f"{API_BASE}/search/photos",
            headers=self._auth_headers(),
            params={"query": query, "page": page, "per_page": per_page},
        )
        resp.raise_for_status()
        return SearchResults.model_validate(resp.json())

    def get_photo_full_bytes(self, photo: UnsplashPhoto) -> bytes:
        """GET the URL in photo.urls.full. Returns the binary.

        Bypasses safe_get's default 1 MB cap because Unsplash
        full-resolution JPEGs commonly run 2-15 MB. 20 MB is the
        upper bound for the largest typical Unsplash original;
        bumping further hurts nothing on a single-author CMS
        but the cap protects against pathological inputs."""
        resp = safe_get(
            photo.urls.full,
            headers=self._auth_headers(),
            max_bytes=20_000_000,
        )
        resp.raise_for_status()
        return resp.content

    def trigger_download_ping(self, photo: UnsplashPhoto) -> None:
        """Fire the API-required download tracker. Errors logged but
        not raised; the user-facing flow has already committed by the
        time this runs."""
        try:
            resp = safe_get(photo.links.download_location, headers=self._auth_headers())
            resp.raise_for_status()
        except Exception as exc:
            LOG.warning(
                "unsplash download_location ping failed for photo %s: %s",
                photo.id,
                exc,
            )
