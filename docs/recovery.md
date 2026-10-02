# Recovering a site or deleted media

[Back to operations](operations.md)

Restore the pre-upgrade database **and media** into a fresh destination,
using the matching application version. Keep the failed deployment stopped
and retain its files and logs for diagnosis. Returning to the previous image
alone does not undo database migrations or media changes.

This procedure covers SQLite and the built-in local storage backend. It
requires a short maintenance window. A backup made while writers are active
is not guaranteed to be a consistent database/media pair.

## Keep a recovery set

| Retain | Included in `bragi backup`? |
| --- | --- |
| SQLite database, including users, credentials, content, revisions, redirects, sessions and migration revision | Yes |
| Files under `BRAGI_ATTACHMENTS_ROOT`, including local originals, renditions and dataset files | Yes, if the directory exists |
| Exact admin/delivery image digests or application source revision and dependency lock | No |
| Installed third-party plugins, themes and their exact versions | No |
| Private environment/configuration, including the persistent `BRAGI_SECRET_KEY` and integration credentials | No |
| Compose/proxy configuration, storage mappings and ownership | No |
| Remote storage or files outside `BRAGI_ATTACHMENTS_ROOT` | No |
| Unsaved browser-local writing | No |

Keep the previous images or install artifacts available before upgrading.
Save configuration securely alongside the recovery instructions. The backup
contains sensitive database records and must be protected like the live data.
Copy it off the deployment's volume before starting an upgrade.

Record the time, image digests/source revision, Python and Bragi versions,
installed plugins, and `SELECT version_num FROM alembic_version` with the
backup. A package version alone cannot distinguish unreleased source commits.
Do not put secret values in the evidence record.

## Make a consistent backup

1. Put the site into maintenance at the proxy and stop **all writers**:
   admin, delivery, `bragi-tasks`, external cron/import/CLI jobs, and any
   separate rendition workers. Delivery also writes analytics and other
   records. Wait for in-flight processes to exit.
2. Verify the configured database and attachment paths are the intended
   mounts and are readable. The current command silently omits a missing
   attachments directory, so a zero exit status alone is insufficient.
3. Run `bragi backup` with the **old application and configuration**, writing
   the archive outside the attachment tree. Keep writers stopped until the
   archive has completed and been copied to independent storage.
4. Inspect the archive, verify expected media are present, and record its
   checksum. Allow space for the temporary SQLite snapshot and the archive.

For the repository's Compose example, from its deployment directory with
its existing pinned release and private `.env`:

```sh
# Also pause external jobs and put the proxy in maintenance first.
docker compose stop admin delivery bragi-tasks
mkdir -m 700 recovery-set
# Use a new archive name for each attempt; do not replace a known-good backup.
docker compose run --rm --no-deps admin bragi backup --output /data/pre-upgrade.tar.gz
docker compose cp admin:/data/pre-upgrade.tar.gz recovery-set/pre-upgrade.tar.gz
tar -tzf recovery-set/pre-upgrade.tar.gz
shasum -a 256 recovery-set/pre-upgrade.tar.gz
```

Check each command's exit status before continuing. This example assumes
existing admin containers and the default named volume; adapt paths for a
custom deployment. `run --no-deps` runs only the backup command, without
starting the migration scheduler. Save configuration and version information
separately as described above.

Why stop writers? `VACUUM INTO` gives a consistent database snapshot. Media
are then copied separately. An attachment removed between those steps can
leave a database reference with no bytes in the archive. A concurrent upload
can instead leave extra bytes without the corresponding database row. SQLite
snapshot consistency does not make the file archive atomic.

## Attempt the upgrade

Keep web services and background jobs stopped. Select the new pinned release
and explicitly run its migrations:

```sh
docker compose run --rm --no-deps admin bragi db upgrade
```

Require a zero exit status, inspect the output and verify the database's
`alembic_version` matches the target application's migration head. Start web
services for private acceptance checks before reopening traffic and resuming
tasks. `docker compose up -d --no-deps admin delivery` leaves the task runner
stopped while you check. Keep outbound integrations isolated during rehearsal.

