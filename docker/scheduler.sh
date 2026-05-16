#!/bin/sh
# Task-runner sidecar loop. Runs alongside the admin/delivery web
# containers, sharing the /data volume. Owns alembic migration
# (single source of DDL truth), then enters a sleeper loop dispatching
# Flask CLI commands at configured cadences.
#
# Each subcommand is invoked via `flask --app bragi.apps.admin cms ...`,
# opens its own DB session, and exits cleanly so a crash in one task
# doesn't take the loop down.
#
# Cadences (env-overridable, all in seconds):
#   SCHEDULED_PUBLISH_EVERY  default 60      ; flip due drafts to published
#   ANALYZE_EVERY            default 86400   ; refresh sqlite_stat1 (daily)
#   VACUUM_EVERY             default 604800  ; compact DB + collapse WAL (weekly)
#
# Timing is relative to container start, not wall-clock; fine for a
# personal-CMS workload. Sleeps 10s between ticks, tighter than the
# fastest cadence so the loop is responsive after a task that ran
# longer than its slot.
#
# Web containers gate their start on this script's healthcheck (see
# compose.yml). The sentinel `/data/.migrated` is touched once the
# initial `alembic upgrade head` succeeds.

set -u

SCHEDULED_PUBLISH_EVERY=${SCHEDULED_PUBLISH_EVERY:-60}
ANALYZE_EVERY=${ANALYZE_EVERY:-86400}
VACUUM_EVERY=${VACUUM_EVERY:-604800}

log() { echo "[scheduler $(date -Iseconds)] $*"; }

run() {
    label=$1; shift
    if "$@"; then
        :
    else
        rc=$?
        log "$label: failed rc=$rc"
    fi
}

log "alembic: start"
if alembic upgrade head; then
    log "alembic: ok"
    # Healthcheck sentinel: the web containers' depends_on uses
    # condition: service_healthy and a `test -f /data/.migrated` test,
    # so gunicorn waits for this file before it starts serving.
    touch /data/.migrated
else
    log "alembic: failed (rc=$?), refusing to start sidecar loop"
    exit 1
fi

now=$(date +%s)
last_scheduled_publish=$now
last_analyze=$now
last_vacuum=$now

while true; do
    now=$(date +%s)

    if [ $((now - last_scheduled_publish)) -ge "$SCHEDULED_PUBLISH_EVERY" ]; then
        run "scheduled-publish" flask --app bragi.apps.admin cms scheduled-publish
        last_scheduled_publish=$(date +%s)
    fi

    if [ $((now - last_analyze)) -ge "$ANALYZE_EVERY" ]; then
        run "analyze" flask --app bragi.apps.admin cms db analyze
        last_analyze=$(date +%s)
    fi

    if [ $((now - last_vacuum)) -ge "$VACUUM_EVERY" ]; then
        run "vacuum" flask --app bragi.apps.admin cms db vacuum
        last_vacuum=$(date +%s)
    fi

    sleep 10
done
