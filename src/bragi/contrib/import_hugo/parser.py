"""Frontmatter + body splitter for Hugo content files.

Hugo recognises three frontmatter syntaxes:

- `+++` ... `+++`   TOML  (most common in older Hugo sites)
- `---` ... `---`   YAML  (most common now)
- `{` ... `}`        JSON  (rare; rejected here for v1, we can
                            add it when a real export needs it)

Returns a `(metadata: dict, body: str)` tuple; metadata is `{}`
when no frontmatter delimiters are found.
"""

from __future__ import annotations

import tomllib
from typing import Any

import yaml


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split Hugo source into (frontmatter dict, body string)."""
    stripped = text.lstrip("﻿")  # BOM tolerance
    if stripped.startswith("+++\n"):
        return _split("+++\n", "+++", stripped, tomllib.loads)
    if stripped.startswith("---\n"):
        return _split("---\n", "---", stripped, yaml.safe_load)
    return {}, text


def _split(
    open_delim: str,
    close_delim: str,
    text: str,
    loader: Any,
) -> tuple[dict[str, Any], str]:
    rest = text[len(open_delim) :]
    end = rest.find(f"\n{close_delim}")
    if end < 0:
        # Unterminated frontmatter: treat the whole file as body.
        # Importer surfaces a warning when the title can't be found.
        return {}, text
    raw = rest[:end]
    body = rest[end + len(close_delim) + 1 :]
    if body.startswith("\n"):
        body = body[1:]
    parsed = loader(raw) or {}
    if not isinstance(parsed, dict):
        # YAML lists at the top of a file are syntactically valid
        # but useless here; fail soft.
        return {}, text
    return parsed, body
