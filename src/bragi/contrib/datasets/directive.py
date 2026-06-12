"""Markdown directive for dataset embeds.

Authoring:

    ::: dataset slug=cpi q=cpi-by-quarter
    :::

    ::: dataset slug=cpi sql="SELECT max(value) FROM cpi" format=scalar
    :::

Wired as a custom markdown-it block rule (same shape as the
embeds directive): match `::: dataset <attrs>` on its own line,
scan forward for the closing `:::`, emit one `bragi_dataset`
token. The renderer resolves the dataset and query against the
post's site, runs the SQL through the guarded engine, and bakes
the chosen format's HTML into `body_html` at save time.

Site identity resolution order:

1. `env["bragi_site_id"]`, supplied by the rerender path (which
   runs outside any request context).
2. `g.current_site`, set by `resolve_site_or_abort` on every
   site-scoped admin request, which is where post/page saves run.

Failures never raise out of the renderer; they bake a visible
error card that the rerender pass can retry.
"""

from __future__ import annotations

import shlex
from typing import Any

from flask import g
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from bragi.contrib.datasets.engine import DatasetError, run_dataset_query
from bragi.contrib.datasets.render import (
    render_chart,
    render_error,
    render_scalar,
    render_table,
)
from bragi.core.db import SessionLocal
from bragi.core.models.dataset import DATASET_FORMATS, Dataset, DatasetQuery
from bragi.core.models.site import Site

_MARKER = "::: dataset"
_CLOSE = ":::"


def _dataset_block(state: Any, start_line: int, end_line: int, silent: bool) -> bool:
    """Match `::: dataset <attrs>\\n:::\\n`; emit a `bragi_dataset` token."""
    pos = state.bMarks[start_line] + state.tShift[start_line]
    maximum = state.eMarks[start_line]
    line = state.src[pos:maximum]

    if not line.startswith(_MARKER):
        return False
    rest = line[len(_MARKER) :]
    # Require a separator so `::: datasets-other` can't match.
    if rest and not rest[0].isspace():
        return False

    # Scan for the closing `:::`; decline if it never comes so the
    # directive can't swallow the rest of the document.
    close_line = -1
    next_line = start_line + 1
    while next_line < end_line:
        line_start = state.bMarks[next_line] + state.tShift[next_line]
        line_max = state.eMarks[next_line]
        if state.src[line_start:line_max].strip() == _CLOSE:
            close_line = next_line
            break
        next_line += 1
    if close_line < 0:
        return False
    if silent:
        # `silent` is used by markdown-it during paragraph
        # termination checks; succeed without emitting tokens.
        return True

    token = state.push("bragi_dataset", "", 0)
    token.block = True
    token.markup = _MARKER
    token.meta = {"attr_text": rest.strip()}
    token.map = [start_line, close_line + 1]
    state.line = close_line + 1
    return True


def _parse_attrs(attr_text: str) -> dict[str, str] | None:
    """`slug=cpi q="name" format=table` -> dict; None on bad syntax.

    shlex handles the quoting so inline SQL with spaces survives.
    """
    try:
        parts = shlex.split(attr_text)
    except ValueError:
        return None
    attrs: dict[str, str] = {}
    for part in parts:
        key, sep, value = part.partition("=")
        if not sep or not key or not value:
            return None
        attrs[key] = value
    return attrs


def _site_id_from_context(env: dict[str, Any]) -> int | None:
    site_id = env.get("bragi_site_id")
    if isinstance(site_id, int):
        return site_id
    try:
        # Outside a request/app context, accessing `g` raises
        # RuntimeError ("working outside of application context");
        # treat that as "no site context", same as a missing attr.
        site = getattr(g, "current_site", None)
    except RuntimeError:
        return None
    # g.current_site is set by resolve_site_or_abort from the URL slug, and
    # the post/page being saved belongs to that same site by construction,
    # so this fallback cannot cross site boundaries.
    return getattr(site, "id", None)


