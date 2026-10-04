import pytest
from flask import Flask

from bragi.contrib.attachments.cli import media_group
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.models.page import Page
from tests.conftest import make_test_site


def test_check_reports_missing_rows_and_bytes_without_writing(
    db_session, patched_session_locals, monkeypatch
):
    site = make_test_site(db_session, slug="blog", hostname="blog.test", title="Blog")
    key = "a" * 64
    missing = "b" * 64
    attachment = Attachment(
        site_id=site.id, filename="a.png", content_type="image/png", size_bytes=1, storage_key=key
    )
    db_session.add(attachment)
    db_session.flush()
    rendition = AttachmentRendition(
        attachment_id=attachment.id,
        size_label="320w",
        format="webp",
        storage_key=key + "/320/webp",
        content_type="image/webp",
        status="done",
    )
    db_session.add(rendition)
    page = Page(
        site_id=site.id,
        author_id=site.owner_user_id,
        slug="page",
        title="Page",
        body_markdown=f"![original](/attachments/{key})"
        f" ![small](/attachments/{key}/320/webp) ![lost](/attachments/{missing})",
    )
    db_session.add(page)
    db_session.commit()
    calls = []
    missing_keys = {key + "/320/webp"}

    class Storage:
        def read(self, slug, storage_key):
            calls.append((slug, storage_key))
            if storage_key in missing_keys:
                raise FileNotFoundError(storage_key)
            return b"image"

    monkeypatch.setattr("bragi.contrib.attachments.cli.resolve_storage", lambda app: Storage())
    app = Flask(__name__)
    app.cli.add_command(media_group)
    result = app.test_cli_runner().invoke(args=["media", "check", "--site", "blog"])
    assert result.exit_code == 1, result.output
    assert result.output.splitlines()[:3] == [
        f"page #{page.id} 'Page' [draft] body_markdown: /attachments/{key}/320/webp: missing bytes",
        f"page #{page.id} 'Page' [draft] body_markdown: /attachments/{missing}: missing record",
        "Checked 3 known references; 2 missing.",
    ]
    assert calls == [("blog", key), ("blog", key + "/320/webp")]
    assert db_session.get(Attachment, attachment.id) is not None
    page.body_markdown = f"![original](/attachments/{key})"
    db_session.commit()
    result = app.test_cli_runner().invoke(args=["media", "check", "--site", "blog"])
    assert result.exit_code == 1, result.output
    missing_keys.clear()
    result = app.test_cli_runner().invoke(args=["media", "check", "--site", "blog"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "Checked 2 known references; 0 missing."


@pytest.mark.parametrize("usage", ["featured", "default", "body"])
def test_check_reports_missing_implicit_renditions(
    db_session, patched_session_locals, monkeypatch, usage
):
    from bragi.core.seo import featured_image_url_for

    site = make_test_site(
        db_session,
        slug="blog",
        hostname="blog.test",
        title="Blog",
        canonical_url="https://blog.test",
    )
    key = "c" * 64
    attachment = Attachment(
        site_id=site.id,
        filename="c.png",
        content_type="image/png",
        size_bytes=1,
        storage_key=key,
    )
    db_session.add(attachment)
    db_session.flush()
    page = Page(
        site_id=site.id,
        author_id=site.owner_user_id,
        slug="p",
        title="Published",
        status="published",
    )
    if usage == "featured":
        page.featured_image_id = attachment.id
    elif usage == "default":
        site.default_featured_image_id = attachment.id
    else:
        page.body_markdown = f"![image](/attachments/{key})"
    db_session.add(page)
    db_session.add(
        AttachmentRendition(
            attachment_id=attachment.id,
            size_label="320w",
            format="webp",
            storage_key=key + "/320/webp",
            content_type="image/webp",
            status="done",
            width=320,
        )
    )
    db_session.commit()
    if usage != "body":
        assert featured_image_url_for(item=page, site=site, db=db_session).endswith("/320/webp")
    calls = []

    class Storage:
        def read(self, slug, storage_key):
            calls.append(storage_key)
            if storage_key.endswith("/webp"):
                raise FileNotFoundError(storage_key)
            return b"original"

    monkeypatch.setattr("bragi.contrib.attachments.cli.resolve_storage", lambda app: Storage())
    app = Flask(__name__)
    app.cli.add_command(media_group)
    result = app.test_cli_runner().invoke(args=["media", "check", "--site", "blog"])
    assert result.exit_code == 1, result.output
    assert f"/attachments/{key}/320/webp: missing bytes" in result.output
    assert calls == [key, key + "/320/webp"]
