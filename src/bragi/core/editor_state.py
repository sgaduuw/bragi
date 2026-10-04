"""Optimistic editor tokens checked while holding SQLite's writer lock."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from flask import current_app, flash, g, request, session
from itsdangerous import BadData, URLSafeSerializer
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session, object_session

from bragi.core.models.post import Post
from bragi.core.models.post_working_copy import PostWorkingCopy
from bragi.core.models.tag import Tag


def lock_editor_write(db: Session) -> None:
    """End the read snapshot, expire loaded rows, then lock before rechecking.

    Call after slow rendering and before any mutations. Rollback prevents
    identity-map state loaded before rendering from passing a stale check.
    """
    if request.method == "POST":
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        # Permissions must also reflect revocations during slow rendering.
        g.pop("_cached_user", None)


def fingerprint(item: Any) -> str | None:
    if item is None:
        return None
    state = {column.key: getattr(item, column.key) for column in inspect(type(item)).columns}
    if isinstance(item, Post):
        # Relationship changes do not update the parent's updated_at.
        state["tags"] = sorted((tag.id, tag.slug, tag.label) for tag in item.tags)
    elif isinstance(item, PostWorkingCopy):
        db = object_session(item)
        assert db is not None
        state["tags"] = [
            tuple(row)
            for row in db.execute(
                select(Tag.id, Tag.slug, Tag.label)
                .where(Tag.site_id == item.site_id, Tag.id.in_(item.tag_ids or []))
                .order_by(Tag.id)
            )
        ]
    encoded = json.dumps(state, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _serializer() -> URLSafeSerializer:
    secret_key = current_app.secret_key
    assert secret_key is not None
    return URLSafeSerializer(secret_key, salt="editor-state-v1")


def _state(item: Any, working_copy: Any = None) -> list[Any]:
    return [session["user_id"], type(item).__name__, fingerprint(item), fingerprint(working_copy)]


def editor_token(item: Any, working_copy: Any = None) -> str:
    # Validation/conflict responses keep the original baseline. A retry must
    # never turn an old submission into permission to overwrite newer work.
    if request.method == "POST":
        return request.form.get("_edit_token", "")
    return _serializer().dumps(_state(item, working_copy)) if item is not None else ""


def editor_matches(item: Any, working_copy: Any = None) -> bool:
    try:
        return bool(
            _serializer().loads(request.form.get("_edit_token", "")) == _state(item, working_copy)
        )
    except BadData:
        return False


def editor_conflict(*, saved_copy: bool = False, missing_copy: bool = False) -> None:
    g.editor_conflict = True
    message = (
        "This content changed after you opened the editor. Your submitted edits are preserved "
        "below. Open the latest version in another tab, compare your changes, and apply them "
        "there. Nothing was overwritten."
    )
    if saved_copy:
        message = (
            "The live content or working copy changed. Nothing was promoted or discarded. "
            "The current saved content is shown below; unsaved browser edits remain in local "
            "recovery. Compare with the latest live editor before staging again."
        )
    if missing_copy:
        message = (
            "The working copy no longer exists. Nothing was saved or promoted. "
            "Submitted fields are shown below, and browser drafts remain in local recovery. "
            "Open the live editor in another tab to compare and stage a new working copy."
        )
    flash(message, "error")


def editor_saved() -> None:
    """Acknowledge only a committed save of the browser's exact draft record."""
    try:
        record_id = str(UUID(request.form.get("_recovery_id", "")))
    except ValueError:
        return
    flash(record_id, "editor-saved")
