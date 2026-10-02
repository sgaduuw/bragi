import pytest

from bragi.core.media_usage import attachment_usage, media_usage
from bragi.core.models.attachment import Attachment
from bragi.core.models.post import Post
from bragi.core.models.post_revision import PostRevision
from tests.conftest import make_test_site


def test_usage_tracks_live_historical_and_missing_local_media(db_session):
    site = make_test_site(db_session, slug="blog", hostname="blog.test", title="Blog")
    key = "a" * 64
    missing = "b" * 64
    attachment = Attachment(
        site_id=site.id, filename="a.png", content_type="image/png", size_bytes=1, storage_key=key
    )
    db_session.add(attachment)
    db_session.flush()
    post = Post(
        site_id=site.id,
        author_id=site.owner_user_id,
        slug="post",
        title="Post",
        body_markdown=f"![a](/attachments/{key}/320/webp) ![b](https://blog.test/attachments/{missing})"
        f" ![external](https://elsewhere.test/attachments/{key})",
        featured_image_id=attachment.id,
    )
    db_session.add(post)
    db_session.flush()
    revision = PostRevision(
        post_id=post.id, slug="old", title="Old", status="published", featured_image_id=999
    )
    db_session.add(revision)
    db_session.flush()
    refs = media_usage(db_session, site)
    assert {(r.storage_key, r.attachment_id) for r in refs} == {
        (key + "/320/webp", None),
        (missing, None),
        (key, attachment.id),
        (None, 999),
    }
    assert len(attachment_usage(db_session, site, [attachment])[attachment.id]) == 2
    historical = next(r for r in refs if r.source_type == "post_revision")
    assert historical.content_id == post.id
    assert historical.status == "revision"


def test_usage_includes_snapshots_defaults_and_legacy_renditions(db_session):
    from bragi.core.models.page import Page
    from bragi.core.models.page_revision import PageRevision
    from bragi.core.models.page_working_copy import PageWorkingCopy
    from bragi.core.models.post_working_copy import PostWorkingCopy

    site = make_test_site(
        db_session,
        slug="blog",
        hostname="blog.test",
        title="Blog",
        canonical_url="https://canonical.test",
    )
    other = make_test_site(db_session, slug="other", hostname="other.test", title="Other")
    key = "a" * 64
    attachment = Attachment(
        site_id=site.id, filename="a.png", content_type="image/png", size_bytes=1, storage_key=key
    )
    db_session.add(attachment)
    db_session.flush()
    site.default_featured_image_id = attachment.id
    post = Post(site_id=site.id, author_id=site.owner_user_id, slug="p", title="P")
    page = Page(site_id=site.id, author_id=site.owner_user_id, slug="p", title="P")
    db_session.add_all([post, page])
    db_session.flush()
    for cls, kwargs in [
        (PostWorkingCopy, {"site_id": site.id, "post_id": post.id}),
        (PageWorkingCopy, {"site_id": site.id, "page_id": page.id, "kind": "page"}),
        (PageRevision, {"page_id": page.id, "status": "published"}),
    ]:
        db_session.add(
            cls(
                **kwargs,
                slug="p",
                title="P",
                featured_image_id=attachment.id,
                body_markdown=f"![legacy](https://canonical.test/attachments/{key}/1/png?x=1)",
            )
        )
    db_session.add(
        Post(
            site_id=other.id,
            author_id=other.owner_user_id,
            slug="p",
            title="Other",
            body_markdown=f"![other](/attachments/{key})",
        )
    )
    db_session.flush()
    refs = media_usage(db_session, site)
    assert len(refs) == 7
    assert {r.source_type for r in refs} == {
        "site",
        "post_working_copy",
        "page_working_copy",
        "page_revision",
    }
    assert {r.storage_key for r in refs} == {key, key + "/1/png"}
    assert len(attachment_usage(db_session, site, [attachment])[attachment.id]) == 7


def test_usage_recognizes_only_this_sites_aliases(db_session):
    from bragi.core.models.site_alias import SiteAlias

    site = make_test_site(db_session, slug="blog", hostname="blog.test", title="Blog")
    other = make_test_site(db_session, slug="other", hostname="other.test", title="Other")
    db_session.add_all(
        [
            SiteAlias(site_id=site.id, hostname="old-blog.test"),
            SiteAlias(site_id=other.id, hostname="old-other.test"),
        ]
    )
    own_key, other_key = "a" * 64, "b" * 64
    post = Post(
        site_id=site.id,
        author_id=site.owner_user_id,
        slug="p",
        title="P",
        body_markdown=f"![own](https://old-blog.test/attachments/{own_key}) "
        f"![other](https://old-other.test/attachments/{other_key})",
    )
    db_session.add(post)
    db_session.flush()
    assert [ref.storage_key for ref in media_usage(db_session, site)] == [own_key]


@pytest.mark.parametrize(
    "body_template",
    [
        "![used](https://blog.test:443/attachments/{key})",
        "![used](http://blog.test:8080/attachments/{key})",
        "![used](&#47;attachments&#47;{key})",
        "![used](/attachments%2F{key})",
        "<div>\n![used](&#47;attachments&#47;{key})\n</div>",
    ],
)
def test_usage_normalizes_rendered_local_urls(db_session, body_template):
    from urllib.parse import unquote, urlsplit

    from bs4 import BeautifulSoup

    from bragi.core.render.markdown import render_markdown

    site = make_test_site(db_session, slug="blog", hostname="blog.test", title="Blog")
    key = "a" * 64
    attachment = Attachment(
        site_id=site.id,
        filename="used.png",
        content_type="image/png",
        size_bytes=1,
        storage_key=key,
    )
    db_session.add(attachment)
    db_session.flush()
    body = body_template.format(key=key)
    src = BeautifulSoup(render_markdown(body), "html.parser").img["src"]
    assert unquote(urlsplit(src).path) == f"/attachments/{key}"
    db_session.add(
        Post(
            site_id=site.id,
            author_id=site.owner_user_id,
            slug="p",
            title="Published",
            status="published",
            body_markdown=body,
        )
    )
    db_session.flush()
    usages = attachment_usage(db_session, site, [attachment])[attachment.id]
    assert len(usages) == 1
    assert usages[0].storage_key == key and usages[0].status == "published"
