"""Locate + parse a Ghost JSON export.

`load_export(path)` accepts either a single .json file or a
directory containing exactly one .json file. Returns the
`db[0].data` dict so the importer can read `posts`, `users`,
`tags`, `posts_tags` uniformly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _candidate_file(path: Path) -> Path | None:
    if path.is_file() and path.suffix == ".json":
        return path
    if path.is_dir():
        jsons = [p for p in path.iterdir() if p.is_file() and p.suffix == ".json"]
        if len(jsons) == 1:
            return jsons[0]
    return None


def looks_like_ghost(path: Any) -> bool:
    """Structural detection: try to parse the file as a Ghost
    export envelope. Returns True if the structure validates,
    False on missing-file / non-JSON / wrong-shape.

    An earlier version of this function read only the first
    4 KB and looked for `"db"` plus `"posts"` / `"users"`
    substrings. Modern Ghost exports (6.x+) lead `db[0].data`
    with a sizable `custom_theme_settings` array (#95), so
    `"posts"` lands past the 4 KB cutoff and the head-scan
    returned False on genuinely valid files. Files are at
    most low-MB; a full parse is fast enough that the cheap-
    detection optimization wasn't worth the false negatives.
    """
    try:
        load_export(path)
    except OSError, ValueError:
        # `json.JSONDecodeError` subclasses ValueError, so this
        # one except line covers malformed JSON, missing
        # db[]/data envelope, and unreadable files.
        return False
    return True


def load_export(path: Any) -> dict[str, Any]:
    """Parse the export and return the inner `data` dict.

    Raises FileNotFoundError when no JSON file can be located,
    ValueError when the file isn't shaped like a Ghost export.
    """
    src = _candidate_file(Path(path))
    if src is None:
        raise FileNotFoundError(f"No Ghost export JSON found at {path!r}")
    with src.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    db = payload.get("db")
    if not isinstance(db, list) or not db:
        raise ValueError("Ghost export missing top-level db[] array")
    first = db[0]
    if not isinstance(first, dict) or "data" not in first:
        raise ValueError("Ghost export missing db[0].data")
    data = first["data"]
    if not isinstance(data, dict):
        raise ValueError("Ghost export db[0].data is not an object")
    return data
