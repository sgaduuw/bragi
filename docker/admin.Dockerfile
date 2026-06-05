# syntax=docker/dockerfile:1.7
# bragi-admin container. Consumes bragi-cms from PyPI; build-arg
# BRAGI_VERSION pins the wheel version at build time. See
# docker/delivery.Dockerfile for the delivery sibling; CMD is the
# only deliberate difference.

ARG BRAGI_VERSION
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# BRAGI_VERSION must be passed via --build-arg from the release.yml
# workflow. release.yml derives it from github.event.release.tag_name
# (stripping the leading v) so the image always ships the same wheel
# that was just published to PyPI.
ARG BRAGI_VERSION
RUN test -n "${BRAGI_VERSION}" \
    || (echo "BRAGI_VERSION build-arg is required" && exit 1)
RUN pip install --no-cache-dir "bragi-cms==${BRAGI_VERSION}"

# Task-runner sidecar reuses the admin image; ship the scheduler
# script alongside the app. (The delivery image doesn't need it.)
COPY docker/scheduler.sh /app/scheduler.sh
RUN chmod +x /app/scheduler.sh

# Drop root. UID pinned to 1000 to match the delivery image so the
# shared /data volume is writable from both regardless of base-image
# drift in the system-uid range. See pre-v1.27.1 Dockerfile for the
# full rationale; the userdrop block is unchanged.
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
