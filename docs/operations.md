# Deployment and operations

[Back to README](../README.md)

## Docker Compose

The repo ships an example [compose.yml](../compose.yml) that pulls
the published images from GHCR. The tag is parameterised via
`BRAGI_TAG` (default `latest`); pin to a specific release in
production. Put the selected `BRAGI_TAG` and a persistent `BRAGI_SECRET_KEY`
in a private `.env` file. Generate the secret once with `openssl rand -hex 32`
and keep it across restarts and upgrades. Do not commit this file.

Start the services:

```sh
# Save BRAGI_TAG and a generated BRAGI_SECRET_KEY in .env first.
docker compose up -d
```

A `bragi-tasks` sidecar owns `bragi db upgrade` on start
(touching `/data/.migrated` once the schema is current), then
enters a sleeper loop that dispatches periodic CMS commands:
`scheduled-publish` (flips drafts whose `scheduled_for` has
elapsed), `embeds rerender-pending`, `webmentions send-pending`,
`activitypub send-pending`, `db analyze` (daily), and `db vacuum`
(weekly). The admin and delivery services gate their start on
the sidecar's healthcheck, which checks for `/data/.migrated`.
That marker persists across deployments, so it does not prove that an
upgrade's migrations have completed. Each web container also exposes a
`/healthz` endpoint that does a `SELECT 1` round-trip; the
Compose health checks report their status. The restart policy restarts
exited containers; an unhealthy status alone does not restart a container.

The shared `bragi-data` volume backs `/data/bragi.db`, `/data/uploads/` (attachments),
and the `/data/.migrated` sentinel; back it up. Ports bind to
`127.0.0.1` only; front the apps with a reverse proxy
(Caddy / nginx / Traefik) for TLS and hostname routing.

## Upgrades

Back up the deployment before changing the pinned image release. Stop the
web services and background tasks, then run migrations with the new admin
image before starting the services again. Verify that `bragi db upgrade`
exits successfully and review its output. If it fails, keep the services
stopped while you investigate.

Do not use an existing `/data/.migrated` file or a successful `/healthz`
response as proof that the new schema is ready: neither checks the
migration revision.

## Create your first account and site

Once the services are running, create an administrator and a site. Replace
these example addresses and names with your own:

```sh
docker compose exec admin bragi user create --email you@example.com \
  --display-name "Your name" --superuser
docker compose exec admin bragi site create --slug blog \
  --hostname blog.example.com --title "My blog" \
  --canonical-url https://blog.example.com --owner you@example.com
```

The first command prints a generated password. Sign in on your admin
hostname and change the password when prompted. Route the admin hostname
to port 8001 and the public site's hostname to port 8002 through your TLS
reverse proxy. In the admin, create pages and configure a post-index page
before publishing posts.

## Proxy and security settings

`BRAGI_ENV=production` (set on both web services in the example
compose) tells the app it's running in production. When set,
booting with the bundled dev `SECRET_KEY` is fatal rather than
just logging a warning, so a misconfigured `BRAGI_SECRET_KEY`
fails loud instead of running with a predictable signing key.
Leave unset for local dev.

`BRAGI_TRUSTED_PROXY_HOPS` (default 0; the example compose sets
it to 1) tells the apps how many trusted reverse-proxy hops sit
in front of them. When > 0, both `create_admin_app` and
`create_delivery_app` wrap the WSGI callable in
`werkzeug.middleware.proxy_fix.ProxyFix(x_for, x_proto, x_host)`
with that hop count. Without it, three breakages manifest on a
fresh prod deploy: (a) `url_for(..., _external=True)` for the
GitHub OAuth `redirect_uri` emits `http://...` and GitHub
rejects the callback; (b) every `AuditLog.ip` and `Session.ip`
row records the reverse proxy's IP, hiding real client IPs;
(c) per-IP analytics groups every visit under the proxy.
**Never set this higher than the actual reverse-proxy depth**:
each unit of trust extends the `X-Forwarded-*` spoofability
boundary one hop outward.

