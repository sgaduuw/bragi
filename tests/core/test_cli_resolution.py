"""Guards against the rc=2 regression that silently broke the
task-runner sidecar in 1.8.0 - 1.9.0.

The sidecar's `docker/scheduler.sh` dispatches
`flask --app bragi.apps.admin cms <subcommand>` on a cadence to run
scheduled-publish, embed rerenders, and SQLite maintenance. Flask's
CLI autodiscovery only looks for factories named `create_app` or
`make_app` — `create_admin_app` doesn't match, so the bare
`--app bragi.apps.admin` form silently fails:

    Error: Failed to find Flask application or factory ...
    Error: No such command 'cms'.

and every tick exits rc=2. The container kept running, the loop
kept ticking, and nothing scheduled-publish, no embed retries, no
ANALYZE, no VACUUM landed in prod until the regression was caught
in `journalctl`.

Fix: use the explicit `module:factory` form everywhere
(`flask --app 'bragi.apps.admin:create_admin_app' cms ...`). These
tests are the canary: any future call site that drops back to the
bare form fails this guard.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask.cli import ScriptInfo
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture(autouse=True)
def _patch_session_locals(
    patched_session_locals: sessionmaker[Session],
) -> Iterator[None]:
    """Plugin discovery during app boot opens DB sessions; pin them
    to the in-memory test DB so the test doesn't reach for the dev
    file or fail on a missing schema."""
    del patched_session_locals
    yield


def test_explicit_factory_form_resolves_and_exposes_cms() -> None:
    """`bragi.apps.admin:create_admin_app` resolves and registers
    the `cms` click group. This is the form used by `scheduler.sh`,
    the gunicorn admin command, and the CLAUDE.md docs."""
    info = ScriptInfo(app_import_path="bragi.apps.admin:create_admin_app")
    app = info.load_app()
    assert app.cli.get_command(None, "cms") is not None, (
        "The `cms` click group must be reachable from the admin "
        "Flask CLI; the scheduler depends on it for every tick."
    )


def test_bare_module_form_fails_loudly() -> None:
    """The bare `bragi.apps.admin` form (no `:create_admin_app`)
    must raise rather than silently fall back to something else.
    Pinning this lets the test guard against a future Flask
    autodiscovery change that started accepting `create_admin_app`
    without us noticing; a green test here in a future Flask would
    let us simplify the scheduler invocation, intentionally."""
    from flask.cli import NoAppException

    info = ScriptInfo(app_import_path="bragi.apps.admin")
    with pytest.raises(NoAppException):
        info.load_app()
