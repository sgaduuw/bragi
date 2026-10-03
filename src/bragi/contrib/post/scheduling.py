"""Site-local scheduling input and display; persisted instants remain naive UTC."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.time import naive_utcnow


def schedule_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            "The site timezone is invalid. Ask an administrator to correct it."
        ) from exc


def schedule_input(value: datetime | None, timezone: str) -> str:
    if value is None:
        return ""
    try:
        local = value.replace(tzinfo=UTC).astimezone(schedule_zone(timezone))
    except OverflowError as exc:
        raise ValueError("Publication time is outside the supported local range.") from exc
    return local.replace(tzinfo=None).isoformat(timespec="seconds")


def parse_schedule(
    raw: str, timezone: str, *, now: datetime, existing: datetime | None
) -> datetime:
    zone = schedule_zone(timezone)
    if not raw:
        raise ValueError("A publication time is required for Scheduled posts.")
    try:
        local = datetime.fromisoformat(raw)
        if "T" not in raw or local.tzinfo is not None:
            raise ValueError
    except ValueError as exc:
        raise ValueError("Enter a valid publication date and time in the site timezone.") from exc
    # Preserve existing instants, including imported seconds, overdue times and
    # either occurrence of a repeated hour. Only a changed schedule needs validation.
    try:
        if existing is not None and local.isoformat(timespec="seconds") == schedule_input(
            existing, timezone
        ):
            return existing
    except ValueError:
        # An existing instant outside the local range can still be replaced.
        pass
    candidates = set()
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        try:
            utc = aware.astimezone(UTC)
            roundtrip = utc.astimezone(zone).replace(tzinfo=None)
        except OverflowError as exc:
            raise ValueError("Publication time is outside the supported range.") from exc
        if roundtrip == local:
            candidates.add(utc.replace(tzinfo=None))
    if not candidates:
        raise ValueError(
            "This local time does not exist because the clocks move forward. Choose another time."
        )
    if len(candidates) > 1:
        raise ValueError(
            "This local time is ambiguous because the clocks move back. "
            "Choose a time outside the repeated hour."
        )
    value = candidates.pop()
    if value <= now:
        raise ValueError("Choose a publication time in the future.")
    return value


def schedule_details(post: Post, site: Site) -> dict[str, str] | None:
    if post.status != PostStatus.SCHEDULED:
        return None
    if post.scheduled_for is None:
        return {
            "state": "missing",
            "label": "Schedule needs a publication time.",
            "time": "",
            "error": "",
        }
    error = ""
    try:
        zone = schedule_zone(site.timezone)
        local = post.scheduled_for.replace(tzinfo=UTC).astimezone(zone)
    except (ValueError, OverflowError) as exc:
        zone = ZoneInfo("UTC")
        local = post.scheduled_for.replace(tzinfo=UTC)
        error = str(exc) + " Showing UTC."
    overdue = post.scheduled_for <= naive_utcnow()
    return {
        "state": "overdue" if overdue else "future",
        "label": "Publication overdue since" if overdue else "Scheduled for",
        "time": local.isoformat(sep=" ", timespec="seconds") + f" ({zone.key})",
        "error": error,
    }
