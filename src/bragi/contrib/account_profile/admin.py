"""Admin Blueprint for the self-service account profile editor.

Mounted at `/admin/account/profile`. The logged-in user edits their own
`User` row: display name, bio, pronouns, location, avatar URL, and rel="me"
links. No role gate beyond being authenticated (you edit only yourself).

The rel="me" links reuse the shared `bragi.api.ProfileLink` model + a
repeatable-row form (mirroring the per-site profile-links editor); the avatar
URL is gated through `safe_external_url` before it is stored, since it is
rendered as an `<img src>` on the public profile surfaces.

Plugin boundary (see `_claude/CLAUDE.md`): imports from `bragi.api`,
`bragi.core`, `bragi.core.models` only.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from pydantic import TypeAdapter, ValidationError

from bragi.api import Crumb, ProfileLink, set_breadcrumbs
from bragi.core.db import SessionLocal
from bragi.core.models.user import User
from bragi.core.safe_urls import safe_external_url
from bragi.core.security import current_user

bp = Blueprint(
    "account_profile",
    __name__,
    template_folder="templates",
    url_prefix="/admin/account",
)

_LINKS_ADAPTER: TypeAdapter[list[ProfileLink]] = TypeAdapter(list[ProfileLink])


def _rows_from_form() -> list[dict[str, str]]:
    """Collect submitted rel=me rows, dropping fully-blank ones."""
    labels = request.form.getlist("profile_label")
    urls = request.form.getlist("profile_url")
    rows: list[dict[str, str]] = []
    for label, url in zip(labels, urls, strict=False):
        label, url = label.strip(), url.strip()
        if not label and not url:
            continue
        rows.append({"label": label, "url": url})
    return rows


def _errors_by_row(exc: ValidationError) -> dict[int, str]:
    """Map a ValidationError to {row index: first human message}."""
    out: dict[int, str] = {}
    for err in exc.errors():
        loc = err.get("loc", ())
        if loc and isinstance(loc[0], int):
            out.setdefault(loc[0], err.get("msg", "Invalid value."))
    return out


def _render(
    values: dict[str, str],
    rows: list[dict[str, str]],
    errors: dict[int, str],
    *,
    avatar_error: str | None = None,
) -> str:
    set_breadcrumbs(Crumb("Profile", None))
    display_rows = [{**r, "error": errors.get(i)} for i, r in enumerate(rows)]
    display_rows.append({"label": "", "url": "", "error": None})
    return render_template(
        "admin/account_profile.html",
        values=values,
        rows=display_rows,
        avatar_error=avatar_error,
    )


def _values_from_user(user: User) -> dict[str, str]:
    return {
        "display_name": user.display_name or "",
        "bio": user.bio or "",
        "pronouns": user.pronouns or "",
        "location": user.location or "",
        "avatar_url": user.avatar_url or "",
    }


@bp.route("/profile", methods=["GET", "POST"])
def edit() -> ResponseReturnValue:
    """View / save the logged-in user's own profile."""
    ctx = current_user()
    if ctx is None:
        abort(401)

    with SessionLocal() as db:
        user = db.get(User, ctx.id)
        if user is None:
            abort(401)

        if request.method == "POST":
            values = {
                "display_name": (request.form.get("display_name") or "").strip(),
                "bio": (request.form.get("bio") or "").strip(),
                "pronouns": (request.form.get("pronouns") or "").strip(),
                "location": (request.form.get("location") or "").strip(),
                "avatar_url": (request.form.get("avatar_url") or "").strip(),
            }
            rows = _rows_from_form()

            if not values["display_name"] or len(values["display_name"]) > 255:
                flash("A display name is required (max 255 characters).", "error")
                return _render(values, rows, {})

            # Avatar: gate the URL through the scheme allowlist (it becomes an
            # <img src> on the public profile). Empty clears it.
            avatar_url: str | None = None
            if values["avatar_url"]:
                avatar_url = safe_external_url(values["avatar_url"])
                if avatar_url is None:
                    flash("The avatar URL must be a valid http(s) URL.", "error")
                    return _render(values, rows, {}, avatar_error="Not a valid http(s) URL.")

            # rel=me links: both fields required per kept row, then validate
            # through the shared ProfileLink model (rejects non-http(s)).
            half_filled = {
                i: "Both a label and a URL are required."
                for i, r in enumerate(rows)
                if not r["label"] or not r["url"]
            }
            if half_filled:
                flash("Some links are invalid; nothing was saved.", "error")
                return _render(values, rows, half_filled)
            try:
                links = _LINKS_ADAPTER.validate_python(rows)
            except ValidationError as exc:
                flash("Some links are invalid; nothing was saved.", "error")
                return _render(values, rows, _errors_by_row(exc))

            user.display_name = values["display_name"]
            user.bio = values["bio"] or None
            user.pronouns = values["pronouns"] or None
            user.location = values["location"] or None
            user.avatar_url = avatar_url
            user.profile_links = [link.model_dump(mode="json") for link in links]
            db.commit()

            flash("Profile updated.", "success")
            return redirect(url_for("account_profile.edit"))

        existing = _LINKS_ADAPTER.validate_python(user.profile_links or [])
        rows = [{"label": link.label, "url": str(link.url)} for link in existing]
        return _render(_values_from_user(user), rows, {})
