"""Tests for the embeds plugin (bragi.contrib.embeds).

Organisation mirrors the source layout:

- providers/ unit tests (id extraction, lookup priority, provider
  matches/render with `requests` patched)
- directive end-to-end through the renderer (app fixture exercises
  `register_markdown_extension` wiring)
- transforms.inject_youtube_cto_script (HTML transform)
- rerender + CLI (DB fixture exercises pending-card replacement)
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.embeds.providers import (
    BlueskyProvider,
    EmbedError,
    GenericOEmbedProvider,
    YouTubeProvider,
    lookup,
)
from bragi.contrib.embeds.providers.youtube import _extract_id
from bragi.contrib.embeds.rerender import is_pending, rerender_pending
from bragi.contrib.embeds.transforms import inject_youtube_cto_script
from bragi.core.models.page import Page
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.render.markdown import render_markdown

# ============================================================
# fixtures
# ============================================================


@pytest.fixture
def admin_app(
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    del patched_session_locals
    yield create_admin_app()


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    del patched_session_locals
    yield create_delivery_app()


@pytest.fixture
def seeded(db_session: Session) -> tuple[Site, User]:
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.commit()
    return site, user


class _StubResponse:
    """Minimal `requests.Response` substitute for monkeypatching.

    Lets tests pin status, body, and `.json()` payload without
    spinning a real HTTP server. `raise_for_status` is intentionally
    NOT implemented; providers check `status_code` directly.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_payload: dict[str, Any] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_payload
        self._raise = raise_exc

    def json(self) -> dict[str, Any]:
        if self._json is None:
            raise ValueError("no json payload")
        return self._json


def _patch_requests_get(
    monkeypatch: pytest.MonkeyPatch,
    *,
    module: str,
    response: _StubResponse | None = None,
    raise_exc: Exception | None = None,
) -> list[dict[str, Any]]:
    """Patch the SSRF-safe GET helper imported into `<module>`.

    Returns a list that captures each call's kwargs, so tests can
    assert the URL and headers the provider sent. Providers now
    call `bragi.core.http.safe_get`; patching at the import site
    means each provider module's binding is replaced independently.
    """
    calls: list[dict[str, Any]] = []

    def _fake_get(url: str, **kwargs: Any) -> _StubResponse:
        calls.append({"url": url, **kwargs})
        if raise_exc is not None:
            raise raise_exc
        assert response is not None
        return response

    monkeypatch.setattr(f"{module}.safe_get", _fake_get)
    return calls


# ============================================================
# YouTube id extraction
# ============================================================


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
    ],
)
def test_extract_youtube_id_across_shapes(url: str) -> None:
    assert _extract_id(url) == "dQw4w9WgXcQ"


def test_extract_youtube_id_rejects_non_id_paths() -> None:
    """A URL with no recognisable id shape returns None, not garbage."""
    assert _extract_id("https://www.youtube.com/feed/trending") is None
    assert _extract_id("https://www.youtube.com/") is None


def test_extract_youtube_id_rejects_malformed_id() -> None:
    """Defends against injection: id must match the 11-char alphabet."""
    assert _extract_id("https://youtu.be/not-an-id-too-long") is None
    assert _extract_id("https://youtu.be/short") is None


# ============================================================
# provider lookup priority
# ============================================================


