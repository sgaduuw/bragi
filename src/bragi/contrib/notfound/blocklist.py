"""Scanner-path blocklist for the 404 recorder.

Vulnerability scanners hammer every public site with probes for
paths that never existed (`/wp-login.php`, `/.env`, `/.git/config`).
The recorder drops any request path matching a configured fnmatch
glob BEFORE writing, so the `not_founds` table holds real dead
links, not scanner noise. Patterns come from
`Settings.notfound_blocklist`.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase


def is_blocklisted(path: str, patterns: Iterable[str]) -> bool:
    """True if `path` matches any blocklist glob (case-insensitive).

    Case-folded so a `*.php` pattern also catches a `/X.PHP` probe;
    scanner paths vary case freely. `fnmatchcase` on the lowered
    path keeps matching deterministic across platforms (plain
    `fnmatch` defers to `os.path.normcase`, which differs by OS).
    """
    lowered = path.lower()
    return any(fnmatchcase(lowered, pat.lower()) for pat in patterns)