An existing `/data/.migrated`, a healthy `/healthz`, or the scheduler exiting
with status zero is **not** proof of a successful upgrade. The scheduler can
exit zero after exhausting migration retries. A failed migration can leave
persistent DDL even if `alembic_version` still names the previous revision.

## Restore into an empty destination

1. Keep the failed deployment stopped. Preserve it for diagnosis. Do not
   overwrite its database, reuse its WAL/SHM files, or run a downgrade as a
   substitute for the backup unless that particular downgrade was tested.
2. Select the saved **previous** application and plugin versions. Restore
   their configuration and persistent secret separately. Prevent public
   traffic, automatic migrations, task execution and outbound federation
   from reaching the recovery environment.
3. Verify the archive checksum against the recorded value. Extract only a
   trusted backup into a newly created directory. For Python 3.14:

   ```sh
   python - <<'PYTHON'
   from pathlib import Path
   import tarfile

   destination = Path('restored-data')
   destination.mkdir(mode=0o700)  # Fails if it already exists.
   with tarfile.open('recovery-set/pre-upgrade.tar.gz') as archive:
       archive.extractall(destination, filter='data')
   PYTHON
   ```

4. Check database integrity and the recorded revision before starting apps:

   ```sh
   python - <<'PYTHON'
   import sqlite3

   with sqlite3.connect('file:restored-data/bragi.db?mode=ro', uri=True) as db:
       assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
       assert db.execute('PRAGMA foreign_key_check').fetchall() == []
       print('Restored revision:', db.execute(
           'SELECT version_num FROM alembic_version').fetchall())
   PYTHON
   ```

5. Point the previous application at the restored database and media. The
   archive calls the media directory `attachments/`; the default Compose
   mount expects `/data/uploads/`. When preparing a **fresh** Compose volume,
   copy `bragi.db` to its root and the contents of `attachments/` to `uploads/`.
   Preserve site/hash subdirectories. Make the new volume writable by the
   image's UID 1000. Confirm the mount is the new volume, not the failed one.
   For a Python deployment, set `BRAGI_DATABASE_URL` and
   `BRAGI_ATTACHMENTS_ROOT` to the absolute restored paths.
6. Start only the previous admin and delivery apps against this new storage.
   Verify the checks below. Keep the task runner off until you have reviewed
   pending scheduled posts and outbound work: restoring an earlier database
   can replay work that already happened after the backup.
7. Only after acceptance, switch the intended deployment to the restored
   storage, reopen traffic, and resume tasks deliberately. Writes after the
   backup are absent; preserve the failed data if any need reconciliation.

## Acceptance checks

Use real application requests, not only `/healthz`:

- Sign in using the recovery/local administrator and open a content editor.
- Open a known published post and page with the correct site hostname; verify
  their content and navigation. Check every site in a multisite deployment.
- Request a known old URL and verify its redirect status and destination.
- Fetch an original image and at least one rendered size at their original
  public URLs, checking bytes rather than only the surrounding HTML.
- Verify any third-party theme/plugin, dataset or remote storage your site
  depends on. These deployment-specific surfaces need their own samples.

## Repeatable developer rehearsal

From a Bragi checkout with locked development dependencies installed:

```sh
uv run pytest -q -s tests/test_recovery.py
```

This creates disposable file-backed SQLite databases through real Alembic
migrations, seeds synthetic content, runs the backup CLI with no concurrent
writers, and restores into an empty destination. It checks login, editing,
post/page rendering, a redirect, an original image, and a rendition. It
injects a migration failure after persistent DDL and verifies recovery from
the untouched backup. A missing-media negative control must fail.

The default run uses the current application on both sides and needs no Git
tags or network access. To also test a populated database across versions,
prepare a separate checkout/environment of the chosen prior release using
its own lockfile, then supply its Python executable:

```sh
BRAGI_RECOVERY_PREVIOUS_PYTHON=/absolute/path/to/prior/.venv/bin/python \
  uv run pytest -q -s tests/test_recovery.py
```

That interpreter runs the seed, backup and restored-site checks. The current
interpreter then migrates the restored populated database and checks it again.
The probe uses BeautifulSoup for form parsing; the tested release's locked
development environment includes it. No production backup is loaded.

