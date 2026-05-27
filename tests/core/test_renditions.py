"""Tests for `bragi.core.renditions.editor_renditions_for_body`.

The helper scans a post / page body's markdown for `/attachments/<sha>`
image refs, looks up done WebP renditions for each, and returns a
`{sha: {small, medium, full}}` map keyed by attachment storage_key.
Used by the post / page edit forms (via the attachments plugin's
`editor_image_renditions` Jinja global) to hydrate the TipTap
editor's in-editor previews on reload.

The other two helpers in this module (`social_card_storage_key`,
`smallest_webp_storage_key`) are exercised by their callers'
integration tests; this file is the focused unit-test home for the
editor-side helper, where the regex, multi-image batching, ladder
bucketing, and site scoping all live in one Python function.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.renditions import editor_renditions_for_body

# Two 64-char hex shas, deterministic and visually distinct.
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_OTHER_SITE = "c" * 64


def _make_site(db: Session, *, slug: str, owner: User | None = None) -> Site:
    if owner is None:
        owner = User(email=f"{slug}@example.test", display_name=slug, is_active=True)
        db.add(owner)
        db.flush()
    site = Site(
        slug=slug,
        hostname=f"{slug}.example.test",
        title=slug,
        canonical_url=f"https://{slug}.example.test",
        owner_user_id=owner.id,
    )
    db.add(site)
    db.flush()
    return site


def _make_attachment(db: Session, *, site_id: int, storage_key: str) -> Attachment:
    att = Attachment(
        site_id=site_id,
        filename=f"{storage_key[:6]}.jpg",
        content_type="image/jpeg",
        size_bytes=100,
        storage_key=storage_key,
        width=2000,
        height=1000,
    )
    db.add(att)
    db.flush()
    return att


def _add_webp_rendition(
    db: Session, *, attachment_id: int, sha: str, width: int, status: str = "done"
) -> None:
    db.add(
        AttachmentRendition(
            attachment_id=attachment_id,
            size_label=f"{width}w",
            format="webp",
            content_type="image/webp",
            status=status,
            storage_key=f"{sha}/{width}/webp",
            width=width,
            height=int(width / 2),
            bytes_size=10,
        )
    )


# ============================================================
# Trivial inputs
# ============================================================


def test_empty_body_returns_empty_dict(db_session: Session) -> None:
    site = _make_site(db_session, slug="blog")
    assert editor_renditions_for_body(db_session, site_id=site.id, body_markdown="") == {}


def test_body_without_image_refs_returns_empty(db_session: Session) -> None:
    site = _make_site(db_session, slug="blog")
    body = "# Heading\n\nJust prose, no image links.\n\n[a link](https://example.test)"
    assert editor_renditions_for_body(db_session, site_id=site.id, body_markdown=body) == {}


def test_body_with_image_ref_but_no_attachment_row(db_session: Session) -> None:
    """A sha that looks valid but has no Attachment row drops out
    (e.g. orphaned reference after an attachment was deleted)."""
    site = _make_site(db_session, slug="blog")
    body = f"![alt](/attachments/{SHA_A})"
    assert editor_renditions_for_body(db_session, site_id=site.id, body_markdown=body) == {}


def test_body_with_attachment_but_no_done_renditions(db_session: Session) -> None:
    """An attachment with no done WebP renditions (e.g. just
    uploaded, worker hasn't drained) drops out — the editor
    falls back to the original src for the preview."""
    site = _make_site(db_session, slug="blog")
    _make_attachment(db_session, site_id=site.id, storage_key=SHA_A)
    body = f"![alt](/attachments/{SHA_A})"
    assert editor_renditions_for_body(db_session, site_id=site.id, body_markdown=body) == {}


# ============================================================
# Happy path: ladder bucketing
# ============================================================


def test_three_tier_ladder_buckets_small_and_medium(db_session: Session) -> None:
    """For the default 3-tier ladder [320, 800, 1600], the helper
    picks 320w as small, 800w as medium (middle), and leaves full
    unset (the editor falls back to the original src for size-full
    so the largest tier is always the source — the largest WebP
    may be smaller when the no-upscale guard skipped the top tier)."""
    site = _make_site(db_session, slug="blog")
    att = _make_attachment(db_session, site_id=site.id, storage_key=SHA_A)
    for width in (320, 800, 1600):
        _add_webp_rendition(db_session, attachment_id=att.id, sha=SHA_A, width=width)
    db_session.flush()

    body = f"![alt](/attachments/{SHA_A}){{.size-medium .align-center}}"
    out = editor_renditions_for_body(db_session, site_id=site.id, body_markdown=body)

    assert out == {
        SHA_A: {
            "small": f"{SHA_A}/320/webp",
            "medium": f"{SHA_A}/800/webp",
            "full": None,
        }
    }


def test_two_tier_ladder_picks_larger_as_medium(db_session: Session) -> None:
    """For a 2-tier ladder [320, 800], `len(ladder) // 2 == 1`, so
    medium = the 800 entry. Documents the bucketing math for the
    smallest non-trivial ladder length (asymmetric round-up to
    larger tier)."""
    site = _make_site(db_session, slug="blog")
    att = _make_attachment(db_session, site_id=site.id, storage_key=SHA_A)
    for width in (320, 800):
        _add_webp_rendition(db_session, attachment_id=att.id, sha=SHA_A, width=width)
    db_session.flush()

    body = f"![alt](/attachments/{SHA_A})"
    out = editor_renditions_for_body(db_session, site_id=site.id, body_markdown=body)

    assert out[SHA_A]["small"] == f"{SHA_A}/320/webp"
    assert out[SHA_A]["medium"] == f"{SHA_A}/800/webp"


def test_only_smallest_rendition_done(db_session: Session) -> None:
    """A single 320w rendition means small == medium (the helper
    biases medium toward the larger of the available tiers; with
    one entry there's only one choice)."""
    site = _make_site(db_session, slug="blog")
    att = _make_attachment(db_session, site_id=site.id, storage_key=SHA_A)
    _add_webp_rendition(db_session, attachment_id=att.id, sha=SHA_A, width=320)
    db_session.flush()

    out = editor_renditions_for_body(
        db_session, site_id=site.id, body_markdown=f"![alt](/attachments/{SHA_A})"
    )
    assert out[SHA_A]["small"] == f"{SHA_A}/320/webp"
    assert out[SHA_A]["medium"] == f"{SHA_A}/320/webp"


# ============================================================
# Filtering and edge cases
# ============================================================


def test_failed_or_pending_renditions_are_skipped(db_session: Session) -> None:
    """Only `status='done'` rows count. Pending / processing /
    failed rows are dropped from the ladder (matching the picker)."""
    site = _make_site(db_session, slug="blog")
    att = _make_attachment(db_session, site_id=site.id, storage_key=SHA_A)
    _add_webp_rendition(db_session, attachment_id=att.id, sha=SHA_A, width=320, status="done")
    _add_webp_rendition(db_session, attachment_id=att.id, sha=SHA_A, width=800, status="failed")
    _add_webp_rendition(db_session, attachment_id=att.id, sha=SHA_A, width=1600, status="pending")
    db_session.flush()

    out = editor_renditions_for_body(
        db_session, site_id=site.id, body_markdown=f"![alt](/attachments/{SHA_A})"
    )
    # Only 320 is done, so it occupies both small and medium slots.
    assert out[SHA_A]["small"] == f"{SHA_A}/320/webp"
    assert out[SHA_A]["medium"] == f"{SHA_A}/320/webp"


def test_cross_site_sha_is_filtered_out(db_session: Session) -> None:
    """A sha in body_markdown that belongs to a different site is
    dropped — matches the picker's tenant discipline so a stale
    cross-site reference can't leak rendition URLs."""
    blog = _make_site(db_session, slug="blog")
    other = _make_site(db_session, slug="other")
    # SHA_OTHER_SITE is owned by `other`, not `blog`.
    other_att = _make_attachment(db_session, site_id=other.id, storage_key=SHA_OTHER_SITE)
    _add_webp_rendition(db_session, attachment_id=other_att.id, sha=SHA_OTHER_SITE, width=320)
    db_session.flush()

    body = f"![alt](/attachments/{SHA_OTHER_SITE})"
    # Looking up under `blog` should return nothing.
    assert editor_renditions_for_body(db_session, site_id=blog.id, body_markdown=body) == {}


def test_multiple_images_in_one_body(db_session: Session) -> None:
    """Two images in one body each get their own ladder; a single
    DB round-trip handles the batch (the implementation uses
    `IN (...)` on attachment_ids)."""
    site = _make_site(db_session, slug="blog")
    att_a = _make_attachment(db_session, site_id=site.id, storage_key=SHA_A)
    att_b = _make_attachment(db_session, site_id=site.id, storage_key=SHA_B)
    for width in (320, 800):
        _add_webp_rendition(db_session, attachment_id=att_a.id, sha=SHA_A, width=width)
    _add_webp_rendition(db_session, attachment_id=att_b.id, sha=SHA_B, width=320)
    db_session.flush()

    body = (
        f"# Two images\n\n"
        f"![first](/attachments/{SHA_A}){{.size-small}}\n\n"
        f"some prose\n\n"
        f"![second](/attachments/{SHA_B}){{.size-medium}}"
    )
    out = editor_renditions_for_body(db_session, site_id=site.id, body_markdown=body)
    assert set(out.keys()) == {SHA_A, SHA_B}
    assert out[SHA_A]["small"] == f"{SHA_A}/320/webp"
    assert out[SHA_B]["small"] == f"{SHA_B}/320/webp"


def test_same_image_referenced_twice_deduplicates(db_session: Session) -> None:
    """If the same sha appears twice in body_markdown (rare but
    possible), it's looked up once and the resulting map has one
    entry. Demonstrates the `set(findall(...))` dedup."""
    site = _make_site(db_session, slug="blog")
    att = _make_attachment(db_session, site_id=site.id, storage_key=SHA_A)
    _add_webp_rendition(db_session, attachment_id=att.id, sha=SHA_A, width=320)
    db_session.flush()

    body = f"![one](/attachments/{SHA_A})\n\n![two](/attachments/{SHA_A})"
    out = editor_renditions_for_body(db_session, site_id=site.id, body_markdown=body)
    assert list(out.keys()) == [SHA_A]
