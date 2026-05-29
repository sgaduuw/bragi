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

import csv
import io
import zipfile
from datetime import datetime

from pydantic import HttpUrl, TypeAdapter, ValidationError

from bragi.api import (
    Certification,
    Education,
    Language,
    Position,
    Project,
)

# TypeAdapter for coercing raw URL strings from CSV to pydantic's
# HttpUrl type (or None for blank / invalid URLs).
_URL_ADAPTER: TypeAdapter[HttpUrl | None] = TypeAdapter(HttpUrl | None)

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


def _csv_rows(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    """Read the named CSV from the open ZipFile, lowercasing and
    trimming column headers so callers can look up by ``"started on"``
    regardless of the source's Title Case.

    Returns [] if the CSV is missing or empty.
    """
    if name not in zf.namelist():
        return []
    with zf.open(name) as f:
        text = io.TextIOWrapper(f, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text)
        rows: list[dict[str, str]] = []
        for raw in reader:
            normalised = {
                (k or "").strip().lower(): (v or "").strip()
                for k, v in raw.items()
                if isinstance(v, str)
            }
            rows.append(normalised)
        return rows


def _none_if_blank(s: str | None) -> str | None:
    """Return None for empty or whitespace-only strings; strip the rest."""
    if s is None:
        return None
    s = s.strip()
    return s or None


def _parse_url(raw: str | None) -> HttpUrl | None:
    """Coerce a raw URL string to pydantic's HttpUrl, returning None
    for blank values or strings that fail validation (malformed URLs
    are silently dropped rather than aborting the whole import row).
    """
    cleaned = _none_if_blank(raw)
    if cleaned is None:
        return None
    try:
        return _URL_ADAPTER.validate_python(cleaned)
    except ValidationError:
        return None


def parse_profile(zf: zipfile.ZipFile) -> dict[str, str | None]:
    """Read Profile.csv into a small dict. LinkedIn's Profile.csv
    has one row; we read the first and ignore any others.

    Returns a dict with keys full_name, headline, location,
    summary. Any field absent in the CSV becomes None.
    """
    rows = _csv_rows(zf, "Profile.csv")
    if not rows:
        return {
            "full_name": None,
            "headline": None,
            "location": None,
            "summary": None,
        }
    r = rows[0]
    first = _none_if_blank(r.get("first name"))
    last = _none_if_blank(r.get("last name"))
    name_parts = [p for p in (first, last) if p]
    return {
        "full_name": " ".join(name_parts) if name_parts else None,
        "headline": _none_if_blank(r.get("headline")),
        "location": _none_if_blank(r.get("geo location")),
        "summary": _none_if_blank(r.get("summary")),
    }


def parse_positions(zf: zipfile.ZipFile) -> list[Position]:
    """Read Positions.csv. Output sorted by start_date descending
    (most recent first); rows with no start_date sort last.
    """
    rows = _csv_rows(zf, "Positions.csv")
    positions: list[Position] = []
    for r in rows:
        company = _none_if_blank(r.get("company name"))
        role = _none_if_blank(r.get("title"))
        if not company or not role:
            # company and role are required fields on Position
            continue
        positions.append(
            Position(
                company=company,
                role=role,
                location=_none_if_blank(r.get("location")),
                start_date=parse_year_month(r.get("started on")),
                end_date=parse_year_month(r.get("finished on")),
                description_markdown=(r.get("description") or "").strip(),
                impacts=[],
            )
        )
    positions.sort(
        key=lambda p: p.start_date or "0000-00",
        reverse=True,
    )
    return positions


def parse_education(zf: zipfile.ZipFile) -> list[Education]:
    """Read Education.csv. Blank Degree Name falls back to
    ``"(unspecified)"`` so the row is preserved (the pydantic field
    is required); the operator can clean it up later.

    Notes and Activities are joined with a blank line between them
    when both are present, matching how authors typically separate
    distinct sections in markdown.
    """
    rows = _csv_rows(zf, "Education.csv")
    out: list[Education] = []
    for r in rows:
        institution = _none_if_blank(r.get("school name"))
        if not institution:
            continue
        degree = _none_if_blank(r.get("degree name")) or "(unspecified)"
        notes = (r.get("notes") or "").strip()
        activities = (r.get("activities") or "").strip()
        joined = "\n\n".join(p for p in (notes, activities) if p)
        out.append(
            Education(
                institution=institution,
                degree=degree,
                location=None,
                start_date=parse_year_month(r.get("start date")),
                end_date=parse_year_month(r.get("end date")),
                description_markdown=joined,
            )
        )
    return out


def parse_skills(zf: zipfile.ZipFile) -> list[str]:
    """Read Skills.csv into a flat ordered list of skill names.

    v1 reads only the ``name`` column; LinkedIn exports may include
    additional columns (Approval count, etc.) which are ignored.
    """
    rows = _csv_rows(zf, "Skills.csv")
    out: list[str] = []
    for r in rows:
        name = _none_if_blank(r.get("name"))
        if name:
            out.append(name)
    return out


def parse_languages(zf: zipfile.ZipFile) -> list[Language]:
    """Read Languages.csv. ``level`` is free-form per the
    ResumeData schema (``Native``, ``C1``, ``Conversational``, etc.).
    """
    rows = _csv_rows(zf, "Languages.csv")
    out: list[Language] = []
    for r in rows:
        name = _none_if_blank(r.get("name"))
        if not name:
            continue
        level = _none_if_blank(r.get("proficiency")) or "(unspecified)"
        out.append(Language(name=name, level=level))
    return out


def parse_certifications(zf: zipfile.ZipFile) -> list[Certification]:
    """Read Certifications.csv. ``year`` is extracted from the YYYY-MM
    string returned by ``parse_year_month``; unparseable dates produce
    ``year=None`` rather than raising.
    """
    rows = _csv_rows(zf, "Certifications.csv")
    out: list[Certification] = []
    for r in rows:
        name = _none_if_blank(r.get("name"))
        if not name:
            continue
        ym = parse_year_month(r.get("started on"))
        year = int(ym.split("-", 1)[0]) if ym else None
        out.append(
            Certification(
                name=name,
                issuer=_none_if_blank(r.get("authority")),
                year=year,
                url=_parse_url(r.get("url")),
            )
        )
    return out


def parse_projects(zf: zipfile.ZipFile) -> list[Project]:
    """Read Projects.csv. Description is preserved verbatim on first
    import; the proposal-generation phase refines it later.
    """
    rows = _csv_rows(zf, "Projects.csv")
    out: list[Project] = []
    for r in rows:
        name = _none_if_blank(r.get("title"))
        if not name:
            continue
        out.append(
            Project(
                name=name,
                url=_parse_url(r.get("url")),
                start_date=parse_year_month(r.get("started on")),
                end_date=parse_year_month(r.get("finished on")),
                description_markdown=(r.get("description") or "").strip(),
                impacts=[],
            )
        )
    return out
