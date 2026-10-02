"""Bounded 404 recording on real, migrated SQLite storage (#519)."""

from __future__ import annotations

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from flask import Flask
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from bragi.contrib.notfound import plugin
from bragi.core.models.not_found import NotFound, NotFoundStatus
from bragi.core.models.site import Site
from bragi.settings import Settings, settings
from tests.integration.test_notfound import (
    HOST_A,
    HOST_B,
    _rows,
    _seed,
)


@pytest.fixture
def delivery_app_file_db(patched_file_session_locals: sessionmaker[Session]) -> Flask:
    from bragi.apps.delivery import create_delivery_app

    return create_delivery_app()


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    monkeypatch.setitem(settings.__dict__, "notfound_max_rows", 3)
    monkeypatch.setitem(settings.__dict__, "notfound_records_per_minute", 2)
    clock = [120.0]
    monkeypatch.setattr(plugin, "monotonic", lambda: clock[0], raising=False)
    return clock


def test_distinct_paths_stop_at_total_capacity(
    patched_file_session_locals: sessionmaker[Session],
    limits: list[float],
) -> None:
    _seed(patched_file_session_locals)
    with patched_file_session_locals() as db:
        site_id = db.scalar(select(Site.id).where(Site.hostname == HOST_A))
    assert site_id is not None
    for path in ("/open/", "/dismissed/", "/ignored/"):
        plugin._record(site_id, path, None)
    with patched_file_session_locals() as db:
        db.scalar(
            select(NotFound).where(NotFound.path == "/dismissed/")
        ).status = NotFoundStatus.DISMISSED
        db.scalar(
            select(NotFound).where(NotFound.path == "/ignored/")
        ).status = NotFoundStatus.IGNORED
        db.commit()
    for i in range(20):
        plugin._record(site_id, f"/novel-{i}/", None)
    assert {row.path for row in _rows(patched_file_session_locals)} == {
        "/open/",
        "/dismissed/",
        "/ignored/",
    }
    # Existing rows can update/reopen at capacity, but ignored remains permanent.
    for path in ("/open/", "/dismissed/", "/ignored/"):
        plugin._record(site_id, path, "https://example.com/new")
    by_path = {row.path: row for row in _rows(patched_file_session_locals)}
    assert [
        (by_path[p].count, by_path[p].status)
        for p in (
            "/open/",
            "/dismissed/",
            "/ignored/",
        )
    ] == [(2, "open"), (2, "open"), (1, "ignored")]
    assert by_path["/open/"].last_referrer == "https://example.com/new"


