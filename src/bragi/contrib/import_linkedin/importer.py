"""detect / plan / apply for the LinkedIn importer.

`detect(zip_path)` returns True if the file looks like a LinkedIn
export (contains Profile.csv).

`plan(zip_path, page)` parses the ZIP, generates proposals
against the page's existing resume_data (or empty ResumeData for
a fresh page), and returns an ImportPlan whose `proposals` list
is the per-change diff.

`apply(zip_path, page, options)` filters proposals by
`options["selected_change_ids"]`, recomputes the diff for
concurrent-edit safety, enacts approved changes, and writes
provenance metadata.
"""

from __future__ import annotations

import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import bragi
from bragi.api import (
    Certification,
    Education,
    ImportPlan,
    ImportResult,
    Language,
    Position,
    Project,
    ResumeData,
    ResumeHeader,
)
from bragi.contrib.import_linkedin.parser import (
    parse_certifications,
    parse_education,
    parse_languages,
    parse_positions,
    parse_profile,
    parse_projects,
    parse_skills,
)
from bragi.contrib.import_linkedin.proposals import (
    apply_skills_merge,
    generate_proposals,
)
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page


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


def apply(zip_path: Any, page: Any, options: dict[str, Any]) -> ImportResult:
    """Apply the proposals whose ids appear in
    `options["selected_change_ids"]` to the given resume page.

    Recomputes the diff internally (recompute-then-filter), so a
    proposal whose target row has changed between plan and apply
    is skipped with a warning rather than enacted against stale
    data.
    """
    start = time.monotonic()
    selected: set[str] = set(options.get("selected_change_ids") or set())
    warnings: list[str] = []
    counts: dict[str, int] = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}

    with zipfile.ZipFile(Path(zip_path), "r") as zf:
        profile = parse_profile(zf)
        positions = parse_positions(zf)
        education = parse_education(zf)
        skills_names = parse_skills(zf)
        languages = parse_languages(zf)
        certs = parse_certifications(zf)
        projects = parse_projects(zf)

    # Refetch the page inside an open session so writes commit.
    with SessionLocal() as db:
        fresh = db.get(Page, page.id)
        if fresh is None:
            return ImportResult(
                counts=counts,
                warnings=[f"Page {page.id} no longer exists."],
            )

        existing = _existing_resume_data(fresh)
        body = fresh.body_markdown or ""
        title = fresh.title

        incoming = ResumeData(
            experience=positions,
            education=education,
            projects=projects,
            certifications=certs,
            languages=languages,
        )
        proposals = generate_proposals(
            incoming,
            existing,
            profile,
            page_is_new=False,
            incoming_skills=skills_names,
            existing_body=body,
            page_title=title,
        )

        approved = {p.id for p in proposals if p.id in selected}
        # Concurrent-edit detection. Any id in `selected` that no
        # longer matches a recomputed proposal id means the target
        # row changed between plan and apply (e.g. the operator
        # edited the page in another tab). Skip the stale proposal
        # and surface a warning so the admin flash can tell the
        # operator that something was dropped.
        dropped = selected - approved
        counts["skipped"] = len(dropped)
        for stale_id in sorted(dropped):
            warnings.append(
                f"Skipped proposal {stale_id} (target row changed since plan was generated)."
            )

        # Build the new ResumeData by walking the proposals; sections
        # not touched by any approved proposal stay at the existing
        # value (narrative preserve).
        new_experience = _apply_list_section(
            existing.experience,
            approved,
            proposals,
            section="experience",
            preserve_fields={"description_markdown", "impacts", "id"},
        )
        new_education = _apply_list_section(
            existing.education,
            approved,
            proposals,
            section="education",
            preserve_fields={"description_markdown", "location", "id"},
        )
        new_projects = _apply_list_section(
            existing.projects,
            approved,
            proposals,
            section="projects",
            preserve_fields={
                "description_markdown",
                "impacts",
                "role",
                "location",
                "linked_position_id",
                "id",
            },
        )
        new_certs = _apply_list_section(
            existing.certifications,
            approved,
            proposals,
            section="certifications",
            preserve_fields={"id"},
        )
        new_languages = _apply_list_section(
            existing.languages,
            approved,
            proposals,
            section="languages",
            preserve_fields={"id"},
        )

        # Skills: if either add OR remove proposal is approved, run
        # the full merge. (Skills can't be partially merged - the
        # merge takes the full incoming set.)
        skills_props = [p for p in proposals if p.section == "skills" and p.id in approved]
        if skills_props:
            new_skills = apply_skills_merge(existing.skills, skills_names)
        else:
            new_skills = list(existing.skills)

        # Header tagline / location.
        new_header = ResumeHeader(
            tagline=existing.header.tagline,
            location=existing.header.location,
            profile_links=existing.header.profile_links,
        )
        for prop in proposals:
            if prop.section != "header" or prop.id not in approved:
                continue
            field_name = prop.payload.get("field")
            if field_name == "tagline":
                new_header = new_header.model_copy(update={"tagline": prop.payload.get("new")})
            elif field_name == "location":
                new_header = new_header.model_copy(update={"location": prop.payload.get("new")})
            elif field_name == "title":
                fresh.title = prop.payload.get("new") or fresh.title

        # Body (Summary): only populate if currently empty.
        for prop in proposals:
            if prop.section == "body" and prop.id in approved and not body:
                fresh.body_markdown = prop.payload.get("new") or ""

        new_rd = ResumeData(
            header=new_header,
            highlights=list(existing.highlights),
            experience=new_experience,
            projects=new_projects,
            education=new_education,
            skills=new_skills,
            certifications=new_certs,
            languages=new_languages,
        )
        fresh.resume_data = new_rd.model_dump(mode="json")

        # Counts by approved-proposal kind.
        for prop in proposals:
            if prop.id not in approved:
                continue
            if prop.kind == "add":
                counts["added"] += 1
            elif prop.kind == "update":
                counts["updated"] += 1
            elif prop.kind == "remove":
                counts["removed"] += 1

        # Provenance.
        target_slug = fresh.slug
        fresh.source_id = f"linkedin:{target_slug}"
        fresh.source_meta = {
            "linkedin_export_filename": Path(zip_path).name,
            "imported_at": datetime.now(UTC).isoformat(),
            "applied_change_ids": sorted(approved),
            "importer_version": bragi.__version__,
        }

        db.commit()

    duration = time.monotonic() - start
    return ImportResult(
        counts=counts,
        warnings=warnings,
        duration_seconds=duration,
    )


