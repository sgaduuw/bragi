"""detect / plan / apply for the LinkedIn importer.

`detect(zip_path)` returns True if the file looks like a LinkedIn
export (contains Profile.csv).

`plan(zip_path, page)` parses the ZIP, generates proposals
against the page's existing resume_data (or empty ResumeData for
a fresh page), and returns an ImportPlan whose `proposals` list
is the per-change diff.

`apply(zip_path, site, options)` is added in Task 10.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from bragi.api import ImportPlan, ResumeData
from bragi.contrib.import_linkedin.parser import (
    parse_certifications,
    parse_education,
    parse_languages,
    parse_positions,
    parse_profile,
    parse_projects,
    parse_skills,
)
from bragi.contrib.import_linkedin.proposals import generate_proposals


def detect(path: Any) -> bool:
    """True if `path` is a ZIP containing a `Profile.csv`."""
    p = Path(path)
    if not p.is_file():
        return False
    try:
        with zipfile.ZipFile(p, "r") as zf:
            return "Profile.csv" in zf.namelist()
    except zipfile.BadZipFile:
        return False


def _existing_resume_data(page: Any) -> ResumeData:
    raw = getattr(page, "resume_data", None) or {}
    try:
        return ResumeData.model_validate(raw)
    except Exception:  # noqa: BLE001 - malformed existing data is rare; fall back to empty
        return ResumeData()


def plan(zip_path: Any, page: Any) -> ImportPlan:
    """Dry-run: parse the ZIP, diff against the page's existing
    resume_data, return an ImportPlan with proposals populated.
    Never writes anything."""
    warnings: list[str] = []

    with zipfile.ZipFile(Path(zip_path), "r") as zf:
        profile = parse_profile(zf)
        positions = parse_positions(zf)
        education = parse_education(zf)
        skills_names = parse_skills(zf)
        languages = parse_languages(zf)
        certs = parse_certifications(zf)
        projects = parse_projects(zf)

    incoming = ResumeData(
        experience=positions,
        education=education,
        projects=projects,
        certifications=certs,
        languages=languages,
    )
    existing = _existing_resume_data(page)
    page_is_new = page is None or not (getattr(page, "title", None))
    existing_body = getattr(page, "body_markdown", "") or ""
    page_title = getattr(page, "title", None)

    proposals = generate_proposals(
        incoming,
        existing,
        profile,
        page_is_new=page_is_new,
        incoming_skills=skills_names,
        existing_body=existing_body,
        page_title=page_title,
    )

    counts = {
        "positions": len(positions),
        "education": len(education),
        "skills": len(skills_names),
        "languages": len(languages),
        "certifications": len(certs),
        "projects": len(projects),
        "proposals": len(proposals),
    }
    return ImportPlan(counts=counts, warnings=warnings, proposals=proposals)
