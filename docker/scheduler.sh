#!/bin/sh
# Task-runner sidecar loop. Runs alongside the admin/delivery web
# containers, sharing the /data volume. Owns alembic migration
# (single source of DDL truth), then enters a sleeper loop dispatching
# Flask CLI commands at configured cadences.
#
# Each subcommand is invoked via `flask --app 'bragi.apps.admin:create_admin_app' cms ...`,
# opens its own DB session, and exits cleanly so a crash in one task
# doesn't take the loop down. The explicit `module:factory` form is
# required: Flask's autodiscovery only looks for factories named
# `create_app` / `make_app`, not `create_admin_app`, so the bare
# `--app bragi.apps.admin` form silently degrades to "no app found"
# and every `cms` invocation exits rc=2.
#
# Cadences (env-overridable, all in seconds):
#   SCHEDULED_PUBLISH_EVERY  default 60      ; flip due drafts to published
#   EMBEDS_RERENDER_EVERY    default 600     ; retry pending embed cards
#   WEBMENTIONS_SEND_EVERY   default 300     ; ship pending outbound webmentions
#   ACTIVITYPUB_SEND_EVERY   default 60      ; sign + deliver pending AP outbox rows
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
EMBEDS_RERENDER_EVERY=${EMBEDS_RERENDER_EVERY:-600}
WEBMENTIONS_SEND_EVERY=${WEBMENTIONS_SEND_EVERY:-300}
ACTIVITYPUB_SEND_EVERY=${ACTIVITYPUB_SEND_EVERY:-60}
ANALYZE_EVERY=${ANALYZE_EVERY:-86400}
VACUUM_EVERY=${VACUUM_EVERY:-604800}

log() { echo "[scheduler $(date -Iseconds)] $*"; }

# Track the active child PID so SIGTERM/SIGINT can forward to it
# before exiting the loop. Without this, `docker compose stop`
# sends SIGTERM and the outer `sleep` runs to completion, then
# SIGKILL fires; a multi-minute `cms db vacuum` or AP-send-pending
# run mid-tick loses work. The trap forwards the signal to the
# child and exits the loop. `SLEEP_PID` covers the cadence-spacing
# `sleep` too: POSIX `/bin/sh` (dash) does NOT interrupt a
# foreground `sleep` for a trapped signal, so without backgrounding
# the sleep, SIGTERM during a sleep waits up to the full sleep
# duration before the trap fires. We background the sleep, store
# its PID, and `wait` on it; the trap can then kill the sleep PID
# directly.
CHILD_PID=
SLEEP_PID=
shutting_down=0

forward_signal() {
    shutting_down=1
    if [ -n "$CHILD_PID" ]; then
        log "shutdown: forwarding signal to child pid=$CHILD_PID"
        kill -TERM "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
    fi
    if [ -n "$SLEEP_PID" ]; then
        kill -TERM "$SLEEP_PID" 2>/dev/null || true
        wait "$SLEEP_PID" 2>/dev/null || true
    fi
    log "shutdown: exiting"
    exit 0
}

trap forward_signal TERM INT

run() {
    label=$1; shift
    "$@" &
    CHILD_PID=$!
    wait "$CHILD_PID"
    rc=$?
    CHILD_PID=
    if [ $rc -ne 0 ] && [ $shutting_down -eq 0 ]; then
        log "$label: failed rc=$rc"
    fi
}

# Interruptible sleep helper: backgrounds the sleep so the SIGTERM
# trap can fire promptly instead of waiting up to the full sleep
# duration.
sleep_interruptible() {
    sleep "$1" &
    SLEEP_PID=$!
    wait "$SLEEP_PID" 2>/dev/null || true
    SLEEP_PID=
}

# Cap alembic restart-loop on a persistently-failing migration.
# `compose.yml` switches this service to `restart: on-failure`, so
# a non-zero exit triggers a restart; after `ALEMBIC_MAX_ATTEMPTS`
# the script exits 0 with a loud log line, which `on-failure`
# treats as terminal (no further restart). Without this the
# sidecar bounces forever and the depending web services stay
# stuck in "created" state with no clear failure signal to ops.
ALEMBIC_MAX_ATTEMPTS=${ALEMBIC_MAX_ATTEMPTS:-5}
ALEMBIC_RETRY_DELAY=${ALEMBIC_RETRY_DELAY:-15}
attempt=1
while true; do
    log "alembic: attempt $attempt of $ALEMBIC_MAX_ATTEMPTS"
    if alembic upgrade head; then
        log "alembic: ok (attempt $attempt)"
        # Healthcheck sentinel: the web containers' depends_on uses
        # condition: service_healthy and a `test -f /data/.migrated`
        # test, so gunicorn waits for this file before it starts.
        touch /data/.migrated
        break
    fi
    rc=$?
    if [ "$attempt" -ge "$ALEMBIC_MAX_ATTEMPTS" ]; then
        log "alembic: failed $ALEMBIC_MAX_ATTEMPTS times (last rc=$rc); giving up. The web services will NOT come up. Check the migration error above, fix, then run: docker compose up -d bragi-tasks"
        # Exit 0 so `restart: on-failure` stops trying. Status shows
        # as `Exited (0)` and operators see this log line above it.
        exit 0
    fi
    log "alembic: attempt $attempt failed (rc=$rc); retrying in ${ALEMBIC_RETRY_DELAY}s"
    sleep_interruptible "$ALEMBIC_RETRY_DELAY"
    attempt=$((attempt + 1))
done

now=$(date +%s)
last_scheduled_publish=$now
last_embeds_rerender=$now
last_webmentions_send=$now
last_activitypub_send=$now
last_analyze=$now
last_vacuum=$now

while true; do
    now=$(date +%s)

    if [ $((now - last_scheduled_publish)) -ge "$SCHEDULED_PUBLISH_EVERY" ]; then
        run "scheduled-publish" flask --app 'bragi.apps.admin:create_admin_app' cms scheduled-publish
        last_scheduled_publish=$(date +%s)
    fi

    if [ $((now - last_embeds_rerender)) -ge "$EMBEDS_RERENDER_EVERY" ]; then
        run "embeds-rerender" flask --app 'bragi.apps.admin:create_admin_app' cms embeds rerender-pending
        last_embeds_rerender=$(date +%s)
    fi

    if [ $((now - last_webmentions_send)) -ge "$WEBMENTIONS_SEND_EVERY" ]; then
        run "webmentions-send" flask --app 'bragi.apps.admin:create_admin_app' cms webmentions send-pending
        last_webmentions_send=$(date +%s)
    fi

    if [ $((now - last_activitypub_send)) -ge "$ACTIVITYPUB_SEND_EVERY" ]; then
        run "activitypub-send" flask --app 'bragi.apps.admin:create_admin_app' cms activitypub send-pending
        last_activitypub_send=$(date +%s)
    fi

    if [ $((now - last_analyze)) -ge "$ANALYZE_EVERY" ]; then
        run "analyze" flask --app 'bragi.apps.admin:create_admin_app' cms db analyze
        last_analyze=$(date +%s)
    fi

    if [ $((now - last_vacuum)) -ge "$VACUUM_EVERY" ]; then
        run "vacuum" flask --app 'bragi.apps.admin:create_admin_app' cms db vacuum
        last_vacuum=$(date +%s)
    fi

    sleep_interruptible 10
done
