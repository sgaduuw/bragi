# AGENTS.md

Conventions for AI coding agents working on bragi. Tracked in the
repo deliberately: more than one agent and more than one tool work
on this codebase, and these are the rules whose absence causes real
bugs rather than style nits. Read this before your first change.

bragi is at 1.53.x, published to PyPI as `bragi-cms`, shipped as two
GHCR images, and serving a live site. Treat it as production.

Operator and deployment specifics are not in this file. If a task
needs them, say so and ask rather than guessing.

## What bragi is

A self-hosted CMS: a Flask app split into two processes against one
database. **Admin** (authoring, auth-gated, its own host) and
**delivery** (public rendering). Content is Markdown, rendered with
markdown-it-py, edited through TipTap, stored in SQLite via
SQLAlchemy 2.0. Features are **plugins**, including the built-in
ones.

## Tooling

uv for Python (PEP 735 `[dependency-groups]`, hatchling backend),
alembic for migrations, ruff for lint, mypy for types, pytest for
tests.

```sh
uv sync

# These four are exactly what CI runs. All four must pass.
uv run ruff check src/ tests/ alembic/
uv run ruff format --check src/ tests/ alembic/
uv run mypy src/
uv run pytest

uv run alembic upgrade head
uv run alembic revision --autogenerate -m "describe change"

make dev        # admin + delivery side by side via Procfile.dev
```

**Run all four gates, not three.** They are independent, CI runs
each as its own step, and lint-clean is not format-clean. Judge each
by its **exit code**, run bare with no pipe: a pipe replaces the
tool's exit status with the pipe's, so an `&&` chain marches past a
failure and the error line scrolls away.

Before pushing, prefer **applying** over checking:
`uv run ruff check --fix` and `uv run ruff format` on the touched
paths. A local `--check` has repeatedly reported clean while CI's
identically-pinned ruff disagreed; applying removes the question.

The raw commands above are deliberate: they are exactly what CI
runs, so they are the contract. For the comfortable local path
(the `make` targets, what `make dev` does for you, how the pieces
fit together) see **[docs/development.md](docs/development.md)**.
That file is the human-facing orientation and this one is the
agent-facing contract; where they cover the same ground they must
agree, and neither should grow a copy of the other.

## Architecture rules that are load-bearing

Violating any of these produces a bug, not a style complaint.

- **Markdown is the source of truth.** `body_markdown` is canonical;
  `body_html` is a cached render regenerated on save. Never write
  `body_html` directly and never read it as authoritative.
- **Contrib plugin boundary.** `bragi/contrib/X/` may import only
  from `bragi.api`, `bragi.core.models`, and `bragi.core` utilities.
  **Never from a sibling `bragi.contrib.*`.** This keeps each plugin
  liftable into its own package later. It also means "three plugins
  share this block, let us extract a helper" is often *illegal*
  rather than merely unnecessary: the helper has to move to
  `bragi.core`, or not happen.
- **Built-ins are plugins by registration.** Each `bragi.contrib.X`
  reaches the app through the `bragi.plugins` entry-point group, the
  same path a third party uses. There is no internal fast path.
- **Public API is `bragi.api`.** Plugin authors import only from
  there. `bragi.hookspecs` is an internal contract and may be
  reshuffled.
- **Models live in `bragi.core.models/`**, even when a plugin
  defines the content type, so alembic autogenerate has one source
  of truth.
- **Settings are pydantic**, in `src/bragi/settings.py`, with a sensible
  default per field and `.env` overrides via pydantic-settings.
- **Server-side sessions.** The cookie carries an opaque UUID only.
- **Redirects are first-class.** A slug change auto-inserts a 301
  and the resolve pipeline runs on every public 404. Do not bypass
  it, and do not bypass URL preservation on import.

## The database rules, which bite hardest

SQLite with one writer. Two rules follow, and both have caused
production 500s:

1. **One connection per request.** A lifecycle hookimpl that writes
   MUST use the session it is handed and must never open its own
   `SessionLocal()`. A second connection's write deadlocks against
   an in-request sibling holding SQLite's single write lock, and
   `busy_timeout` cannot break an intra-request deadlock.
2. **Sessions are `autoflush=False`, so a per-row loop that writes a
   value checked against sibling rows must `db.flush()` after each
   write.** Otherwise row N's uniqueness check cannot see row N-1's
   assignment. This fails two ways: an `IntegrityError` 500 when the
   DB constraint catches it, and a *silent duplicate* when it does
   not (SQLite treats NULL in a UNIQUE tuple as distinct, so the
   app-level check is the only guard). Single-row routes are usually
   safe because they commit per call; bulk actions are not.

## htmx dispatch

Views return a full page on cold load and a partial on htmx
requests. Crawlers always see the full page; partial swaps are a UX
affordance, never a content gate.

Partial templates sit next to their full-page sibling with a `_`
prefix, wrapped in a stable id so `hx-target` works against either
response shape, and the full page `{% include %}`s the partial so
markup is not duplicated.

**Use `wants_partial()`, not `is_htmx()`, at the partial fork of any
view a boosted rail link can reach.** A boosted request carries both
`HX-Request` and `HX-Boosted`: it is a full-page navigation, so the
view must render the whole page for htmx to select out of it.
`wants_partial()` is `is_htmx() and not is_boosted()`, true only for
genuine in-page swaps. Getting this wrong returns a fragment where a
page was needed.

**Inline-edit cells come in two shapes, and the control type picks
the shape.** A `<select>` must be always-live and save on `change`:
it does not trigger implicit submission on Enter and the open
dropdown swallows Escape, so an Enter-to-save select silently cannot
save. A free-text `<input>` uses the double-click editor, and its
`hx-trigger="dblclick, keyup[key=='Enter']"` must be wrapped in
`{% if mode != 'edit' %}`, or Enter fires both the save and a
re-render that races it and wins, so the cell reverts.