### Recorded rehearsal: 2026-10-02

- Prior application: v1.53.1, source `2ebc687`, schema `78d0fa9367df`.
- Upgrade target: develop `17095df`, schema `f515c0ffee01`; package metadata
  still reports 1.53.1, with unreleased changes on top.
- Runtime: macOS, Python 3.14.4, SQLite via Python, each checkout's frozen uv
  environment. This was an application-level rehearsal using Flask's request
  clients, not a Docker/proxy or live-deployment test.
- Result: previous-app restore and populated-database upgrade passed. The
  injected migration left its partial table behind; fresh restoration removed
  it. The missing-media control failed as intended, then complete media passed.
- Limits: synthetic single-site content, local storage and built-in plugins.
  This does not establish completeness of an operator's backup or test their
  OAuth provider, third-party plugins, host permissions or network routing.


## Recover accidentally deleted media

Deletion removes the attachment record and its rendition records. Unshared
files are removed too. Markdown references remain, while featured-image and
site-default selections are cleared. A surviving attachment record can keep
shared bytes alive; that alone does not preserve a deleted featured-image
selection.

Before confirming deletion, review the listed published content, drafts,
working copies, saved revisions and site defaults. Published featured images
can also supply social cards. Confirmation is checked again when submitted;
changed usage requires another review. The list reports known usage, not a
promise that an image is unused. External sites, third-party plugins,
structured resume data, custom theme/settings fields and browser-only drafts
are outside this check.

### Full recovery from backup

Use the recovery procedure above with a backup made before deletion. Restore
both the database and media into an empty destination with the matching
application version. This preserves original and rendition URLs, attachment
metadata, and featured-image associations. **It also rolls back content and
settings changed after that backup.** Inspect the recovered site privately
before deciding to replace the current deployment.

### Recover one original without rolling back the site

1. Extract a trusted pre-deletion backup into a separate directory using the
   safe extraction procedure above. Keep it separate from the running site.
2. Locate the exact original file under
   `attachments/<site>/<first-two-hash-characters>/<hash>/original.<extension>`.
   Older backups can use a flat hash file. Check its SHA-256 against the hash
   in the broken `/attachments/<hash>` URL (`shasum -a 256 <file>`).
3. Upload those exact bytes to the same site's Media library. Bragi derives
   the original public URL from those bytes, so the original URL returns
   without editing Markdown. Re-saving or re-encoding an image changes its
   hash and does not repair the old URL.
4. Restore alt text, credits and featured-image/site-default selections from
   the backup as needed. Uploading the bytes does not restore this metadata
   or the cleared selections automatically.
5. Process the queued renditions with `bragi media process-renditions`.
   Current theme widths determine which renditions are recreated. Historical
   rendition URLs for other widths or formats may still be missing; use full
   backup recovery when those exact URLs must be preserved. Copying files
   alone is insufficient because delivery also needs their database records.
6. Run the check below and inspect the affected public pages and social cards.

### Check references after recovery or import

```sh
bragi media check --site blog
```

Run this with the restored/imported site's configuration and media storage.
It is read-only and does not fetch external URLs. It checks known local media
references against both database records and stored bytes, reports affected
content, and exits nonzero when media are missing. For referenced originals,
it also checks every completed rendition, including variants chosen for
social cards and responsive images. Pending rendition jobs are not missing
files. Markdown destinations are decoded and site hostnames and aliases are
recognized with explicit ports, as they are by delivery. It includes historical
revisions and working copies, so a finding can concern a recoverable draft
rather than currently published content. A clean result cannot detect a
featured-image selection already cleared by deletion; compare those
selections with the backup separately.

The file-backed rehearsal is runnable with:

```sh
uv run pytest tests/test_recovery.py::test_deleted_media_recovery_preserves_public_urls
```

It verifies deletion damage, original URL recovery by re-uploading backed-up
bytes, and complete recovery of originals, renditions and featured-image
associations from a database/media backup. There is no trash retention period;
a pre-deletion backup or retained original is required.