## Container runtime

Container runtime hardening already in the published images:
both `admin` and `delivery` run as a non-root `bragi` user
(`--uid 1000`, pinned identically across the two so the shared
`/data` volume is writable from both); gunicorn ships with
`--graceful-timeout 25` paired with `stop_grace_period: 30s`
on the compose services so an in-flight outbound POST
(webmention sender, AP delivery) has up to 25 s to return on
`docker compose stop` before SIGKILL fires; the `bragi-tasks`
sidecar retries `bragi db upgrade` with backoff
(`ALEMBIC_MAX_ATTEMPTS=5`, `ALEMBIC_RETRY_DELAY=15`, in seconds) and exits
0 after exhausting attempts so a broken migration shows as a
clean `Exited (0)` rather than livelocking the deploy.

## Request limits and login protection

`BRAGI_MAX_REQUEST_BYTES` (default 1 MiB) caps the request body
size to protect the federation inboxes from streaming-body OOM.
On the admin app, the request-body cap is the largest of:

- `BRAGI_MAX_REQUEST_BYTES` (default 1 MiB).
- `BRAGI_ATTACHMENTS_MAX_BYTES` (default 20 MiB) plus 64 KiB.
- `BRAGI_DATASET_MAX_UPLOAD_BYTES` (default 100 MiB) plus 64 KiB.

The default admin cap is therefore 100 MiB plus 64 KiB. The extra space
allows for upload form data. To permit larger attachment or dataset
uploads, raise the corresponding upload limit; the admin request-body
cap adjusts automatically.

`BRAGI_LOGIN_THROTTLE_MAX_FAILURES` (default 5) and
`BRAGI_LOGIN_THROTTLE_WINDOW_SECONDS` (default 900) throttle
local-login brute force: a login POST from an IP that already has
that many failed login-form attempts in the window is rejected with
`429` (+ `Retry-After`) before the password is checked. Keyed on
client IP only (no per-account lockout, so an attacker cannot lock an
operator out by guessing their email). It **activates only when
`BRAGI_TRUSTED_PROXY_HOPS` is greater than 0**, i.e. when
`request.remote_addr` is the real per-client address; behind a proxy
with hops unset every client would share the proxy's address and one
attacker could lock everyone out, so the gate stays inactive there
(the admin app logs a warning in production). Set
`BRAGI_LOGIN_THROTTLE_ENABLED=false` to turn it off entirely.