Note that both of those bugs shipped, and neither reproduces on
`make dev`: the dev server is single-threaded and serialises
requests that gunicorn runs concurrently.

## Markdown extensions

- **A fence-renderer override must wrap and delegate, never
  replace.** Capture `previous = md.renderer.rules.get("fence")` at
  configure time and delegate for anything you do not handle, so
  overrides compose regardless of registration order. Replacing
  outright clobbers the others.
- **Adding a `:::` directive: pick by whether the body is parsed.**
  `container_plugin` when the inner content is markdown; a
  hand-written `md.block.ruler.before` rule when the block is
  replaced wholesale.
- **A TipTap node's editor round-trip proves `body_markdown` only.**
  Delivery renders `body_html` server-side, so verify that side by
  exercising the Python renderer against the exact shapes the JS
  serializer emits (doc-start, back-to-back, last block, nested,
  empty, CRLF). There is no JS test harness.

## Security

- **Never linkify an externally-sourced URL without a scheme
  allowlist.** Template autoescaping stops text and attribute
  breakout; it does **not** stop a `javascript:` or `data:` URL
  sitting inside an otherwise valid `href`, which executes in the
  admin origin and defeats the admin-host isolation. Resolve through
  `bragi.core.safe_urls.safe_external_url` and linkify only when it
  returns non-None; always render the raw string as text. Applies to
  anything that entered the DB from outside: webmention sources,
  analytics referrers, imported links. Internal links built as
  `base + stored_path` are exempt.
- **A default-on control that depends on separate config must fail
  toward off.** The login throttle is per-IP and only meaningful
  when `trusted_proxy_hops > 0`; behind a proxy with hops unset,
  every client shares one bucket and a few failed logins lock out
  everyone including the operator. Gate activation on the
  dependency and warn at startup rather than shipping a
  self-inflicted lockout.

## Tests

`uv run pytest` is the gate. Add tests for anything touching
rendering, the plugin surface, auth, redirects, or a write path.

Two structural blind spots in the fixtures, worth knowing before you
trust a green run:

- The suite uses `:memory:` SQLite built with `Base.metadata.create_all`
  rather than alembic, so anything that exists only in a migration
  (FTS5 virtual tables, triggers, generated columns) is absent, and
  a `_safe`-style swallow turns that into a silent pass.
- `:memory:` has different connection semantics from file-backed
  SQLite, so cross-connection write-lock races never reproduce.

When correctness depends on either, add a unit-level spy that
asserts the invariant directly rather than relying on the
integration path.

Five mechanical checks worth more than "write a good test":

1. **Watch it fail.** Run the assertion against the unfixed code and
   see red first. A test never seen to fail is not a guard.
2. **Assert the fixture's precondition**, so it cannot pass for the
   wrong reason or quietly stop exercising the state under test.
3. **Count executions** when the central assertion sits behind an
   `if` or a `continue`.
4. **Ask what the broken state looks like** and whether your
   comparison degenerates there.
5. **Parse values; never `in` against formatted output.**
   `"remaining=5" in log` also matches `remaining=50`.

## CHANGELOG

**Add a line under `## [Unreleased]` in the same PR as the change**,
for any user-visible behaviour, schema, config, or CLI/route change.
Skip it only for pure internal refactors.

This is the convention agents miss most often, and it is why this
file exists. The entries become published release notes, so treat
them as a live surface: if one quotes a figure the code later
corrected, fix the entry too. No em-dashes, since the text ends up
in GitHub release notes verbatim.

Categories: **Added**, **Changed**, **Deprecated**, **Removed**,
**Fixed**, **Security**.

## README currency

Before opening a PR, sweep the README files the change touches or
invalidates: command examples, env vars, flag names, deploy
ordering, project-layout listings, route lists, feature claims,
hardcoded versions.

Removing or renaming a structural identifier (a module, workflow,
plugin, Settings field, CLI command) means grepping the README for
the old name in the same PR. Adding a public route means updating
the route list.

## Pull requests

- **One closing keyword per issue.** `Closes #N, #M.` closes only
  `#N`. Write `Closes #N. Closes #M.`, in the PR body.
- Keep the diff truthful to its title. Revert unrelated drift rather
  than bundling it.
- **No em-dashes or double-dashes in prose**, anywhere: comments,
  docstrings, commit messages, PR bodies, CHANGELOG.
- git-flow branch names off `develop`: `feature/*`, `fix/*`,
  `chore/*`, `docs/*`, `refactor/*`, `test/*`.

## Branching and versioning

git-flow. `develop` is the default branch and integration target;
`main` holds tagged released code only. Releases go through
`release/X.Y.Z`, urgent fixes through `hotfix/X.Y.Z` off `main`.

Version source of truth is `pyproject.toml`'s `version`. Refresh
`uv.lock` in the same commit or `develop` diverges from `main` on
the next clone. Both are bumped on the release branch, never on a
feature branch.

SemVer by what changed: PATCH for fixes and dependency refreshes,
MINOR for behaviour, UX or new config, MAJOR for schema or
interface breaks.

## Out of scope without asking

Do not add a headless/JSON content API, multilingual support, or a
block-tree content model. Each was considered and deliberately
deferred; the rationale is recorded and a PR that assumes otherwise
will be rejected on design grounds rather than on code quality.

## If you are unsure

Ask rather than guess, particularly about the contrib plugin
boundary, anything touching the write path, the redaction and
escaping posture for externally-sourced content, or whether a
surface is crawler-visible.
