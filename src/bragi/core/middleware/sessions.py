"""Server-side session interface.

Replaces Flask's default signed-cookie sessions with a DB-backed
store. The cookie on the wire is named `bragi_sid` and carries
only an opaque UUID; everything else (user_id, CSRF token, flash
messages) lives in the `sessions` table.

Trade-offs vs Flask's default:

- Pro: logout invalidates the session row immediately, so a leaked
  cookie stops working the moment "Log out" is clicked anywhere.
- Pro: server holds the data; clients can't inspect it.
- Con: an extra DB read on every request and a DB write whenever
  the session is modified. Acceptable on the admin app (low QPS,
  authenticated users only). The delivery app keeps Flask's default
  cookie interface to stay write-free on the read path.

Install via `register_server_sessions(app)` from the admin app
factory. Idempotent within a single app instance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from flask import Flask
from flask.sessions import SessionInterface, SessionMixin
from flask.wrappers import Request, Response
from sqlalchemy import delete

from bragi.core.db import SessionLocal
from bragi.core.models.session import Session as SessionRow

COOKIE_NAME = "bragi_sid"
DEFAULT_LIFETIME = timedelta(days=14)


class BragiServerSession(dict[str, Any], SessionMixin):
    """A session that knows when it's been mutated.

    `modified` flips True on any write; the SessionInterface only
    persists when modified, so reads (page views without state
    changes) don't trigger writes. `new` distinguishes "no cookie
    yet" from "valid cookie, loaded data".

    `clear()` marks the current sid for destruction and resets to
    `new=True`. Any subsequent writes (e.g., a post-logout flash)
    land on a fresh sid; the old row is deleted in save_session.
    This is the "logout invalidates immediately" guarantee from
    CLAUDE.md.
    """

    def __init__(
        self,
        sid: str | None,
        initial: dict[str, Any] | None = None,
        *,
        new: bool = False,
    ) -> None:
        super().__init__(initial or {})
        self.sid: str | None = sid
        self.destroyed_sid: str | None = None
        self.modified = False
        self.new = new

    def __setitem__(self, key: str, value: Any) -> None:  # noqa: ANN401
        super().__setitem__(key, value)
        self.modified = True

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self.modified = True

    def clear(self) -> None:
        if self.sid is not None:
            self.destroyed_sid = self.sid
            self.sid = None
            self.new = True
        super().clear()
        self.modified = True

    def pop(self, key: str, default: Any = None) -> Any:  # noqa: ANN401
        self.modified = True
        return super().pop(key, default)

    def update(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().update(*args, **kwargs)
        self.modified = True


class BragiSessionInterface(SessionInterface):
    """SQLAlchemy-backed session store.

    `open_session` looks up the cookie's UUID, returning a fresh
    empty session if missing or expired. `save_session` upserts
    the row on modification, or deletes it (and the cookie) if the
    session was cleared.
    """

    def open_session(self, app: Flask, request: Request) -> BragiServerSession:
        sid = request.cookies.get(COOKIE_NAME)
        if not sid:
            return BragiServerSession(sid=None, new=True)

        now = datetime.now(UTC)
        with SessionLocal() as db:
            row = db.get(SessionRow, sid)
            if row is None:
                return BragiServerSession(sid=None, new=True)
            # Compare naive vs naive: SQLite stores datetimes naive
            # (UTC-implied). Drop tz from `now` for the compare.
            if row.expires_at < now.replace(tzinfo=None):
                db.delete(row)
                db.commit()
                return BragiServerSession(sid=None, new=True)
            row.last_seen_at = now.replace(tzinfo=None)
            data = dict(row.data or {})
            db.commit()
        return BragiServerSession(sid=sid, initial=data, new=False)

    def save_session(
        self,
        app: Flask,
        session: SessionMixin,
        response: Response,
    ) -> None:
        sess = cast(BragiServerSession, session)
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)

        # `clear()` records the old sid here. Delete the old row up
        # front; if the session is then empty we clear the cookie,
        # otherwise the upsert path below issues a new sid + cookie.
        if sess.destroyed_sid is not None:
            with SessionLocal() as db:
                old = db.get(SessionRow, sess.destroyed_sid)
                if old is not None:
                    db.delete(old)
                    db.commit()

        # Empty session: clear the cookie (and the row if any).
        if not sess:
            if sess.sid:
                with SessionLocal() as db:
                    row = db.get(SessionRow, sess.sid)
                    if row is not None:
                        db.delete(row)
                        db.commit()
            response.delete_cookie(COOKIE_NAME, domain=domain, path=path)
            return

        if not sess.modified and sess.sid and sess.destroyed_sid is None:
            # Read-only request on an existing session: nothing to do.
            return

        sid = sess.sid or uuid4().hex
        now = datetime.now(UTC).replace(tzinfo=None)
        expires_at = now + DEFAULT_LIFETIME
        from flask import request  # local import: only valid in request scope

        user_id = sess.get("user_id")
        ip = request.remote_addr
        ua = (request.headers.get("User-Agent") or "")[:512] or None

        with SessionLocal() as db:
            row = db.get(SessionRow, sid)
            if row is None:
                row = SessionRow(
                    id=sid,
                    user_id=user_id,
                    expires_at=expires_at,
                    last_seen_at=now,
                    ip=ip,
                    user_agent=ua,
                    data=dict(sess),
                )
                db.add(row)
            else:
                row.user_id = user_id
                row.expires_at = expires_at
                row.last_seen_at = now
                row.ip = ip
                row.user_agent = ua
                row.data = dict(sess)
            db.commit()

        response.set_cookie(
            COOKIE_NAME,
            sid,
            expires=expires_at,
            httponly=True,
            secure=self.get_cookie_secure(app),
            samesite=self.get_cookie_samesite(app) or "Lax",
            domain=domain,
            path=path,
        )


def register_server_sessions(app: Flask) -> None:
    """Replace `app.session_interface` with the DB-backed store."""
    app.session_interface = BragiSessionInterface()


def purge_expired_sessions() -> int:
    """Delete all sessions whose `expires_at` is in the past.

    Returns the number of rows deleted. Intended for a cron job or
    one-off cleanup (`flask cms session purge`).
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    with SessionLocal() as db:
        result = db.execute(delete(SessionRow).where(SessionRow.expires_at < now))
        db.commit()
        # CursorResult exposes rowcount; the static return type from
        # session.execute() is the broader Result, so a cast pins it.
        return int(getattr(result, "rowcount", 0) or 0)
