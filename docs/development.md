# Development

[Back to README](../README.md)

Use Python 3.14+, uv, and make. Follow the [local setup](../README.md#try-it-locally)
to install dependencies and start the apps.

## Checks

Run all four checks before submitting a change:

```sh
uv run ruff check src/ tests/ alembic/
uv run ruff format --check src/ tests/ alembic/
uv run mypy src/
uv run pytest
```

`make lint`, `make typecheck`, and `make test` run the same checks.
`make fmt` applies Ruff fixes and formatting.

## How the application fits together

Bragi uses Flask, SQLAlchemy, Pydantic Settings, and pluggy. Admin and
public delivery run as separate processes sharing a SQLite database and
attachment storage. The datasets plugin also uses DuckDB.

Markdown is the stored source. Rendering uses markdown-it-py and Pygments;
the admin editor uses TipTap. htmx enhances server-rendered pages.

| Path | Purpose |
| --- | --- |
| `src/bragi/apps/` | Admin and delivery app factories |
| `src/bragi/core/` | Shared models, rendering, storage, and middleware |
| `src/bragi/contrib/` | Built-in features and themes, registered as plugins |
| `src/bragi/api.py` | Public API for plugin authors |
| `src/bragi/settings.py` | Configuration fields and defaults |
| `src/bragi/cli.py` | Top-level CLI and maintenance commands |
| `src/bragi/alembic/` | Database migrations bundled in the package |
| `tests/` | Unit, plugin, and integration checks |
| `docker/` | Container builds and background task runner |

Plugins register through the `bragi.plugins` entry-point group. Built-in
plugins use the same mechanism as third-party packages. Import the public
plugin API from `bragi.api`; keep shared models in `bragi.core.models`.
See [Building a theme](themes.md) for a complete plugin example and
[AGENTS.md](../AGENTS.md) for repository conventions and database invariants.

## Database migrations

```sh
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "describe change"
```

Review generated migrations before applying them. `make dev` applies
pending migrations automatically before starting the local processes.

## Branches and releases

Start feature and documentation branches from `develop` and target PRs there.
`main` contains released code. Releases use `release/X.Y.Z`; urgent fixes use
`hotfix/X.Y.Z` from `main`.

The version lives in `pyproject.toml` and is exposed as `bragi.__version__`.
Update it together with `uv.lock` on the release branch, not a feature branch.

GitHub Releases publish the `bragi-cms` package to PyPI, then build the
`bragi-admin` and `bragi-delivery` images from that wheel. Images are published
to GHCR for `linux/amd64` and `linux/arm64` with `vX.Y.Z` tags.
The Python import remains `import bragi`.

### Browser recovery check

After changes to the post/page editing flow, run the browser check alongside the
normal test suite. It uses a local HTTP fixture and the real editor assets;
TipTap and the resume date picker require access to their existing esm.sh imports.

```sh
uv run --with playwright playwright install chromium
uv run --with playwright python tests/browser/check_editor_recovery.py
```
