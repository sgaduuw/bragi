"""Dataset / DatasetQuery model shape (#42)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.core.models import Dataset, DatasetQuery
from tests.conftest import make_test_site


def _make_dataset(db_session: Session, **kw) -> Dataset:
    site = make_test_site(
        db_session,
        slug=kw.pop("site_slug", "blog"),
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
    )
    ds = Dataset(
        site_id=site.id,
        slug=kw.pop("slug", "cpi"),
        name=kw.pop("name", "CPI series"),
        source_type=kw.pop("source_type", "duckdb"),
        storage_key=kw.pop("storage_key", "a" * 64),
        size_bytes=kw.pop("size_bytes", 123),
        content_sha=kw.pop("content_sha", "a" * 64),
        **kw,
    )
    db_session.add(ds)
    db_session.commit()
    return ds


def test_dataset_roundtrip(db_session: Session) -> None:
    ds = _make_dataset(db_session)
    row = db_session.execute(select(Dataset).where(Dataset.slug == "cpi")).scalar_one()
    assert row.id == ds.id
    assert row.source_type == "duckdb"
    assert row.created_at is not None


def test_dataset_query_default_format_defaults_to_table(db_session: Session) -> None:
    ds = _make_dataset(db_session)
    dq = DatasetQuery(dataset_id=ds.id, name="defaulted", sql="SELECT 1")
    db_session.add(dq)
    db_session.commit()
    row = db_session.execute(select(DatasetQuery)).scalar_one()
    assert row.default_format == "table"


def test_dataset_query_cascade_on_dataset_delete(db_session: Session) -> None:
    ds = _make_dataset(db_session)
    dq = DatasetQuery(
        dataset_id=ds.id, name="by-quarter", sql="SELECT * FROM cpi", default_format="table"
    )
    db_session.add(dq)
    db_session.commit()
    db_session.delete(ds)
    db_session.commit()
    assert db_session.execute(select(DatasetQuery)).scalar_one_or_none() is None
