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
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

# `bulk_delete`'s `T` is declared inline via PEP 695 (`def bulk_delete[T]
# (...)`). It is intentionally unbound: SQLAlchemy ORM models expose `id`
# and `site_id` as InstrumentedAttribute descriptors at the class level,
# not plain `int` values, so a structural Protocol bound would fail mypy's
# type-var check on every call site. The attribute accesses below carry
# `type: ignore` comments instead, which is the idiomatic SQLAlchemy
# pattern.


@dataclass(frozen=True)
class DeletedItem:
    """Snapshot of a row taken before delete, captured for post-commit
    use (audit row, cache purge key) when the live ORM object is
    detached/expired."""

    id: int
    title: str
    extras: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Ok:
    """A row that was deleted successfully."""

    item: DeletedItem


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
    """Accumulated outcome of a bulk_delete call.

    Returned to the caller so it can flash appropriate messages and
    queue any post-commit work (cache purge, audit row) without
    re-querying the database.
    """

    # Snapshots of rows that were successfully deleted (per-row callable returned Ok).
    deleted_rows: list[DeletedItem]
    # (title, reason) pairs for rows the per-row callable refused (Skipped).
    skipped: list[tuple[str, str]]
    # Count of ids the SELECT did not resolve: wrong site or already gone.
    missing_count: int


class BulkLimitExceeded(Exception):
    """Raised when an inbound id list exceeds `max_batch`. The caller
    flashes the exception string and returns to the list view."""


def bulk_delete[T](
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

    Rows are deleted in ascending id order. This is the only ordering guarantee.
    """
    if len(ids) > max_batch:
        raise BulkLimitExceeded(f"Bulk delete is limited to {max_batch} items per request.")

    rows = (
        db.execute(
            select(model)
            .where(
                model.id.in_(ids),  # type: ignore[attr-defined]
                model.site_id == site.id,  # type: ignore[attr-defined]
            )
            .order_by(model.id)  # type: ignore[attr-defined]
        )
        .scalars()
        .all()
    )
    found_ids = {r.id for r in rows}  # type: ignore[attr-defined]
    missing = len(set(ids) - found_ids)

    deleted: list[DeletedItem] = []
    skipped: list[tuple[str, str]] = []
    for row in rows:
        outcome = delete_one(db, site, row)
        if isinstance(outcome, Ok):
            deleted.append(outcome.item)
        else:
            skipped.append((outcome.title, outcome.reason))

    return BulkResult(deleted_rows=deleted, skipped=skipped, missing_count=missing)


_SKIP_TRUNCATE = 3


def format_bulk_result(
    result: BulkResult, *, singular: str, plural: str, verb: str = "Deleted"
) -> str:
    """Produce the operator-facing flash string for a bulk-action result.

    `verb` is the past-tense action word ("Deleted", "Recomputed"); the
    default keeps existing delete callers unchanged.

    Singular/plural labels are passed in so this is content-type
    agnostic; the routes choose them. Skips beyond the first three are
    summarised as "and N more". `missing_count` is intentionally
    omitted: the operator already lost the race for those rows and a
    flash about them is noise.
    """
    n = len(result.deleted_rows)
    label = singular if n == 1 else plural
    head = f"{verb} {n} {label}." if n > 0 else f"0 {plural} {verb.lower()}."

    if not result.skipped:
        return head

    visible = result.skipped[:_SKIP_TRUNCATE]
    parts = [f'"{title}" ({reason})' for title, reason in visible]
    if len(result.skipped) > _SKIP_TRUNCATE:
        parts.append(f"and {len(result.skipped) - _SKIP_TRUNCATE} more")
    tail = f" Skipped {len(result.skipped)}: " + ", ".join(parts) + "."
    return head + tail
