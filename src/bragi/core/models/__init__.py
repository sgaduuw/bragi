"""SQLAlchemy declarative base and model registry for bragi.

Every model inherits from `Base`. Models live here (NOT inside
contrib plugins) so alembic autogenerate has one source of truth.

To register a new model: add a module under this package, import
its class below, and re-export via `__all__`. The model's
`__tablename__` is then visible to `Base.metadata` which alembic
walks for autogenerate diffs.
"""

from __future__ import annotations

from bragi.core.models._base import Base
from bragi.core.models.activitypub import (
    ActivityPubFollower,
    ActivityPubOutbox,
    ActivityPubOutboxStatus,
    SiteKeypair,
)
from bragi.core.models.analytics_event import AnalyticsEvent
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.models.audit_log import AuditLog
from bragi.core.models.internal_link import InternalLink
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.page_revision import PageRevision
from bragi.core.models.personal_access_token import PersonalAccessToken, TokenScope
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.post_revision import PostRevision
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.session import Session
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias
from bragi.core.models.tag import Tag, post_tags
from bragi.core.models.user import User
from bragi.core.models.user_identity import UserIdentity
from bragi.core.models.user_site_role import Role, UserSiteRole
from bragi.core.models.webmention import (
    Webmention,
    WebmentionOutbox,
    WebmentionOutboxStatus,
    WebmentionStatus,
    WebmentionType,
)

__all__ = [
    "ActivityPubFollower",
    "ActivityPubOutbox",
    "ActivityPubOutboxStatus",
    "AnalyticsEvent",
    "Attachment",
    "AttachmentRendition",
    "AuditLog",
    "Base",
    "InternalLink",
    "LocalCredential",
    "MatchType",
    "Page",
    "PageRevision",
    "PageStatus",
    "PersonalAccessToken",
    "Post",
    "PostRevision",
    "PostStatus",
    "Redirect",
    "RedirectSource",
    "Role",
    "Session",
    "Site",
    "SiteAlias",
    "SiteKeypair",
    "Tag",
    "TokenScope",
    "User",
    "UserIdentity",
    "UserSiteRole",
    "Webmention",
    "WebmentionOutbox",
    "WebmentionOutboxStatus",
    "WebmentionStatus",
    "WebmentionType",
    "post_tags",
]
