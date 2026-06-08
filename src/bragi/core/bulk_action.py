"""Shared loop machinery for bulk admin actions.

Owns the per-batch flow (id parsing, multisite-scope filter, per-row
callable dispatch, accumulator). Each contrib supplies a per-row
callable that encodes its delete semantics; both single-delete and
bulk-delete routes call the same callable so a bulk delete of N rows
is semantically equivalent to N single deletes collapsed into one
transaction.

The helper does NOT commit. The caller commits once at the end of
the batch. This preserves per-contrib "fire hooks before commit"
semantics for whatever the per-row callable needs (post / page hook
fanout, attachment refcount-aware on-disk cleanup) and gives bulk
one commit for N rows.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session


@runtime_checkable
class _HasIdAndSiteId(Protocol):
    """Structural minimum required by bulk_delete on each row type.

    Any SQLAlchemy model that carries `id` and `site_id` satisfies this
    protocol. The Protocol bound lets mypy verify attribute access on `T`
    without requiring a concrete base class.
    """

    id: int
    site_id: int


T = TypeVar("T", bound=_HasIdAndSiteId)


@dataclass(frozen=True)
class _DeletedItem:
    """Snapshot of a row taken before delete, captured for post-commit
    use (audit row, cache purge key) when the live ORM object is
    detached/expired."""

    id: int
    title: str
    extras: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Ok:
    """A row that was deleted successfully."""

    item: _DeletedItem


@dataclass(frozen=True)
class Skipped:
    """A row the per-row callable refused to delete, with the reason
    surfaced to the operator. Real exceptions are not skips; they
    propagate."""

    title: str
    reason: str


type BulkOutcome = Ok | Skipped


@dataclass(frozen=True)
class BulkResult:
    deleted_rows: list[_DeletedItem]
    skipped: list[tuple[str, str]]
    missing_count: int


class BulkLimitExceeded(Exception):
    """Raised when an inbound id list exceeds `max_batch`. The caller
    flashes the exception string and returns to the list view."""


def bulk_delete(
    *,
    db: Session,
    site: Any,
    model: type[T],
    ids: Sequence[int],
    delete_one: Callable[[Session, Any, T], BulkOutcome],
    max_batch: int = 200,
) -> BulkResult:
    """Run `delete_one` over every row in `ids` scoped to `site`.

    Returns a BulkResult; caller is responsible for committing.
    """
    if len(ids) > max_batch:
        raise BulkLimitExceeded(f"Bulk delete is limited to {max_batch} items per request.")

    rows = (
        db.execute(
            select(model).where(
                model.id.in_(ids),  # type: ignore[attr-defined]
                model.site_id == site.id,
            )
        )
        .scalars()
        .all()
    )
    found_ids = {r.id for r in rows}
    missing = len(set(ids) - found_ids)

    deleted: list[_DeletedItem] = []
    skipped: list[tuple[str, str]] = []
    for row in rows:
        outcome = delete_one(db, site, row)
        if isinstance(outcome, Ok):
            deleted.append(outcome.item)
        else:
            skipped.append((outcome.title, outcome.reason))

    return BulkResult(deleted_rows=deleted, skipped=skipped, missing_count=missing)
