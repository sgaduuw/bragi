# syntax=docker/dockerfile:1.7
# Multi-stage build for bragi-delivery. Same shape as admin.Dockerfile;
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

RUN pip install --no-deps -e .

EXPOSE 8002
# `bragi-delivery` (the click script) calls `app.run(...)` and is
# the local-dev entrypoint. In the container, run gunicorn against
# the WSGI factory. Delivery is read-heavy; 4 workers default fine
# under SQLite WAL. `DELIVERY_WORKERS` env var lets ops dial it up
# or down. See the admin Dockerfile for the rationale on
# `sync` worker class and `--access-logfile -`.
CMD ["sh", "-c", "exec gunicorn -w ${DELIVERY_WORKERS:-4} -b 0.0.0.0:8002 --timeout 30 --access-logfile - 'bragi.apps.delivery:create_delivery_app()'"]
