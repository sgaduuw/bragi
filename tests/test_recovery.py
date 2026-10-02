"""File-backed backup/failed-upgrade rehearsal (#523).

Run with pytest. BRAGI_RECOVERY_PREVIOUS_PYTHON optionally selects a separately
installed prior release for seeding, backup and rollback verification. Without
it, CI exercises the current application without network access or Git tags.
The seed/check subprocess modes use synthetic data only.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwAC"
    "TGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)
RENDITION = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
PASSWORD = "recovery-rehearsal-only"
PUBLIC = "https://recovery.example.test"
ADMIN = "https://admin.example.test"


def seed_site() -> None:
    from bragi.apps.admin import create_admin_app
    from bragi.contrib.auth_local.passwords import hash_password
    from bragi.core.db import SessionLocal
    from bragi.core.models.attachment import Attachment
    from bragi.core.models.attachment_rendition import AttachmentRendition
    from bragi.core.models.local_credential import LocalCredential
    from bragi.core.models.page import Page, PageKind, PageStatus
    from bragi.core.models.post import Post, PostStatus
    from bragi.core.models.redirect import Redirect
    from bragi.core.models.site import Site
    from bragi.core.models.user import User
    from bragi.core.render.markdown import render_markdown
    from bragi.core.storage import store_original, store_rendition
    from bragi.core.time import naive_utcnow

    app = create_admin_app()
    with app.app_context(), SessionLocal() as db:
        user = User(email="recovery@example.test", display_name="Recovery", is_superuser=True)
        db.add(user)
        db.flush()
        db.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
        site = Site(
            slug="blog",
            hostname="recovery.example.test",
            title="Recovery site",
            canonical_url=PUBLIC,
            owner_user_id=user.id,
        )
        db.add(site)
        db.flush()
        key, size = store_original("blog", content_type="image/png", data=IMAGE)
        store_rendition("blog", key, width=1, format_slug="png", data=RENDITION)
        attachment = Attachment(
            site_id=site.id,
            filename="recovery.png",
            content_type="image/png",
            size_bytes=size,
            storage_key=key,
            width=2,
            height=2,
        )
        db.add(attachment)
        db.flush()
        db.add(
            AttachmentRendition(
                attachment_id=attachment.id,
                size_label="1w",
                format="png",
                storage_key=f"{key}/1/png",
                content_type="image/png",
                width=1,
                height=1,
                bytes_size=len(RENDITION),
                status="done",
            )
        )
        for slug, kind in (("posts", PageKind.POST_INDEX), ("about", PageKind.STATIC)):
            body = "Recovery page body"
            db.add(
                Page(
                    site_id=site.id,
                    author_id=user.id,
                    slug=slug,
                    title="Recovery page",
                    kind=kind,
                    status=PageStatus.PUBLISHED,
                    body_markdown=body,
                    body_html=render_markdown(body),
                )
            )
        body = f"Recovery post body\n\n![Recovery image](/attachments/{key})"
        db.add(
            Post(
                site_id=site.id,
                author_id=user.id,
                slug="recovered",
                title="Recovery post",
                status=PostStatus.PUBLISHED,
                published_at=naive_utcnow(),
                body_markdown=body,
                body_html=render_markdown(body),
                featured_image_id=attachment.id,
            )
        )
        db.add(Redirect(site_id=site.id, source_path="/old-post/", target="/posts/recovered/"))
        db.commit()


def check_site() -> None:
    from bs4 import BeautifulSoup
    from sqlalchemy import select

    from bragi.apps.admin import create_admin_app
    from bragi.apps.delivery import create_delivery_app
    from bragi.core.db import SessionLocal
    from bragi.core.models.post import Post
    from bragi.settings import settings

    assert settings.github_client_secret is None, "operator credential leaked"
    assert settings.github_client_id is None, "operator setting leaked"
    admin = create_admin_app()
    admin.config["TESTING"] = True
    client = admin.test_client()
    assert client.get("/admin/sites/blog/posts/", base_url=ADMIN).status_code == 302
    login = client.get("/auth/login", base_url=ADMIN)
    assert login.status_code == 200
    field = BeautifulSoup(login.data, "html.parser").select_one('input[name="_csrf_token"]')
    assert field is not None
    response = client.post(
        "/auth/login",
        base_url=ADMIN,
        data={
            "email": "recovery@example.test",
            "password": PASSWORD,
            "_csrf_token": field["value"],
        },
    )
    assert response.status_code == 302
    with client.session_transaction(base_url=ADMIN) as session:
        assert session["user_id"] == 1
    assert client.get("/admin/sites/blog/posts/", base_url=ADMIN).status_code == 200
    # Also read the editing surface, which exercises the current working-copy schema.
    assert client.get("/admin/sites/blog/posts/1/edit", base_url=ADMIN).status_code == 200

    delivery = create_delivery_app()
    delivery.config["TESTING"] = True
    public = delivery.test_client()
    for path, text in (
        ("/posts/recovered/", "Recovery post body"),
        ("/about/", "Recovery page body"),
    ):
        response = public.get(path, base_url=PUBLIC)
        assert response.status_code == 200, (path, response.status_code)
        assert text in BeautifulSoup(response.data, "html.parser").get_text()
    redirected = public.get("/old-post/", base_url=PUBLIC)
    assert redirected.status_code == 301
    assert redirected.headers["Location"] == "/posts/recovered/"
    key = hashlib.sha256(IMAGE).hexdigest()
    for storage_key, expected in ((key, IMAGE), (f"{key}/1/png", RENDITION)):
        response = public.get(f"/attachments/{storage_key}", base_url=PUBLIC)
        assert response.status_code == 200, f"missing restored media: {storage_key}"
        assert response.mimetype == "image/png"
        assert response.data == expected, f"wrong restored media: {storage_key}"
    with SessionLocal() as db:
        post = db.execute(select(Post)).scalar_one()
        assert post.body_markdown == f"Recovery post body\n\n![Recovery image](/attachments/{key})"
    print("verified login, editing, post, page, redirect, original and rendition")


def test_backup_recovers_site_after_failed_upgrade(tmp_path: Path, monkeypatch) -> None:
    """Catch incomplete media backups, lost credentials/content, and migration regressions."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from PIL import Image

    import bragi

    assert IMAGE != RENDITION
    for data, dimensions in ((IMAGE, (2, 2)), (RENDITION, (1, 1))):
        with Image.open(BytesIO(data)) as fixture:
            assert fixture.size == dimensions
            fixture.verify()
    monkeypatch.setenv("bragi_github_client_secret", "synthetic-recovery-secret")
    monkeypatch.setenv("BrAgI_GITHUB_CLIENT_ID", "synthetic-recovery-client")
    previous_python = os.environ.get("BRAGI_RECOVERY_PREVIOUS_PYTHON", sys.executable)
    # Do not inherit operator Bragi/Flask settings, source overrides or a local .env.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(("BRAGI_", "FLASK_")) and k not in ("PYTHONPATH", "PYTHONHOME")
    }
    env.update(BRAGI_ENV="production", BRAGI_SECRET_KEY=secrets.token_hex(32))
    source = tmp_path / "source"
    source.mkdir()
    script = str(Path(__file__).resolve())

    def run(python: str, args: list[str], root: Path, *, succeeds: bool = True):
        result = subprocess.run(
            [python, *args],
            cwd=tmp_path,
            env=dict(
                env,
                BRAGI_DATABASE_URL=f"sqlite:///{root / 'bragi.db'}",
                BRAGI_ATTACHMENTS_ROOT=str(root / "attachments"),
            ),
            capture_output=True,
            text=True,
            timeout=90,
        )
        if succeeds:
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            assert result.returncode != 0, "fault injection unexpectedly succeeded"
        return result

    cli = ["-c", "from bragi.cli import bragi; bragi()"]
    run(previous_python, [*cli, "db", "upgrade"], source)
    run(previous_python, [script, "seed"], source)
    run(previous_python, [script, "check"], source)
    with sqlite3.connect(source / "bragi.db") as db:
        previous_revision = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert db.execute("SELECT count(*) FROM posts").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM attachments").fetchone()[0] == 1
        assert db.execute("SELECT name FROM sqlite_master WHERE name='posts_fts'").fetchone()
    archive = tmp_path / "before-upgrade.tar.gz"
    # Every subprocess has exited; no writers run during either backup phase.
    run(previous_python, [*cli, "backup", "--output", str(archive)], source)

    migrations = tmp_path / "migrations"
    shutil.copytree(Path(bragi.__file__).parent / "alembic", migrations)
    config = Config()
    config.set_main_option("script_location", str(migrations))
    target_revision = ScriptDirectory.from_config(config).get_current_head()
    (migrations / "versions" / "rehearsal_failure.py").write_text(
        f'revision = "rehearsal_failure"\ndown_revision = {target_revision!r}\n'
        "from alembic import op\n"
        "def upgrade():\n"
        '    op.execute("CREATE TABLE partial_upgrade (id INTEGER)")\n'
        '    raise RuntimeError("rehearsal migration failed after DDL")\n'
    )
    # Apply real current migrations first, then a deliberately failing migration.
    run(sys.executable, [*cli, "db", "upgrade"], source)
    failed = run(
        sys.executable,
        [
            "-c",
            "from alembic import command; from alembic.config import Config; "
            "import os, sys; c = Config(); c.set_main_option('script_location', sys.argv[1]); "
            "c.set_main_option('sqlalchemy.url', os.environ['BRAGI_DATABASE_URL']); "
            "command.upgrade(c, 'head')",
            str(migrations),
        ],
        source,
        succeeds=False,
    )
    assert "rehearsal migration failed after DDL" in failed.stderr
    with sqlite3.connect(source / "bragi.db") as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='partial_upgrade'").fetchone()
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            target_revision,
        )

    restored = tmp_path / "restored"
    restored.mkdir()
    assert not list(restored.iterdir())
    with tarfile.open(archive) as backup:
        assert "bragi.db" in backup.getnames()
        assert any(name.startswith("attachments/") for name in backup.getnames())
        backup.extractall(restored, filter="data")
    with sqlite3.connect(restored / "bragi.db") as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            previous_revision,
        )
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='partial_upgrade'"
        ).fetchone()
    run(previous_python, [script, "check"], restored)
    # A missing-media restore must fail even when login and public HTML still work.
    media = restored / "attachments"
    media.rename(restored / "held-media")
    missing = run(previous_python, [script, "check"], restored, succeeds=False)
    assert "missing restored media" in missing.stderr
    (restored / "held-media").rename(media)
    run(previous_python, [script, "check"], restored)
    # Upgrade a populated, restored database, rather than a metadata-created fixture.
    run(sys.executable, [*cli, "db", "upgrade"], restored)
    run(sys.executable, [script, "check"], restored)
    with sqlite3.connect(restored / "bragi.db") as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            target_revision,
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    print(f"recovery verified: schema {previous_revision} -> {target_revision}")


if __name__ == "__main__":
    {"seed": seed_site, "check": check_site}[sys.argv[1]]()
