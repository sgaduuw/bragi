"""Unit tests for bragi.core.bulk_action.

Pure-logic tests: no Flask app, no DB, no live SQLAlchemy session.
The helper is exercised against an in-memory SQLite via SQLAlchemy
because it issues one SELECT; everything else is in-process.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from sqlalchemy import Column, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from bragi.core.bulk_action import (
    BulkLimitExceeded,
    BulkOutcome,
    DeletedItem,
    Ok,
    Skipped,
    bulk_delete,
    format_bulk_result,
)


class _Base(DeclarativeBase):
    pass


class _Site(_Base):
    __tablename__ = "sites"
    id = Column(Integer, primary_key=True)
    slug = Column(String, nullable=False)


class _Thing(_Base):
    __tablename__ = "things"
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)


@pytest.fixture()
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    _Base.metadata.create_all(engine)
    with Session(engine) as session:
        site = _Site(id=1, slug="a")
        session.add(site)
        session.flush()
        yield session


def _delete_ok(db: Session, site: _Site, row: _Thing) -> BulkOutcome:
    db.delete(row)
    # row.id / row.title are InstrumentedAttribute at the class level but
    # resolve to plain values at the instance level; mypy sees Column types
    # on the test-local DeclarativeBase models.
    return Ok(DeletedItem(id=row.id, title=row.title))  # type: ignore[arg-type]


def test_happy_path_deletes_all_rows(db: Session) -> None:
    site = db.get(_Site, 1)
    assert site is not None  # seeded in the db fixture
    db.add_all([_Thing(id=i, title=f"t{i}", site_id=site.id) for i in (10, 20, 30)])
    db.flush()

    result = bulk_delete(
        db=db,
        site=site,
        model=_Thing,
        ids=[10, 20, 30],
        delete_one=_delete_ok,
    )

    assert [item.id for item in result.deleted_rows] == [10, 20, 30]
    assert [item.title for item in result.deleted_rows] == ["t10", "t20", "t30"]
    assert result.skipped == []
    assert result.missing_count == 0


def _delete_skip_even(db: Session, site: _Site, row: _Thing) -> BulkOutcome:
    if row.id % 2 == 0:
        return Skipped(row.title, f"id {row.id} is even")  # type: ignore[arg-type]
    db.delete(row)
    return Ok(DeletedItem(id=row.id, title=row.title))  # type: ignore[arg-type]


def test_skipped_outcomes_accumulate(db: Session) -> None:
    site = db.get(_Site, 1)
    assert site is not None  # seeded in the db fixture
    db.add_all([_Thing(id=i, title=f"t{i}", site_id=site.id) for i in (1, 2, 3, 4)])
    db.flush()

    result = bulk_delete(
        db=db,
        site=site,
        model=_Thing,
        ids=[1, 2, 3, 4],
        delete_one=_delete_skip_even,
    )

    assert [item.id for item in result.deleted_rows] == [1, 3]
    assert result.skipped == [("t2", "id 2 is even"), ("t4", "id 4 is even")]
    assert result.missing_count == 0


def test_over_max_batch_raises(db: Session) -> None:
    site = db.get(_Site, 1)
    assert site is not None  # seeded in the db fixture
    with pytest.raises(BulkLimitExceeded) as exc:
        bulk_delete(
            db=db,
            site=site,
            model=_Thing,
            ids=list(range(1, 11)),
            delete_one=_delete_ok,
            max_batch=5,
        )
    assert "5" in str(exc.value)


def test_cross_site_and_missing_ids_count_as_missing(db: Session) -> None:
    other_site = _Site(id=2, slug="b")
    db.add(other_site)
    db.add(_Thing(id=99, title="other-site", site_id=other_site.id))
    db.add(_Thing(id=11, title="ours", site_id=1))
    db.flush()
    site = db.get(_Site, 1)
    assert site is not None  # seeded in the db fixture

    result = bulk_delete(
        db=db,
        site=site,
        model=_Thing,
        ids=[11, 99, 999],
        delete_one=_delete_ok,
    )

    assert [item.id for item in result.deleted_rows] == [11]
    assert result.skipped == []
    assert result.missing_count == 2  # 99 (wrong site) + 999 (does not exist)


# ---------------------------------------------------------------------------
# format_bulk_result tests
# ---------------------------------------------------------------------------


def _result(
    deleted: int = 0,
    skipped: Sequence[tuple[str, str]] = (),
) -> Any:
    return type(
        "R",
        (),
        {
            "deleted_rows": [DeletedItem(id=i, title=f"t{i}") for i in range(deleted)],
            "skipped": list(skipped),
            "missing_count": 0,
        },
    )()


def test_format_all_deleted_no_skips() -> None:
    result = _result(deleted=8)
    assert format_bulk_result(result, singular="post", plural="posts") == ("Deleted 8 posts.")


def test_format_one_deleted_singular_label() -> None:
    result = _result(deleted=1)
    assert format_bulk_result(result, singular="page", plural="pages") == ("Deleted 1 page.")


def test_format_mixed_skip_under_truncation() -> None:
    result = _result(deleted=2, skipped=[("About", "has 3 children")])
    assert format_bulk_result(result, singular="page", plural="pages") == (
        'Deleted 2 pages. Skipped 1: "About" (has 3 children).'
    )


def test_format_mixed_skip_at_truncation_boundary() -> None:
    result = _result(
        deleted=5,
        skipped=[
            ("About", "has 3 children"),
            ("Docs", "has 5 children"),
            ("Help", "has 1 child"),
        ],
    )
    assert format_bulk_result(result, singular="page", plural="pages") == (
        'Deleted 5 pages. Skipped 3: "About" (has 3 children), '
        '"Docs" (has 5 children), "Help" (has 1 child).'
    )


def test_format_mixed_skip_over_truncation_boundary() -> None:
    result = _result(
        deleted=5,
        skipped=[
            ("About", "has 3 children"),
            ("Docs", "has 5 children"),
            ("Help", "has 1 child"),
            ("Faq", "has 2 children"),
            ("News", "has 4 children"),
        ],
    )
    assert format_bulk_result(result, singular="page", plural="pages") == (
        'Deleted 5 pages. Skipped 5: "About" (has 3 children), '
        '"Docs" (has 5 children), "Help" (has 1 child), and 2 more.'
    )


def test_format_zero_deleted_with_skips_uses_distinct_phrasing() -> None:
    result = _result(deleted=0, skipped=[("About", "has 3 children")])
    assert format_bulk_result(result, singular="page", plural="pages") == (
        '0 pages deleted. Skipped 1: "About" (has 3 children).'
    )
