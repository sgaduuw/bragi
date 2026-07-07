"""Site model: one row per tenant in the multisite deployment.

The Host header at the WSGI edge resolves to a Site row. Every
content table has a `site_id` FK. Additional Site fields
(aliases, theme, robots_txt override, etc.) land in follow-up
migrations as the features that need them ship.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class Site(IdMixin, TimestampsMixin, Base):
    __tablename__ = "sites"

    slug: Mapped[str] = mapped_column(String(64), unique=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    locale: Mapped[str] = mapped_column(String(16), default="en")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    canonical_url: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(default=True)
    # The user who owns this site. Distinct from `UserSiteRole`
    # (which carries collaborator roles): ownership is a uniqueness
    # invariant and ownership transfer is a deliberate act, both of
    # which muddy when mixed with team roles. The owner is implicit
    # admin on permission checks; they don't need a UserSiteRole
    # row to act on their own site.
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # NULL means "no theme override, render with the plugin /
    # default templates as today." A non-null slug must match a
    # registered ThemeSpec.slug at request time; an orphaned value
    # (slug no longer installed) falls back to the default chain
    # rather than 500'ing, so an operator can uninstall a theme
    # without breaking sites that still reference it.
    theme: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # Plugin-contributed flat key/value bag. Deliberately
    # schemaless: reads are always "all settings for this site",
    # never "sites where setting X == Y", so a flat JSON column is
    # cheaper than a key-value table. MutableDict tracks in-place
    # mutations so `site.extra_settings["x"] = 1` survives commit
    # without a manual reassignment.
    extra_settings: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict
    )
    # Optional static homepage. When NULL (default), `/` falls
    # through to the post-index landing page (the post plugin's
    # resolve_home fallback). When set, `/` renders the referenced
    # Page via the page plugin's resolve_home tryfirst impl.
    # ON DELETE SET NULL so deleting the target Page cleanly
    # reverts the site to the default landing page instead of
    # leaving a dangling FK.
    home_page_id: Mapped[int | None] = mapped_column(
        ForeignKey("pages.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        index=True,
    )
    # Default featured image for posts and pages that don't specify
    # one of their own. Used wherever a post / page reads
    # `featured_image_id` (OG meta, landing-page cards, etc.).
    # NULL leaves the meta tag omitted entirely. ON DELETE SET NULL
    # so removing the attachment cleanly reverts the site to no
    # default image.
    default_featured_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )

    @property
    def base_url(self) -> str:
        """`canonical_url` without a trailing slash, for URL joins.

        Every absolute-URL builder concatenates `base + "/path"`, so a
        stored value that carries a trailing slash (operators type
        "https://host/" freely) would otherwise yield "https://host//path".
        Stripping here makes the stored slash harmless at every call site,
        so existing rows are correct without a re-save. Empty stays empty
        (falsy), so `if site.base_url` guards behave like `canonical_url`.
        """
        return (self.canonical_url or "").rstrip("/")
