"""Unit tests for bragi.core.bulk_action.

Pure-logic tests: no Flask app, no DB, no live SQLAlchemy session.
The helper is exercised against an in-memory SQLite via SQLAlchemy
because it issues one SELECT; everything else is in-process.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Column, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from bragi.core.bulk_action import (
    BulkOutcome,
    Ok,
    _DeletedItem,
    bulk_delete,
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
    return Ok(_DeletedItem(id=row.id, title=row.title))  # type: ignore[arg-type]


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