def test_lookup_prefers_youtube_for_youtube_hosts() -> None:
    provider = lookup("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert isinstance(provider, YouTubeProvider)


def test_lookup_prefers_bluesky_for_bsky_hosts() -> None:
    provider = lookup("https://bsky.app/profile/foo/post/xyz")
    assert isinstance(provider, BlueskyProvider)


def test_lookup_uses_generic_oembed_for_allowlisted_host() -> None:
    provider = lookup("https://vimeo.com/12345")
    assert isinstance(provider, GenericOEmbedProvider)


def test_lookup_returns_none_for_unknown_host() -> None:
    assert lookup("https://example.com/whatever") is None


# ============================================================
# YouTube provider render
# ============================================================


def test_youtube_click_to_load_renders_thumbnail_and_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Title fetch succeeds; click-to-load aria-label uses it.
    _patch_requests_get(
        monkeypatch,
        module="bragi.contrib.embeds.providers.youtube",
        response=_StubResponse(json_payload={"title": "Never Gonna Give You Up"}),
    )
    monkeypatch.setattr("bragi.settings.settings.embed_youtube_mode", "click-to-load")

    html = YouTubeProvider().render("https://youtu.be/dQw4w9WgXcQ", timeout=1.0, user_agent="ua")
    assert "bragi-embed--youtube-cto" in html
    assert 'data-video-id="dQw4w9WgXcQ"' in html
    assert 'data-embed-action="load"' in html
    assert "i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg" in html
    assert "Never Gonna Give You Up" in html
    # Fallback link present in <noscript>.
    assert "<noscript>" in html


def test_youtube_click_to_load_survives_title_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing oEmbed title fetch must not raise; aria-label falls back."""
    _patch_requests_get(
        monkeypatch,
        module="bragi.contrib.embeds.providers.youtube",
        response=_StubResponse(status_code=404),
    )
    monkeypatch.setattr("bragi.settings.settings.embed_youtube_mode", "click-to-load")

    html = YouTubeProvider().render("https://youtu.be/dQw4w9WgXcQ", timeout=1.0, user_agent="ua")
    assert "bragi-embed--youtube-cto" in html
    assert 'aria-label="Play video"' in html


def test_youtube_iframe_mode_renders_iframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.embed_youtube_mode", "iframe")

    html = YouTubeProvider().render(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ", timeout=1.0, user_agent="ua"
    )
    assert "<iframe" in html
    assert "youtube-nocookie.com/embed/dQw4w9WgXcQ" in html
    assert "bragi-embed--youtube-cto" not in html


def test_youtube_raises_on_unrecognised_url() -> None:
    """A YT host with no extractable id is a structural failure;
    the directive will surface it as a pending card."""
    with pytest.raises(EmbedError):
        YouTubeProvider().render(
            "https://www.youtube.com/feed/trending", timeout=1.0, user_agent="ua"
        )


# ============================================================
# Bluesky provider
# ============================================================


def test_bluesky_render_inlines_returned_html(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_requests_get(
        monkeypatch,
        module="bragi.contrib.embeds.providers.bluesky",
        response=_StubResponse(
            json_payload={"html": '<blockquote class="bluesky-embed">hi</blockquote>'}
        ),
    )
    html = BlueskyProvider().render(
        "https://bsky.app/profile/x/post/y", timeout=1.0, user_agent="ua-test"
    )
    assert '<blockquote class="bluesky-embed">hi</blockquote>' in html
    assert "bragi-embed--bluesky" in html
    assert calls[0]["url"].startswith("https://embed.bsky.app/oembed")
    assert calls[0]["headers"]["User-Agent"] == "ua-test"


def test_bluesky_raises_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_requests_get(
        monkeypatch,
        module="bragi.contrib.embeds.providers.bluesky",
        response=_StubResponse(status_code=503),
    )
    with pytest.raises(EmbedError):
        BlueskyProvider().render("https://bsky.app/profile/x/post/y", timeout=1.0, user_agent="ua")


def test_bluesky_raises_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests as _requests

    _patch_requests_get(
        monkeypatch,
        module="bragi.contrib.embeds.providers.bluesky",
        raise_exc=_requests.ConnectionError("nope"),
    )
    with pytest.raises(EmbedError):
        BlueskyProvider().render("https://bsky.app/profile/x/post/y", timeout=1.0, user_agent="ua")


# ============================================================
# Generic oEmbed
# ============================================================


def test_generic_oembed_renders_allowlisted_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_requests_get(
        monkeypatch,
        module="bragi.contrib.embeds.providers.oembed",
        response=_StubResponse(
            json_payload={"html": '<iframe src="https://player.vimeo.com/video/1"></iframe>'}
        ),
    )
    html = GenericOEmbedProvider().render("https://vimeo.com/1", timeout=1.0, user_agent="ua")
    assert 'src="https://player.vimeo.com/video/1"' in html
    # Host-derived class so theme CSS can target per-provider.
    assert "bragi-embed--vimeo-com" in html


def test_generic_oembed_does_not_match_unknown_host() -> None:
    assert not GenericOEmbedProvider().matches("https://example.com/x")


# ============================================================
# Directive end-to-end via the renderer
# ============================================================


def test_directive_renders_youtube_to_inline_html(
    admin_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bragi.settings.settings.embed_youtube_mode", "iframe")
    src = "Hello\n\n" "::: embed https://www.youtube.com/watch?v=dQw4w9WgXcQ\n" ":::\n\n" "World\n"
    with admin_app.app_context():
        out = render_markdown(src)
    assert "<p>Hello</p>" in out
    assert "<p>World</p>" in out
    assert "youtube-nocookie.com/embed/dQw4w9WgXcQ" in out


def test_directive_without_closing_marker_does_not_eat_rest(
    admin_app: Flask,
) -> None:
    """If the directive opens but never closes, the rule must
    decline so the document falls back to paragraph parsing."""
    src = "::: embed https://youtu.be/dQw4w9WgXcQ\nnot the marker\nstill not\n"
    with admin_app.app_context():
        out = render_markdown(src)
    # No embed div emitted; the lines come through as a paragraph.
    assert "bragi-embed--youtube" not in out
    assert "youtu.be/dQw4w9WgXcQ" in out


def test_directive_unknown_host_emits_pending_card(admin_app: Flask) -> None:
    src = "::: embed https://example.com/some-thing\n:::\n"
    with admin_app.app_context():
        out = render_markdown(src)
    assert "bragi-embed--pending" in out
    assert 'data-embed-url="https://example.com/some-thing"' in out


def test_directive_provider_failure_emits_pending_card(
    admin_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_requests_get(
        monkeypatch,
        module="bragi.contrib.embeds.providers.bluesky",
        response=_StubResponse(status_code=500),
    )
    src = "::: embed https://bsky.app/profile/x/post/y\n:::\n"
    with admin_app.app_context():
        out = render_markdown(src)
    assert "bragi-embed--pending" in out
    assert 'data-embed-url="https://bsky.app/profile/x/post/y"' in out


# ============================================================
# HTML transform: click-to-load script injection
# ============================================================


def test_inject_youtube_cto_script_adds_script_when_marker_present() -> None:
    html = '<div class="bragi-embed bragi-embed--youtube-cto">x</div>'
    out = inject_youtube_cto_script(html)
    assert out.startswith(html)
    assert "<script>" in out[len(html) :]
    assert "youtube-nocookie.com/embed/" in out


def test_inject_youtube_cto_script_noop_without_marker() -> None:
    html = '<p>nothing to see here</p><div class="bragi-embed bragi-embed--bluesky"></div>'
    assert inject_youtube_cto_script(html) == html


def test_html_transform_runs_via_app_pipeline(
    admin_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin's register_html_transform must actually attach
    so a click-to-load embed in a body picks up the handler script.
    """
    _patch_requests_get(
        monkeypatch,
        module="bragi.contrib.embeds.providers.youtube",
        response=_StubResponse(json_payload={"title": "T"}),
    )
    monkeypatch.setattr("bragi.settings.settings.embed_youtube_mode", "click-to-load")

    src = "::: embed https://youtu.be/dQw4w9WgXcQ\n:::\n"
    with admin_app.app_context():
        out = render_markdown(src)
    assert "bragi-embed--youtube-cto" in out
    assert "<script>" in out


# ============================================================
# Rerender pending + CLI
# ============================================================


def _make_post_with_html(db: Session, site: Site, user: User, body_html: str) -> int:
    post = Post(
        site_id=site.id,
        slug="p",
        title="P",
        body_markdown="",
        body_html=body_html,
        body_excerpt="",
        author_id=user.id,
        status=PostStatus.PUBLISHED,
        published_at=datetime.now(UTC),
    )
    db.add(post)
    db.commit()
    return int(post.id)


def _make_page_with_html(db: Session, site: Site, user: User, body_html: str) -> int:
    page = Page(
        site_id=site.id,
        slug="pg",
        title="PG",
        body_markdown="",
        body_html=body_html,
        body_excerpt="",
        author_id=user.id,
        status="published",
    )
    db.add(page)
    db.commit()
    return int(page.id)


_PENDING_CARD = (
    '<div class="bragi-embed bragi-embed--pending" '
    'data-embed-url="https://bsky.app/profile/x/post/y">'
    '<a href="https://bsky.app/profile/x/post/y">link</a>'
    "</div>"
)


def test_rerender_replaces_pending_card_on_success(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    seeded: tuple[Site, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bsky provider that responds OK on retry must collapse the
    pending card into the resolved HTML."""
    del admin_app  # fixture only loaded for SessionLocal patch
    site, user = seeded
    with db_session_factory() as db:
        post_id = _make_post_with_html(db, site, user, body_html=f"<p>before</p>{_PENDING_CARD}")

    _patch_requests_get(
        monkeypatch,
        module="bragi.contrib.embeds.providers.bluesky",
        response=_StubResponse(
            json_payload={"html": '<blockquote class="bluesky-embed">ok</blockquote>'}
        ),
    )

    stats = rerender_pending()
    assert stats.cards_resolved == 1
    assert stats.cards_still_pending == 0
    assert stats.rows_updated == 1

    with db_session_factory() as db:
        post = db.get(Post, post_id)
    assert post is not None
    assert "bragi-embed--pending" not in post.body_html
    assert "bluesky-embed" in post.body_html


def test_rerender_leaves_card_when_provider_still_fails(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    seeded: tuple[Site, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del admin_app
    site, user = seeded
    with db_session_factory() as db:
        post_id = _make_post_with_html(db, site, user, body_html=_PENDING_CARD)

    _patch_requests_get(
        monkeypatch,
        module="bragi.contrib.embeds.providers.bluesky",
        response=_StubResponse(status_code=502),
    )

    stats = rerender_pending()
    assert stats.cards_resolved == 0
    assert stats.cards_still_pending == 1
    assert stats.rows_updated == 0

    with db_session_factory() as db:
        post = db.get(Post, post_id)
    assert post is not None
    assert is_pending(post.body_html)


def test_rerender_covers_pages_too(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    seeded: tuple[Site, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending cards on pages must rerender the same way as on
    posts; one regression test guards the two scan paths."""
    del admin_app
    site, user = seeded
    with db_session_factory() as db:
        page_id = _make_page_with_html(db, site, user, body_html=_PENDING_CARD)

    _patch_requests_get(
        monkeypatch,
        module="bragi.contrib.embeds.providers.bluesky",
        response=_StubResponse(
            json_payload={"html": '<blockquote class="bluesky-embed">ok</blockquote>'}
        ),
    )

    stats = rerender_pending()
    assert stats.cards_resolved == 1

    with db_session_factory() as db:
        page = db.get(Page, page_id)
    assert page is not None
    assert "bluesky-embed" in page.body_html


def test_rerender_dry_run_does_not_write(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    seeded: tuple[Site, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del admin_app
    site, user = seeded
    with db_session_factory() as db:
        post_id = _make_post_with_html(db, site, user, body_html=_PENDING_CARD)

    _patch_requests_get(
        monkeypatch,
        module="bragi.contrib.embeds.providers.bluesky",
        response=_StubResponse(
            json_payload={"html": '<blockquote class="bluesky-embed">ok</blockquote>'}
        ),
    )

    stats = rerender_pending(dry_run=True)
    assert stats.cards_resolved == 1
    assert stats.rows_updated == 0

    with db_session_factory() as db:
        post = db.get(Post, post_id)
    assert post is not None
    assert is_pending(post.body_html)


def test_cli_rerender_pending_reports_summary(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    seeded: tuple[Site, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site, user = seeded
    with db_session_factory() as db:
        _make_post_with_html(db, site, user, body_html=_PENDING_CARD)

    _patch_requests_get(
        monkeypatch,
        module="bragi.contrib.embeds.providers.bluesky",
        response=_StubResponse(
            json_payload={"html": '<blockquote class="bluesky-embed">ok</blockquote>'}
        ),
    )

    runner = admin_app.test_cli_runner()
    result = runner.invoke(args=["cms", "embeds", "rerender-pending"])
    assert result.exit_code == 0, result.output
    assert "resolved 1 card(s)" in result.output


def test_cli_rerender_pending_nothing_to_do(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    seeded: tuple[Site, User],
) -> None:
    del db_session_factory, seeded
    runner = admin_app.test_cli_runner()
    result = runner.invoke(args=["cms", "embeds", "rerender-pending"])
    assert result.exit_code == 0, result.output
    assert "nothing pending" in result.output


# ============================================================
# delivery app: register_markdown_extension wiring sanity
# ============================================================


def test_delivery_app_registers_embed_renderer(delivery_app: Flask) -> None:
    """The wiring should attach a `MarkdownIt` carrying the
    bragi_embed renderer rule on the delivery app too, not just
    admin."""
    md = delivery_app.extensions["markdown_renderer"]
    assert "bragi_embed" in md.renderer.rules
