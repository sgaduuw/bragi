"""Shared eligibility for 404 triage and cleanup."""

from sqlalchemy import select
from sqlalchemy.sql.selectable import Exists

from bragi.core.models.not_found import NotFound
from bragi.core.models.redirect import MatchType, Redirect


def covered_by_exact_redirect(site_id: int) -> Exists:
    return (
        select(Redirect.id)
        .where(
            Redirect.site_id == site_id,
            Redirect.source_path == NotFound.path,
            Redirect.match_type == MatchType.EXACT,
            Redirect.active.is_(True),
        )
        .correlate(NotFound)
        .exists()
    )
