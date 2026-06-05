# syntax=docker/dockerfile:1.7
# bragi-delivery container. Consumes bragi-cms from PyPI; build-arg
# BRAGI_VERSION pins the wheel version at build time. See
# docker/admin.Dockerfile for the admin sibling; CMD is the only
# deliberate difference.

ARG BRAGI_VERSION
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

ARG BRAGI_VERSION
RUN test -n "${BRAGI_VERSION}" \
    || (echo "BRAGI_VERSION build-arg is required" && exit 1)
RUN pip install --no-cache-dir "bragi-cms==${BRAGI_VERSION}"

# Drop root (same as admin). Delivery doesn't ship scheduler.sh.
# Even though delivery is read-only at the schema level, a worker
# RCE under uid 0 still escapes into the /data volume with root
# privileges. The UID is pinned to 1000, identical to admin, so
# the shared /data volume is writable from both regardless of
# base-image drift in the system-uid range.
RUN useradd --system --create-home --shell /usr/sbin/nologin --uid 1000 bragi \
    && mkdir -p /data \
    && chown -R bragi:bragi /app /data
USER bragi

EXPOSE 8002
# `bragi-delivery` (the click script) calls `app.run(...)` and is
# the local-dev entrypoint. In the container, run gunicorn against
# the WSGI factory. Delivery is read-heavy; 4 workers default fine
# under SQLite WAL. `DELIVERY_WORKERS` env var lets ops dial it up
# or down. See the admin Dockerfile for the rationale on
# `sync` worker class and `--access-logfile -`. `--graceful-timeout
# 25` ensures in-flight requests get up to 25s to complete on
# SIGTERM before workers are killed; pair with compose.yml's
# `stop_grace_period: 30s`.
CMD ["sh", "-c", "exec gunicorn -w ${DELIVERY_WORKERS:-4} -b 0.0.0.0:8002 --timeout 30 --graceful-timeout 25 --access-logfile - 'bragi.apps.delivery:create_delivery_app()'"]