The admin app ships all of its CSS/JS as static files (htmx,
flatpickr, and the TipTap editor are self-hosted; `esm.sh` is the only
external script source, for the editor's ES modules), which lets it
send a **Content-Security-Policy** with a strict `script-src` that
carries no `'unsafe-inline'`. `BRAGI_ADMIN_CSP` controls it:
`report-only` (default) sends `Content-Security-Policy-Report-Only`
(violations logged to the browser console, nothing blocked) so you can
confirm the admin works before switching to `enforce`; `off` omits the
header. The delivery host is not covered (operator content pulls
arbitrary external images/embeds).

Both apps run under gunicorn inside the container (sync worker
class; `--access-logfile -` to stdout). Worker counts default to
2 for admin and 4 for delivery; tune via `ADMIN_WORKERS` /
`DELIVERY_WORKERS` env vars on each service if your traffic
shape needs it.

## Optional integrations and resource limits

`BRAGI_UNSPLASH_ACCESS_KEY` (optional) enables the Unsplash
plugin: authors search Unsplash from inside the admin
attachments picker (and the TipTap "Insert image" button, which
opens the same picker), pick a photo, and the plugin downloads
it into bragi's storage as a regular attachment with the
photographer's name and profile URL stored alongside. On the
public page the credit auto-renders beneath inline-body
Unsplash images. Set the key on the `admin` service only; the
`delivery` service doesn't talk to Unsplash. Get a key from
<https://unsplash.com/developers>. Leave unset to disable the
plugin (the Unsplash tab stays hidden). `BRAGI_UNSPLASH_APP_NAME`
(default `bragi`) controls the `utm_source` tag on credit links;
if you customise it, set it on both admin and delivery for
consistency.

`BRAGI_DATASET_MAX_UPLOAD_BYTES` (default 100 MiB),
`BRAGI_DATASET_QUERY_TIMEOUT_SECONDS` (default 10.0),
`BRAGI_DATASET_QUERY_MAX_ROWS` (default 1000),
`BRAGI_DATASET_QUERY_MEMORY_LIMIT` (default `512MB`), and
`BRAGI_DATASET_QUERY_TEMP_LIMIT` (default `1GB`) tune the
datasets plugin. Upload size, DuckDB query timeout, per-query row
cap, DuckDB memory ceiling, and the on-disk temp-spill bound
respectively. The memory ceiling is a soft cap: once exceeded
DuckDB spills intermediate data to a temp directory, and the
temp-spill bound is what stops a heavy query from filling the disk
(it errors instead). Set on the `admin` service; the delivery app
inherits the row, memory, and temp caps for render-time queries.
Leave at defaults unless a specific file size or query workload
demands otherwise.

`BRAGI_NOTFOUND_BLOCKLIST` is a JSON list of fnmatch globs
(matched against the request path) that the 404 recorder drops
before writing, so vulnerability-scanner probes never reach the
triage table. It defaults to the common exploit paths (`/wp-*`,
`*.php`, `/.env`, `/.git/*`, ...); override with
`BRAGI_NOTFOUND_BLOCKLIST='["/wp-*","*.php"]'` to replace the list
(`.well-known/*` is never blocked). The per-site "404s" admin page
lists the surviving dead paths so you can redirect them, create the
missing content, mark them Gone (410), or dismiss them.

SQLite write-contention knobs (set on every app process; read once
at connect time) default to `BRAGI_SQLITE_BUSY_TIMEOUT_MS=10000`, how
long a write waits for the single write lock before raising `database
is locked`, and `BRAGI_SLOW_WRITE_WARN_MS=2000`, the threshold above
which a write transaction holding the lock emits a `held=Nms` WARNING
on the `bragi.db.slow_write` logger. Raise the busy-timeout on deploys
with sustained concurrent-write pressure; watch the `held=` lines to
see whether contention is actually occurring.

## Background task intervals

Task-runner cadences (all in seconds, set on the `bragi-tasks`
service) default to `SCHEDULED_PUBLISH_EVERY=60`,
`EMBEDS_RERENDER_EVERY=600`, `WEBMENTIONS_SEND_EVERY=300`,
`ACTIVITYPUB_SEND_EVERY=60`, `ANALYZE_EVERY=86400`,
`VACUUM_EVERY=604800`. Override in `compose.yml` if a different
rhythm suits your workload. The webmentions / ActivityPub
cadences only do work when there are queued rows; a site that
hasn't enabled either plugin pays nothing per tick.

## Backups

`bragi backup [--output PATH]`
writes a single `.tar.gz` containing a consistent SQLite snapshot
(produced with `VACUUM INTO`, so no companion `-wal` / `-shm`
files) plus the contents of `Settings.attachments_root` as
`attachments/`. Default output: `bragi-backup-YYYYMMDD-HHMMSS.tar.gz`
in the current working directory.

To restore: extract the tarball, drop `bragi.db` and
`attachments/` into a fresh deployment (matching paths), and
restart the admin + delivery processes. There is no `restore`
subcommand by design; a tool that overwrites a live deployment
is a big risk for not much help.

`bragi backup` is SQLite-only and exits 2 with a clear message
under a non-SQLite `BRAGI_DATABASE_URL` (its `VACUUM INTO` is
SQLite-specific). Postgres operators: use `pg_dump` for the
DB half and a separate tar of `attachments_root` for the file
half. `bragi db vacuum` follows the same gate (`PRAGMA
wal_checkpoint(TRUNCATE)` is SQLite-only); on Postgres use
`VACUUM (FULL)` or your usual autovacuum tooling instead.
