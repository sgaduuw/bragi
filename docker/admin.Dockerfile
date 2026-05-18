# syntax=docker/dockerfile:1.7
# Multi-stage build for bragi-admin. Same shape as delivery.Dockerfile;
# CMD is the only deliberate difference.

FROM python:3.12-slim AS build

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VIRTUALENVS_CREATE=false

# Poetry 2.x split `poetry export` out into the poetry-plugin-export
# package; pin both so the build stays deterministic across upstream
# version bumps.
RUN pip install --no-cache-dir "poetry>=2.0,<3.0" "poetry-plugin-export>=1.8"

WORKDIR /app
COPY pyproject.toml poetry.lock* ./
RUN poetry export -f requirements.txt --without-hashes --only main > requirements.txt


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY --from=build /app/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY pyproject.toml README.md ./
# Task-runner sidecar reuses the admin image (it has the `cms`
# CLI registered); ship the scheduler script alongside the app.
COPY docker/scheduler.sh /app/scheduler.sh
RUN chmod +x /app/scheduler.sh

RUN pip install --no-deps -e .

# Drop root for the runtime. Adds a non-login system user with an
# explicit UID, gives it ownership of /app (the source tree) and
# /data (the volume mount point); the entrypoints inherit this
# UID via `USER bragi` below. Worker-RCE blast radius is then
# limited to the container fs and the volume, not uid 0 escapes
# into the volume host. The UID is pinned (`--uid 1000`) and
# identical between admin and delivery Dockerfiles so the shared
# /data volume is writable from both regardless of base-image
# drift in the system-uid range.
RUN useradd --system --create-home --shell /usr/sbin/nologin --uid 1000 bragi \
    && mkdir -p /data \
    && chown -R bragi:bragi /app /data
USER bragi

EXPOSE 8001
# `bragi-admin` (the click script) calls `app.run(...)` and is the
# local-dev entrypoint where Werkzeug's auto-reload is useful. In
# the container, run gunicorn against the WSGI factory instead.
# Workers default to 2 (admin is low-concurrency, one operator);
# `ADMIN_WORKERS` env var lets ops dial it up. `sync` worker class
# matches Flask + SQLAlchemy ORM; async would need monkey-patching
# or ASGI. `--access-logfile -` sends combined-log-format access
# lines to stdout so they land in the container log.
# `--graceful-timeout 25` gives an in-flight request up to 25s to
# return before the worker is force-killed on SIGTERM; this matters
# for outbound webmention POSTs (3s timeout each) and federation
# fetches. Pair with `compose.yml`'s `stop_grace_period: 30s`.
# Wrapped in `sh -c` so $ADMIN_WORKERS interpolates at start time;
# the bare exec form does no variable expansion.
CMD ["sh", "-c", "exec gunicorn -w ${ADMIN_WORKERS:-2} -b 0.0.0.0:8001 --timeout 30 --graceful-timeout 25 --access-logfile - 'bragi.apps.admin:create_admin_app()'"]
