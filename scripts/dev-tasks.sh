#!/usr/bin/env bash
# Local-dev task-runner loop. Equivalent to docker/scheduler.sh
# but invoked from the in-repo Procfile supervisor
# (`scripts/run-procfile.py`) so `make dev` exercises the
# scheduled-publish / webmentions-send / activitypub-send /
# indexnow-send / embeds-rerender code paths against the local
# SQLite DB.
#
# Cadence is a deliberately tight 60s (vs the production
# 60s-to-1-week range) so a dev iterating on a related feature
# sees the queue drain promptly. The script is foreground; the
# supervisor forwards SIGTERM cleanly via the process group.
set -uo pipefail

SLEEP_BETWEEN_PASSES=${BRAGI_DEV_TASKS_SLEEP:-60}

log() {
    printf '[dev-tasks %s] %s\n' "$(date -Iseconds)" "$*"
}

run_cmd() {
    log "running: $*"
    if ! uv run bragi "$@"; then
        log "command failed (rc=$?); continuing"
    fi
}

log "dev-tasks loop started; cadence=${SLEEP_BETWEEN_PASSES}s"

while true; do
    run_cmd scheduled-publish
    run_cmd embeds rerender-pending
    run_cmd webmentions send-pending
    run_cmd activitypub send-pending
    run_cmd indexnow send-pending
    run_cmd media process-renditions
    sleep "$SLEEP_BETWEEN_PASSES"
done