def _render_bragi_dataset(tokens: list[Any], idx: int, _options: Any, env: dict[str, Any]) -> str:
    attr_text = tokens[idx].meta.get("attr_text", "")
    attrs = _parse_attrs(attr_text)
    if attrs is None:
        return render_error(f"malformed attributes: {attr_text!r}", slug=None)

    slug = attrs.get("slug")
    query_name = attrs.get("q")
    inline_sql = attrs.get("sql")
    fmt = attrs.get("format")

    if not slug:
        return render_error("missing required attribute: slug", slug=None)
    if bool(query_name) == bool(inline_sql):
        return render_error(
            'exactly one of q=<saved query> or sql="..." is required',
            slug=slug,
            query=query_name,
            fmt=fmt,
        )
    if inline_sql and not fmt:
        return render_error("inline sql needs an explicit format=", slug=slug, fmt=fmt)
    if inline_sql and fmt == "chart":
        return render_error(
            "format=chart needs a saved query (the Vega-Lite spec lives there)",
            slug=slug,
            fmt=fmt,
        )
    if fmt is not None and fmt not in DATASET_FORMATS:
        return render_error(f"unknown format {fmt!r}", slug=slug, query=query_name)

    site_id = _site_id_from_context(env)
    if site_id is None:
        return render_error(
            "no site context (render outside a site-scoped save?)",
            slug=slug,
            query=query_name,
            fmt=fmt,
        )

    # Fresh short-lived read session: the calling save view's
    # session isn't reachable from a markdown renderer rule, and
    # these are pure reads (no write-lock interaction; the save's
    # write transaction starts at flush, after rendering).
    try:
        with SessionLocal() as db:
            dataset = db.execute(
                select(Dataset).where(Dataset.site_id == site_id, Dataset.slug == slug)
            ).scalar_one_or_none()
            if dataset is None:
                return render_error(
                    f"no dataset {slug!r} on this site", slug=slug, query=query_name, fmt=fmt
                )
            # scalar_one_or_none, not scalar_one: the dataset row just
            # resolved against site_id, but the Site row could in
            # principle vanish between the two reads. Bake an error
            # card rather than let NoResultFound escape the renderer.
            site_slug = db.execute(select(Site.slug).where(Site.id == site_id)).scalar_one_or_none()
            if site_slug is None:
                return render_error(
                    "site row vanished during render", slug=slug, query=query_name, fmt=fmt
                )

            vega_spec: str | None = None
            if query_name:
                saved = db.execute(
                    select(DatasetQuery).where(
                        DatasetQuery.dataset_id == dataset.id,
                        DatasetQuery.name == query_name,
                    )
                ).scalar_one_or_none()
                if saved is None:
                    return render_error(
                        f"no saved query {query_name!r} on dataset {slug!r}",
                        slug=slug,
                        query=query_name,
                        fmt=fmt,
                    )
                sql = saved.sql
                fmt = fmt or saved.default_format
                vega_spec = saved.vega_spec_json
            else:
                sql = inline_sql or ""

            if fmt == "chart" and not vega_spec:
                return render_error(
                    f"saved query {query_name!r} has no Vega-Lite spec",
                    slug=slug,
                    query=query_name,
                    fmt=fmt,
                )

            try:
                result = run_dataset_query(site_slug, dataset, sql)
            except DatasetError as exc:
                return render_error(str(exc), slug=slug, query=query_name, fmt=fmt)
    except SQLAlchemyError as exc:
        # Defence in depth: a connection-level failure (locked DB,
        # disposed engine) must still bake a card, never raise out of
        # the renderer and abort the whole save.
        return render_error(f"database error: {exc}", slug=slug, query=query_name, fmt=fmt)

    if fmt == "chart":
        return render_chart(result, vega_spec or "")
    if fmt == "scalar":
        return render_scalar(result)
    return render_table(result)


def configure_datasets(md: Any) -> None:
    """Wire the block rule and renderer onto `md` in place.

    `register_markdown_extension` returns this callable; the app
    factory invokes it once per process at boot.
    """
    md.block.ruler.before(
        "fence",
        "bragi_dataset",
        _dataset_block,
        {"alt": ["paragraph", "reference", "blockquote", "list"]},
    )
    # `md` is typed `Any` (the registration callable is invoked with a
    # concrete `MarkdownIt`), so unlike the embeds directive no
    # `attr-defined` ignore is needed on the `renderer.rules` access.
    md.renderer.rules["bragi_dataset"] = _render_bragi_dataset
