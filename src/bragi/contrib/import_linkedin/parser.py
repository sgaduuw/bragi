"""Pure CSV parsing helpers for the LinkedIn export ZIP.

Each top-level helper takes either a raw string (for the date
parser) or an open file-like (for CSV readers) and returns
typed values. No Flask, no DB, no `bragi.core` request context.

The parser is deliberately lenient: malformed dates fall back to
None; missing optional columns yield empty strings or None;
nothing raises on imperfect input. Recorded warnings live in the
caller's accumulator (the importer's plan/apply orchestrator).
"""

from __future__ import annotations

from datetime import datetime

# LinkedIn writes dates as "MMM YYYY" (e.g. "Apr 2024") or
# "MMMM YYYY" ("January 2020"); empty for current positions.
_YEAR_MONTH_FORMATS = ("%b %Y", "%B %Y")


def parse_year_month(raw: str | None) -> str | None:
    """Convert a LinkedIn date string into the YYYY-MM format the
    `resume_data` schema requires.

    Returns None for blank / unparseable input. The operator can
    fill in unparseable dates by hand later in the admin form.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    for fmt in _YEAR_MONTH_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m")
        except ValueError:
            continue
    return None
