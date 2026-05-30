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
import re
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

# Unicode glyphs LinkedIn (and Word / Pages, where authors often
# paste from) uses as bullet markers at the start of plain-text
# description lines. Match these only when they sit at the start
# of a line (possibly after whitespace, i.e. an indented bullet)
# so a glyph used mid-line as an ornamental separator is left
# alone.
_BULLET_GLYPHS = "•‣▪◦●○◆"
_BULLET_LINE_RE = re.compile(r"^([ \t]*)[" + _BULLET_GLYPHS + r"][ \t]*")

# Inline asterisk bullet marker. LinkedIn often serialises a
# bulleted description into a single line with no actual line
# breaks, using ` * ` (space-asterisk-whitespace) or a leading
# `* ` at the start as the separator between bullet items.
# Example shape: `intro. * point one * point two * point three`.
# We only treat this as a list when at least 2 markers are
# present so a single literal asterisk in prose (e.g. `5 * 7 =
# 35` or a footnote marker) is not misclassified.
_INLINE_ASTERISK_BULLET_RE = re.compile(r"(?:^|\s)\*\s+")


def _convert_inline_asterisk_bullets(text: str) -> str:
    """If the text looks like an inline-asterisk bullet list,
    rewrite it as intro paragraph + markdown list. Returns the
    text unchanged otherwise.
    """
    matches = list(_INLINE_ASTERISK_BULLET_RE.finditer(text))
    # Require at least 2 markers; a single inline `*` is more
    # likely a literal asterisk than a one-item list.
    if len(matches) < 2:
        return text
    chunks: list[str] = []
    prev_end = 0
    for m in matches:
        chunks.append(text[prev_end : m.start()].strip())
        prev_end = m.end()
    chunks.append(text[prev_end:].strip())
    intro = chunks[0]
    bullets = [c for c in chunks[1:] if c]
    if not bullets:
        return text
    parts: list[str] = []
    if intro:
        parts.append(intro)
        # Blank line before the list so strict CommonMark renders
        # it as a list rather than continuing the paragraph.
        parts.append("")
    parts.extend(f"- {b}" for b in bullets)
    return "\n".join(parts)


def clean_linkedin_description(raw: str) -> str:
    """Convert a LinkedIn plain-text description into presentable
    markdown.

    LinkedIn's CSV export carries description fields as plain
    text. Common shapes:

    1. `\\r\\n` line endings between paragraphs, with a Unicode
       bullet glyph (`•`, `‣`, etc.) at the start of each bullet
       line. (Older / paste-from-Pages authors.)
    2. A single line containing the whole description with no
       real line breaks, using inline ` * ` markers between
       bullet items: ``intro. * point one * point two``. (Most
       common in newer LinkedIn-authored exports.)
    3. Plain prose with no bullets at all.

    This helper applies a small deterministic cleanup that
    covers shapes 1 and 2 and leaves shape 3 alone:

    - Normalises `\\r\\n` and `\\r` to `\\n`.
    - Detects inline ` * ` bullet patterns (2+ markers required
      to avoid misclassifying a literal asterisk in prose) and
      rewrites them as intro paragraph + markdown list.
    - Strips trailing whitespace on each line.
    - Converts a bullet glyph at the start of a line (after
      optional indentation) into a markdown ``-`` list marker.
      Indentation is preserved so nested bullets become nested
      markdown lists.
    - Preserves paragraph breaks (`\\n\\n`).
    - Leaves bullet glyphs mid-line alone (`Foo • Bar • Baz` is
      an ornamental separator pattern, not a list).

    Heuristic, not perfect. The operator can still hand-edit any
    description that comes out looking odd in the admin form.
    """
    if not raw:
        return ""
    # Line endings: normalise to \n.
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    # Inline ` * ` bullet pattern: rewrite as intro + list.
    text = _convert_inline_asterisk_bullets(text)
    out_lines: list[str] = []
    for line in text.split("\n"):
        # Trailing whitespace stripped per line.
        stripped = line.rstrip()
        # Bullet glyph at start (after optional whitespace) -> "- ".
        # The capture group preserves the leading indentation.
        converted = _BULLET_LINE_RE.sub(r"\1- ", stripped)
        # If the substitution produced "- " followed by nothing
        # (the original line was just a bullet), trim the trailing
        # space so the line reads "-", which markdown still renders
        # as an empty list item without leaving a dangling space.
        if converted.endswith("- "):
            converted = converted[:-1]
        out_lines.append(converted)
    return "\n".join(out_lines).strip()


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
    summary_raw = r.get("summary") or ""
    summary_clean = clean_linkedin_description(summary_raw)
    return {
        "full_name": " ".join(name_parts) if name_parts else None,
        "headline": _none_if_blank(r.get("headline")),
        "location": _none_if_blank(r.get("geo location")),
        "summary": summary_clean or None,
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
                description_markdown=clean_linkedin_description(r.get("description") or ""),
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
        notes = clean_linkedin_description(r.get("notes") or "")
        activities = clean_linkedin_description(r.get("activities") or "")
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
                description_markdown=clean_linkedin_description(r.get("description") or ""),
                impacts=[],
            )
        )
    return out