def _apply_list_section(
    existing_list: list[Any],
    approved: set[str],
    proposals: list[Any],
    *,
    section: str,
    preserve_fields: set[str],
) -> list[Any]:
    """Build the new list for a section by walking approved
    proposals. Existing matched rows keep `preserve_fields`
    even if a proposal's `new` payload accidentally carries
    values for them; this function is the local enforcement
    point so future callers don't have to trust that the
    upstream proposal generator already stripped them.
    """
    sec_props = [p for p in proposals if p.section == section]
    out = list(existing_list)

    # Apply removes (drop from out).
    for prop in sec_props:
        if prop.kind != "remove" or prop.id not in approved:
            continue
        mk = tuple(prop.payload.get("match_key") or [])
        out = [r for r in out if _list_match_key(r, section) != mk]

    # Apply updates (in-place by match key; never overwrite
    # preserve_fields even if a buggy proposal includes them).
    for prop in sec_props:
        if prop.kind != "update" or prop.id not in approved:
            continue
        mk = tuple(prop.payload.get("match_key") or [])
        new_fields = {
            k: v for k, v in (prop.payload.get("new") or {}).items() if k not in preserve_fields
        }
        for i, row in enumerate(out):
            if _list_match_key(row, section) == mk:
                out[i] = row.model_copy(update=new_fields)
                break

    # Apply adds (build the corresponding incoming row by match key).
    for prop in sec_props:
        if prop.kind != "add" or prop.id not in approved:
            continue
        # The "new" payload holds a model-dump of the incoming row.
        # Build via model_validate so pydantic re-runs defaults
        # (notably the per-row id factory).
        new_row_dict = prop.payload.get("new") or {}
        out.append(_build_section_model(section, new_row_dict))

    return out


def _list_match_key(row: Any, section: str) -> tuple[Any, ...]:
    """Return the match key for a row in the given section.

    Mirrors the key functions in proposals.py so the same row
    maps to the same key on both sides of the plan/apply boundary.
    """
    if section == "experience":
        return (row.company.lower(), row.role.lower(), row.start_date)
    if section == "education":
        return (row.institution.lower(), row.degree.lower())
    if section == "projects":
        return (row.name.lower(),)
    if section == "certifications":
        return (row.name.lower(), (row.issuer or "").lower() or None)
    if section == "languages":
        return (row.name.lower(),)
    return ()


def _build_section_model(section: str, data: dict[str, Any]) -> Any:
    """Build a pydantic model instance for the given section from a
    model-dump dict. Dispatches to the correct class so Pydantic
    re-runs validators and default factories (e.g. id generation).
    """
    if section == "experience":
        return Position.model_validate(data)
    if section == "education":
        return Education.model_validate(data)
    if section == "projects":
        return Project.model_validate(data)
    if section == "certifications":
        return Certification.model_validate(data)
    if section == "languages":
        return Language.model_validate(data)
    raise ValueError(f"unknown section {section!r}")
