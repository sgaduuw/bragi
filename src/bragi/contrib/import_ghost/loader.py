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
    """Soft detection: a single JSON file whose top-level matches
    the Ghost export envelope. Reads only the first few KiB to
    keep `detect()` cheap on misuse."""
    src = _candidate_file(Path(path))
    if src is None:
        return False
    try:
        with src.open("r", encoding="utf-8") as f:
            head = f.read(4096)
    except OSError:
        return False
    # Loose: a real check would parse and inspect db[0].data.posts.
    # The cheap heuristic is enough to avoid false positives in
    # `detect()`; the full parse happens in `load_export`.
    return '"db"' in head and ('"posts"' in head or '"users"' in head)


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
