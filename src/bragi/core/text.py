"""Text utilities shared across plugins.

Slugify lives here because it's used in at least three places
(heading anchors, post / page slug auto-suggest, tag slug
derivation) and cross-plugin imports are forbidden by the
contrib boundary rule. Keep it dependency-light: stdlib only.
"""

from __future__ import annotations

import re
import unicodedata


def slugify(text: str) -> str:
    """Return a URL-safe slug for `text`.

    Strategy:
    1. NFKD-normalise + drop non-ASCII (`Naïve` -> `Naive`).
    2. Lowercase.
    3. Replace runs of non-[a-z0-9] with a single `-`.
    4. Trim leading and trailing `-`.

    Returns `""` if no sluggable characters remain.
    """
    normalised = unicodedata.normalize("NFKD", text)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered)
    return hyphenated.strip("-")
