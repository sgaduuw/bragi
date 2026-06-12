"""`::: dataset :::` directive: parsing and end-to-end render (#42).

End-to-end tests go straight through a hand-configured MarkdownIt
(no Flask app): site identity arrives via the markdown-it `env`,
exactly as the rerender path supplies it. The storage backend
resolves to the local fallback outside an app context, pointed at
tmp_path via the patched `attachments_root`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb
import pytest
from markdown_it import MarkdownIt
from sqlalchemy.orm import Session, sessionmaker

from bragi.contrib.datasets.directive import configure_datasets
from bragi.core.models import Dataset, DatasetQuery
from bragi.core.storage import storage_path_for
from bragi.settings import settings
from tests.conftest import make_test_site


@pytest.fixture
def md() -> MarkdownIt:
    instance = MarkdownIt()
    configure_datasets(instance)
    return instance


@pytest.fixture
def seeded_dataset(
    db_session: Session,
    patched_session_locals: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A Site + Dataset whose .duckdb bytes are on local storage."""
    del patched_session_locals
    monkeypatch.setattr(settings, "attachments_root", str(tmp_path / "uploads"))

    site = make_test_site(
        db_session,
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
    )

    dbfile = tmp_path / "cpi.duckdb"
    con = duckdb.connect(str(dbfile))
    con.execute("CREATE TABLE cpi (quarter VARCHAR, value DOUBLE)")
    con.execute("INSERT INTO cpi VALUES ('2025Q1', 102.5), ('2025Q2', 103.1)")
    con.close()
    data = dbfile.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    dest = storage_path_for(site.slug, sha)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    ds = Dataset(
        site_id=site.id,
        slug="cpi",
        name="CPI",
        source_type="duckdb",
        storage_key=sha,
        size_bytes=len(data),
        content_sha=sha,
    )
    db_session.add(ds)
    db_session.flush()
    db_session.add(
        DatasetQuery(
            dataset_id=ds.id,
            name="by-quarter",
            sql="SELECT quarter, value FROM cpi ORDER BY quarter",
            default_format="table",
        )
    )
    db_session.add(
        DatasetQuery(
            dataset_id=ds.id,
            name="trend",
            sql="SELECT quarter, value FROM cpi ORDER BY quarter",
            default_format="chart",
            vega_spec_json='{"mark": "line", "encoding": {}}',
        )
    )
    db_session.commit()
    return site, ds


def _render(md: MarkdownIt, text: str, site_id: int) -> str:
    return md.render(text, {"bragi_site_id": site_id})


def test_named_query_renders_table(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    html = _render(md, "::: dataset slug=cpi q=by-quarter\n:::\n", site.id)
    assert '<table class="bragi-dataset-table">' in html
    assert "2025Q1" in html


def test_named_chart_query_renders_chart(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    html = _render(md, "::: dataset slug=cpi q=trend\n:::\n", site.id)
    assert 'class="bragi-dataset-chart"' in html
    assert "<noscript>" in html


def test_inline_sql_scalar(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    html = _render(
        md, '::: dataset slug=cpi sql="SELECT max(value) FROM cpi" format=scalar\n:::\n', site.id
    )
    assert '<span class="bragi-dataset-scalar">103.1</span>' in html


def test_format_override_on_named_query(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    html = _render(md, "::: dataset slug=cpi q=by-quarter format=scalar\n:::\n", site.id)
    assert "bragi-dataset-scalar" in html


def test_unknown_slug_bakes_error_card(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    html = _render(md, "::: dataset slug=nope q=by-quarter\n:::\n", site.id)
    assert "bragi-dataset--error" in html
    assert 'data-dataset-slug="nope"' in html


def test_unknown_query_name_bakes_error_card(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    html = _render(md, "::: dataset slug=cpi q=nope\n:::\n", site.id)
    assert "bragi-dataset--error" in html


def test_sql_error_bakes_error_card(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    html = _render(
        md, '::: dataset slug=cpi sql="SELECT broken FROM nowhere" format=table\n:::\n', site.id
    )
    assert "bragi-dataset--error" in html


def test_chart_requires_named_query(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    html = _render(md, '::: dataset slug=cpi sql="SELECT 1" format=chart\n:::\n', site.id)
    assert "bragi-dataset--error" in html


def test_inline_sql_requires_format(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    html = _render(md, '::: dataset slug=cpi sql="SELECT 1"\n:::\n', site.id)
    assert "bragi-dataset--error" in html


def test_missing_site_context_bakes_error_card(md, seeded_dataset) -> None:
    html = md.render("::: dataset slug=cpi q=by-quarter\n:::\n", {})
    assert "bragi-dataset--error" in html


def test_unclosed_block_declines(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    html = _render(md, "::: dataset slug=cpi q=by-quarter\nno close here", site.id)
    # The rule declines; markdown-it renders it as a paragraph.
    assert "bragi-dataset" not in html


def test_non_dataset_container_untouched(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    html = _render(md, "::: embed https://example.com\n:::\n", site.id)
    assert "bragi-dataset" not in html


def test_bad_memory_limit_bakes_error_card(
    md, seeded_dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When BRAGI_DATASET_QUERY_MEMORY_LIMIT is set to a unit-less value
    # (e.g. "512"), DuckDB rejects the SET call with a duckdb.Error.
    # The renderer must catch the resulting DatasetStorageError and bake
    # an error card; it must not propagate the exception out of render(),
    # which would abort the entire save.
    site, _ = seeded_dataset
    monkeypatch.setattr(settings, "dataset_query_memory_limit", "512")
    html = _render(md, "::: dataset slug=cpi q=by-quarter\n:::\n", site.id)
    assert "bragi-dataset--error" in html
    assert "Traceback" not in html


def test_two_directives_in_one_document(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    text = (
        "::: dataset slug=cpi q=by-quarter\n:::\n\n"
        '::: dataset slug=cpi sql="SELECT max(value) FROM cpi" format=scalar\n:::\n'
    )
    html = _render(md, text, site.id)
    assert html.count('<table class="bragi-dataset-table">') == 1
    assert html.count("bragi-dataset-scalar") == 1


def test_directive_inside_blockquote_does_not_swallow(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    text = "> ::: dataset slug=cpi q=by-quarter\n\nafter paragraph\n\n:::\n"
    html = _render(md, text, site.id)
    assert "after paragraph" in html


def test_inline_sql_with_embedded_equals(md, seeded_dataset) -> None:
    site, _ = seeded_dataset
    directive = (
        "::: dataset slug=cpi"
        " sql=\"SELECT value FROM cpi WHERE quarter='2025Q1'\""
        " format=scalar\n:::\n"
    )
    html = _render(md, directive, site.id)
    assert "102.5" in html