def test_over_capacity_data_preserved_and_other_site_can_record(
    patched_file_session_locals: sessionmaker[Session],
    limits: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(patched_file_session_locals)
    with patched_file_session_locals() as db:
        a = db.scalar(select(Site.id).where(Site.hostname == HOST_A))
        b = db.scalar(select(Site.id).where(Site.hostname == HOST_B))
    assert a and b
    for i in range(3):
        plugin._record(a, f"/old-{i}/", None)
    monkeypatch.setitem(settings.__dict__, "notfound_max_rows", 1)
    plugin._record(a, "/new/", None)
    plugin._record(a, "/old-0/", None)
    plugin._record(b, "/new/", None)
    rows = _rows(patched_file_session_locals)
    assert len(rows) == 4
    assert len([r for r in rows if r.site_id == a]) == 3
    assert next(r.count for r in rows if r.path == "/old-0/") == 2


@pytest.mark.parametrize("repeat", [False, True])
def test_rate_gate_skips_recorder_and_recovers_next_window(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    limits: list[float],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    repeat: bool,
) -> None:
    _seed(file_db_session_factory)
    client = delivery_app_file_db.test_client()
    for i in range(2):
        assert (
            client.get("/dead/" if repeat else f"/dead-{i}/", headers={"Host": HOST_A}).status_code
            == 404
        )
    calls = []
    original = plugin.SessionLocal

    def session_spy():
        calls.append(1)
        return original()

    monkeypatch.setattr(plugin, "SessionLocal", session_spy)
    with caplog.at_level(logging.WARNING, logger=plugin.__name__):
        for i in range(20):
            assert (
                client.get(
                    "/dead/" if repeat else f"/overflow-{i}/", headers={"Host": HOST_A}
                ).status_code
                == 404
            )
    assert calls == []  # The gate must run before even opening a recorder session.
    warnings = [r for r in caplog.records if r.name == plugin.__name__]
    assert len(warnings) == 1
    assert sum(r.count for r in _rows(file_db_session_factory)) == 2
    limits[0] = 180.0
    assert client.get("/dead/", headers={"Host": HOST_A}).status_code == 404
    assert len(calls) == 1
    assert sum(r.count for r in _rows(file_db_session_factory)) == 3


def test_rate_state_is_site_and_app_scoped(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    limits: list[float],
) -> None:
    from bragi.apps.delivery import create_delivery_app

    _seed(file_db_session_factory)
    for host in (HOST_A, HOST_B):
        for _ in range(4):
            assert (
                delivery_app_file_db.test_client()
                .get("/repeat/", headers={"Host": host})
                .status_code
                == 404
            )
    assert sorted(r.count for r in _rows(file_db_session_factory)) == [2, 2]
    # A newly created delivery worker has a fresh budget, sharing the same rows.
    fresh = create_delivery_app().test_client()
    assert fresh.get("/repeat/", headers={"Host": HOST_A}).status_code == 404
    assert sorted(r.count for r in _rows(file_db_session_factory)) == [2, 3]


def test_zero_rate_disables_recording(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    limits: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(file_db_session_factory)
    monkeypatch.setitem(settings.__dict__, "notfound_records_per_minute", 0)
    assert (
        delivery_app_file_db.test_client().get("/off/", headers={"Host": HOST_A}).status_code == 404
    )
    assert _rows(file_db_session_factory) == []


def test_concurrent_workers_cannot_insert_past_last_slot(
    patched_file_session_locals: sessionmaker[Session],
    limits: list[float],
) -> None:
    _seed(patched_file_session_locals)
    with patched_file_session_locals() as db:
        site_id = db.scalar(select(Site.id).where(Site.hostname == HOST_A))
    assert site_id
    plugin._record(site_id, "/first/", None)
    plugin._record(site_id, "/second/", None)
    barrier = Barrier(2)

    def record(i: int) -> None:
        barrier.wait(timeout=5)
        plugin._record(site_id, f"/racer-{i}/", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(record, range(2)))
    rows = _rows(patched_file_session_locals)
    assert len(rows) == 3
    assert len([r for r in rows if r.path.startswith("/racer-")]) == 1


def test_busy_recorder_returns_404_without_retry_and_restores_timeout(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    file_db_engine: Engine,
    limits: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(file_db_session_factory)
    # Shorten the baseline's 10s timeout so its incorrect retries fail quickly.
    with file_db_engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA busy_timeout=200")
    writer = sqlite3.connect(file_db_engine.url.database, timeout=0)
    writer.execute("BEGIN IMMEDIATE")
    seen = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO NOT_FOUNDS"):
            seen.append(conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one())

    event.listen(file_db_engine, "before_cursor_execute", record_sql)
    try:
        started = time.perf_counter()
        response = delivery_app_file_db.test_client().get("/busy/", headers={"Host": HOST_A})
        elapsed = time.perf_counter() - started
        assert response.status_code == 404
        assert seen == [50]
        assert elapsed < 1.0
    finally:
        writer.rollback()
        writer.close()
        event.remove(file_db_engine, "before_cursor_execute", record_sql)
    with file_db_engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 200
    assert _rows(file_db_session_factory) == []
    # Recovery after the writer releases the lock uses a normal second attempt.
    assert (
        delivery_app_file_db.test_client().get("/after/", headers={"Host": HOST_A}).status_code
        == 404
    )
    assert [r.path for r in _rows(file_db_session_factory)] == ["/after/"]
    with file_db_engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 200


@pytest.mark.parametrize(
    "field,value", [("notfound_max_rows", 0), ("notfound_records_per_minute", -1)]
)
def test_invalid_recording_limits_are_rejected(field: str, value: int) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "headers", [{}, {"HX-Request": "true"}, {"HX-Request": "true", "HX-Boosted": "true"}]
)
def test_capacity_visible_in_full_partial_and_boosted_admin(
    admin_app_file_db: Flask,
    patched_file_session_locals: sessionmaker[Session],
    limits: list[float],
    headers: dict[str, str],
) -> None:
    from bs4 import BeautifulSoup

    from tests.integration.test_notfound import _login

    _seed(patched_file_session_locals)
    with patched_file_session_locals() as db:
        site = db.scalar(select(Site).where(Site.hostname == HOST_A))
        site_id = site.id
    for path in ("/visible/", "/dismissed/", "/ignored/"):
        plugin._record(site_id, path, None)
    with patched_file_session_locals() as db:
        for path, status in (("/dismissed/", "dismissed"), ("/ignored/", "ignored")):
            db.scalar(select(NotFound).where(NotFound.path == path)).status = status
        db.commit()
    client = admin_app_file_db.test_client()
    _login(client)
    response = client.get("/admin/sites/blog/not-found/", headers={"Host": HOST_A, **headers})
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    capacity = soup.select_one("#notfound-capacity")
    assert capacity is not None
    assert capacity.get_text(" ", strip=True) == "Retained records: 3 / 3"
    assert soup.select_one("#notfound-capacity-warning") is not None
    assert [h.get_text(strip=True) for h in soup.select("th")][2] == "Recorded hits"
    other = client.get("/admin/sites/other/not-found/", headers={"Host": HOST_A, **headers})
    other_soup = BeautifulSoup(other.data, "html.parser")
    assert (
        other_soup.select_one("#notfound-capacity").get_text(" ", strip=True)
        == "Retained records: 0 / 3"
    )
    assert other_soup.select_one("#notfound-capacity-warning") is None


def test_capacity_notice_uses_existing_plugin_surface(
    admin_app_file_db: Flask,
    patched_file_session_locals: sessionmaker[Session],
    limits: list[float],
) -> None:
    _seed(patched_file_session_locals)
    with patched_file_session_locals() as db:
        a = db.scalar(select(Site).where(Site.hostname == HOST_A))
        b = db.scalar(select(Site).where(Site.hostname == HOST_B))
    for i in range(3):
        plugin._record(a.id, f"/dead-{i}/", None)
    pm = admin_app_file_db.extensions["plugin_manager"]
    with admin_app_file_db.app_context():
        notices = [n for group in pm.hook.admin_notices(site=a) for n in group]
    matching = [n for n in notices if n.key == "notfound.capacity"]
    assert len(matching) == 1
    assert matching[0].severity == "warn"
    assert matching[0].dismissible is False
    assert matching[0].cta_endpoint == "notfound_admin.list_notfound"
    assert matching[0].cta_endpoint_kwargs == {"site_slug": "blog"}
    with admin_app_file_db.app_context():
        assert not [
            n
            for group in pm.hook.admin_notices(site=b)
            for n in group
            if n.key == "notfound.capacity"
        ]


def test_prune_only_dismissed_or_exact_redirect_covered_and_preserves_ignored(
    patched_file_session_locals: sessionmaker[Session],
    limits: list[float],
) -> None:
    from click.testing import CliRunner

    from bragi.cli import bragi
    from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
    from bragi.core.time import naive_utcnow

    _seed(patched_file_session_locals)
    with patched_file_session_locals() as db:
        a = db.scalar(select(Site.id).where(Site.hostname == HOST_A))
        b = db.scalar(select(Site.id).where(Site.hostname == HOST_B))
        for site_id, path, status in (
            (a, "/dismissed/", "dismissed"),
            (a, "/covered/", "open"),
            (a, "/ignored/", "ignored"),
            (a, "/covered-ignored/", "ignored"),
            (a, "/open/", "open"),
            (a, "/inactive/", "open"),
            (a, "/prefix/", "open"),
            (a, "/cross-site/", "open"),
            (b, "/dismissed/", "dismissed"),
        ):
            db.add(
                NotFound(
                    site_id=site_id,
                    path=path,
                    status=status,
                    count=1,
                    first_seen=naive_utcnow(),
                    last_seen=naive_utcnow(),
                )
            )
        for site_id, path, match, active in (
            (a, "/covered/", MatchType.EXACT, True),
            (a, "/covered-ignored/", MatchType.EXACT, True),
            (a, "/inactive/", MatchType.EXACT, False),
            (a, "/prefix/", MatchType.PREFIX, True),
            (b, "/cross-site/", MatchType.EXACT, True),
        ):
            db.add(
                Redirect(
                    site_id=site_id,
                    source_path=path,
                    target="/new/",
                    match_type=match,
                    active=active,
                    source=RedirectSource.MANUAL,
                )
            )
        db.commit()
    before = {(r.site_id, r.path) for r in _rows(patched_file_session_locals)}
    assert len(before) == 9
    runner = CliRunner()
    result = runner.invoke(bragi, ["notfound", "prune", "--site", "blog", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Would prune 2 records." in result.output.splitlines()
    assert {(r.site_id, r.path) for r in _rows(patched_file_session_locals)} == before
    result = runner.invoke(bragi, ["notfound", "prune", "--site", "blog"])
    assert result.exit_code == 0, result.output
    assert "Pruned 2 records." in result.output.splitlines()
    expected = before - {(a, "/dismissed/"), (a, "/covered/")}
    assert {(r.site_id, r.path) for r in _rows(patched_file_session_locals)} == expected
    for args in (["notfound", "prune"], ["notfound", "prune", "--site", "absent"]):
        result = runner.invoke(bragi, args)
        assert result.exit_code == 2, result.output
        assert {(r.site_id, r.path) for r in _rows(patched_file_session_locals)} == expected


def test_threads_share_one_recording_budget(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    limits: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(file_db_session_factory)
    barrier = Barrier(8)
    calls = []
    original = plugin._record

    def record_spy(site_id, path, referrer):
        calls.append((site_id, path))
        return original(site_id, path, referrer)

    monkeypatch.setattr(plugin, "_record", record_spy)

    def request(i):
        barrier.wait(timeout=5)
        return (
            delivery_app_file_db.test_client()
            .get(f"/thread-{i}/", headers={"Host": HOST_A})
            .status_code
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(request, range(8))) == [404] * 8
    assert len(calls) == 2
    assert len({site_id for site_id, path in calls}) == 1


def test_ineligible_requests_do_not_spend_the_recording_budget(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    limits: list[float],
) -> None:
    _seed(file_db_session_factory)
    client = delivery_app_file_db.test_client()
    for method, path, host, expected_status in (
        ("GET", "/x.php", HOST_A, 404),
        ("HEAD", "/missing/", HOST_A, 404),
        ("POST", "/missing/", HOST_A, 405),
        ("GET", "/missing/", "unknown.example.com", 404),
        ("GET", "/" + "x" * 1025, HOST_A, 404),
    ):
        assert (
            client.open(path, method=method, headers={"Host": host}).status_code == expected_status
        )
    for path in ("/.well-known/missing", "/ordinary/"):
        assert client.get(path, headers={"Host": HOST_A}).status_code == 404
    assert {r.path for r in _rows(file_db_session_factory)} == {
        "/.well-known/missing",
        "/ordinary/",
    }


def test_failures_use_attempt_budget_and_log_once_per_window(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    limits: list[float],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(file_db_session_factory)
    calls = []

    def failed_write(*args):
        calls.append(1)
        raise RuntimeError("failure containing attacker-controlled input")

    monkeypatch.setattr(plugin, "_record", failed_write)
    client = delivery_app_file_db.test_client()
    for window in (120.0, 180.0):
        limits[0] = window
        for _ in range(4):
            assert client.get("/secret-path/", headers={"Host": HOST_A}).status_code == 404
    assert len(calls) == 4
    warnings = [r for r in caplog.records if r.name == plugin.__name__]
    assert len(warnings) == 2
    assert all("secret-path" not in r.getMessage() and r.exc_info is None for r in warnings)


def test_site_alias_cannot_get_an_extra_recording_budget(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    limits: list[float],
) -> None:
    from bragi.core.models.site_alias import SiteAlias

    _seed(file_db_session_factory)
    with file_db_session_factory() as db:
        site_id = db.scalar(select(Site.id).where(Site.hostname == HOST_A))
        db.add(SiteAlias(site_id=site_id, hostname="alias.example.com"))
        db.commit()
    client = delivery_app_file_db.test_client()
    for host in (HOST_A, "alias.example.com", HOST_A, "alias.example.com"):
        assert client.get("/repeat/", headers={"Host": host}).status_code == 404
    assert [(r.path, r.count) for r in _rows(file_db_session_factory)] == [("/repeat/", 2)]
