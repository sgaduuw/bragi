"""Tests for the UnsplashClient HTTP wrapper.

The client wraps `bragi.core.http.safe_get` which uses `requests`
under the hood. We mock at the safe_get seam (the same pattern
the auth_github tests use for the Authlib client) to avoid hitting
the real Unsplash API.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from bragi.contrib.unsplash import client as uc

SAMPLE_SEARCH_RESPONSE: dict[str, Any] = {
    "total": 2,
    "total_pages": 1,
    "results": [
        {
            "id": "abc123",
            "alt_description": "snowy mountain",
            "width": 4000,
            "height": 3000,
            "color": "#abcdef",
            "urls": {
                "full": "https://images.unsplash.com/full/abc123.jpg",
                "thumb": "https://images.unsplash.com/thumb/abc123.jpg",
            },
            "user": {
                "name": "Jane Doe",
                "username": "jane",
                "links": {"html": "https://unsplash.com/@jane"},
            },
            "links": {
                "download_location": "https://api.unsplash.com/photos/abc123/download",
            },
        },
        {
            "id": "def456",
            "alt_description": None,
            "width": 5000,
            "height": 3500,
            "color": "#123456",
            "urls": {
                "full": "https://images.unsplash.com/full/def456.jpg",
                "thumb": "https://images.unsplash.com/thumb/def456.jpg",
            },
            "user": {
                "name": "John Roe",
                "username": "john",
                "links": {"html": "https://unsplash.com/@john"},
            },
            "links": {
                "download_location": "https://api.unsplash.com/photos/def456/download",
            },
        },
    ],
}


def _fake_safe_get_factory(
    response_json: Any = None,
    response_bytes: bytes = b"",
    status_code: int = 200,
):
    """Return a callable that mimics safe_get's signature."""

    def _fake(url: str, *, headers=None, params=None, **kwargs) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = response_json
        resp.content = response_bytes
        resp.raise_for_status = MagicMock()
        resp._url = url
        resp._headers = headers
        resp._params = params
        return resp

    return _fake


def test_search_photos_parses_results_and_sends_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_safe_get_factory(response_json=SAMPLE_SEARCH_RESPONSE)
    monkeypatch.setattr(uc, "safe_get", fake)

    client = uc.UnsplashClient(access_key="test-key", app_name="testapp")
    results = client.search_photos(query="mountain", page=1)

    assert results.total == 2
    assert results.total_pages == 1
    assert len(results.results) == 2
    assert results.results[0].id == "abc123"
    assert results.results[0].user.name == "Jane Doe"
    assert results.results[0].links.download_location == (
        "https://api.unsplash.com/photos/abc123/download"
    )


def test_search_photos_sends_authorization_header_and_query_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _capturing_safe_get(url: str, *, headers=None, params=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        resp = MagicMock()
        resp.json.return_value = SAMPLE_SEARCH_RESPONSE
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(uc, "safe_get", _capturing_safe_get)

    client = uc.UnsplashClient(access_key="test-key", app_name="testapp")
    client.search_photos(query="mountain", page=2, per_page=12)

    assert captured["url"] == "https://api.unsplash.com/search/photos"
    assert captured["headers"]["Authorization"] == "Client-ID test-key"
    assert captured["params"]["query"] == "mountain"
    assert captured["params"]["page"] == 2
    assert captured["params"]["per_page"] == 12


def test_get_photo_full_bytes_fetches_urls_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_safe_get_factory(response_bytes=b"\x89PNG fake bytes")
    monkeypatch.setattr(uc, "safe_get", fake)

    client = uc.UnsplashClient(access_key="test-key", app_name="testapp")
    photo = uc.UnsplashPhoto.model_validate(SAMPLE_SEARCH_RESPONSE["results"][0])
    data = client.get_photo_full_bytes(photo)
    assert data == b"\x89PNG fake bytes"


def test_trigger_download_ping_swallows_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed download_location ping must NOT raise; it's
    fire-and-forget. Warning logged instead."""

    def _failing_safe_get(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(uc, "safe_get", _failing_safe_get)

    client = uc.UnsplashClient(access_key="test-key", app_name="testapp")
    photo = uc.UnsplashPhoto.model_validate(SAMPLE_SEARCH_RESPONSE["results"][0])
    with caplog.at_level(logging.WARNING, logger=uc.__name__):
        client.trigger_download_ping(photo)
    assert any("download_location ping" in r.message for r in caplog.records)


def test_trigger_download_ping_calls_the_correct_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _capturing_safe_get(url: str, *, headers=None, params=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(uc, "safe_get", _capturing_safe_get)

    client = uc.UnsplashClient(access_key="test-key", app_name="testapp")
    photo = uc.UnsplashPhoto.model_validate(SAMPLE_SEARCH_RESPONSE["results"][0])
    client.trigger_download_ping(photo)
    assert captured["url"] == "https://api.unsplash.com/photos/abc123/download"
    assert captured["headers"]["Authorization"] == "Client-ID test-key"
