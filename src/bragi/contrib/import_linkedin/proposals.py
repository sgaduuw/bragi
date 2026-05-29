"""Generate ChangeProposal lists by diffing incoming ResumeData
against existing ResumeData. Also exposes `apply_skills_merge`
which the importer's apply phase calls to reconcile flat
incoming skill lists against operator-defined groupings.

All functions are pure: no Flask, no DB, no I/O. The proposal
ids are deterministic so the same inputs produce the same ids
across runs (lets plan files survive editing + admin form
submissions echo the same ids the review page rendered).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bragi.api import (
    Certification,
    ChangeProposal,
    Education,
    Language,
    Position,
    Project,
    ResumeData,
    SkillGroup,
)

# Fields that are preserved on update for each list-section. The
# proposal payload's "new" dict does NOT include these (apply()
# reads them from the existing matched row).
_PRESERVED_FIELDS = {
    "experience": {"description_markdown", "impacts", "id"},
    "education": {"description_markdown", "location", "id"},
    "projects": {"description_markdown", "impacts", "role", "location", "linked_position_id", "id"},
    "certifications": {"id"},
    "languages": {"id"},
}


def _stable_id(section: str, match_key: tuple[Any, ...], payload: dict[str, Any]) -> str:
    blob = json.dumps(
        {"section": section, "match_key": list(match_key), "payload": payload},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _exp_key(p: Position) -> tuple[str, str, str | None]:
    return (p.company.lower(), p.role.lower(), p.start_date)


def _edu_key(e: Education) -> tuple[str, str]:
    return (e.institution.lower(), e.degree.lower())


def _proj_key(p: Project) -> str:
    return p.name.lower()


def _cert_key(c: Certification) -> tuple[str, str | None]:
    return (c.name.lower(), (c.issuer or "").lower() or None)


def _lang_key(lang: Language) -> str:
    return lang.name.lower()


def _structural_diff(existing: Any, incoming: Any, preserved: set[str]) -> dict[str, Any] | None:
    """Compare two pydantic model instances; return a dict of
    differing fields (excluding preserved-fields). None if no diff.

    Uses `mode="json"` so the resulting diff dict is JSON-safe
    (HttpUrl values become strings); update proposals embed this
    dict in their payload, which the CLI plan file serialises.
    """
    e = existing.model_dump(mode="json")
    i = incoming.model_dump(mode="json")
    diff: dict[str, Any] = {}
    for k, v in i.items():
        if k in preserved:
            continue
        if e.get(k) != v:
            diff[k] = v
    return diff or None


# --------------------------------------------------------------
# Per-section proposal generators
# --------------------------------------------------------------


def _experience_proposals(
    incoming: list[Position], existing: list[Position]
) -> list[ChangeProposal]:
    out: list[ChangeProposal] = []
    existing_by_key = {_exp_key(p): p for p in existing}
    incoming_keys = {_exp_key(p) for p in incoming}

    for new in incoming:
        key = _exp_key(new)
        if key in existing_by_key:
            diff = _structural_diff(existing_by_key[key], new, _PRESERVED_FIELDS["experience"])
            if diff:
                payload = {
                    "match_key": list(key),
                    "new": diff,
                }
                out.append(
                    ChangeProposal(
                        id=_stable_id("experience", key, payload),
                        section="experience",
                        kind="update",
                        summary=f"Update Position: {new.role} at {new.company}",
                        payload=payload,
                    )
                )
        else:
            payload = {"new": new.model_dump(mode="json")}
            out.append(
                ChangeProposal(
                    id=_stable_id("experience", key, payload),
                    section="experience",
                    kind="add",
                    summary=(
                        f"Add Position: {new.role} at {new.company}"
                        f" ({_date_range(new.start_date, new.end_date)})"
                    ),
                    payload=payload,
                )
            )

    for old in existing:
        key = _exp_key(old)
        if key not in incoming_keys:
            payload = {"match_key": list(key), "position_id": old.id}
            out.append(
                ChangeProposal(
                    id=_stable_id("experience", key, payload),
                    section="experience",
                    kind="remove",
                    summary=(
                        f"Remove Position: {old.role} at {old.company}"
                        f" ({_date_range(old.start_date, old.end_date)})"
                    ),
                    payload=payload,
                )
            )
    return out


def _date_range(start: str | None, end: str | None) -> str:
    s = start or "?"
    e = end or "Present"
    return f"{s} -> {e}"


def _education_proposals(
    incoming: list[Education], existing: list[Education]
) -> list[ChangeProposal]:
    # Same shape as experience; a single function keeps the section
    # logic local rather than scattered across helpers.
    out: list[ChangeProposal] = []
    existing_by_key = {_edu_key(e): e for e in existing}
    incoming_keys = {_edu_key(e) for e in incoming}
    for new in incoming:
        key = _edu_key(new)
        if key in existing_by_key:
            diff = _structural_diff(existing_by_key[key], new, _PRESERVED_FIELDS["education"])
            if diff:
                payload = {"match_key": list(key), "new": diff}
                out.append(
                    ChangeProposal(
                        id=_stable_id("education", key, payload),
                        section="education",
                        kind="update",
                        summary=f"Update Education: {new.degree} at {new.institution}",
                        payload=payload,
                    )
                )
        else:
            payload = {"new": new.model_dump(mode="json")}
            out.append(
                ChangeProposal(
                    id=_stable_id("education", key, payload),
                    section="education",
                    kind="add",
                    summary=f"Add Education: {new.degree} at {new.institution}",
                    payload=payload,
                )
            )
    for old in existing:
        key = _edu_key(old)
        if key not in incoming_keys:
            payload = {"match_key": list(key), "education_id": old.id}
            out.append(
                ChangeProposal(
                    id=_stable_id("education", key, payload),
                    section="education",
                    kind="remove",
                    summary=f"Remove Education: {old.degree} at {old.institution}",
                    payload=payload,
                )
            )
    return out


def _projects_proposals(incoming: list[Project], existing: list[Project]) -> list[ChangeProposal]:
    out: list[ChangeProposal] = []
    existing_by_key = {_proj_key(p): p for p in existing}
    incoming_keys = {_proj_key(p) for p in incoming}
    for new in incoming:
        key = (_proj_key(new),)
        if new.name.lower() in existing_by_key:
            diff = _structural_diff(
                existing_by_key[new.name.lower()], new, _PRESERVED_FIELDS["projects"]
            )
            if diff:
                payload = {"match_key": list(key), "new": diff}
                out.append(
                    ChangeProposal(
                        id=_stable_id("projects", key, payload),
                        section="projects",
                        kind="update",
                        summary=f"Update Project: {new.name}",
                        payload=payload,
                    )
                )
        else:
            payload = {"new": new.model_dump(mode="json")}
            out.append(
                ChangeProposal(
                    id=_stable_id("projects", key, payload),
                    section="projects",
                    kind="add",
                    summary=f"Add Project: {new.name}",
                    payload=payload,
                )
            )
    for old in existing:
        if _proj_key(old) not in incoming_keys:
            key = (_proj_key(old),)
            payload = {"match_key": list(key), "project_id": old.id}
            out.append(
                ChangeProposal(
                    id=_stable_id("projects", key, payload),
                    section="projects",
                    kind="remove",
                    summary=f"Remove Project: {old.name}",
                    payload=payload,
                )
            )
    return out


def _certifications_proposals(
    incoming: list[Certification], existing: list[Certification]
) -> list[ChangeProposal]:
    out: list[ChangeProposal] = []
    existing_by_key = {_cert_key(c): c for c in existing}
    incoming_keys = {_cert_key(c) for c in incoming}
    for new in incoming:
        key = _cert_key(new)
        if key in existing_by_key:
            diff = _structural_diff(existing_by_key[key], new, _PRESERVED_FIELDS["certifications"])
            if diff:
                payload = {"match_key": list(key), "new": diff}
                out.append(
                    ChangeProposal(
                        id=_stable_id("certifications", key, payload),
                        section="certifications",
                        kind="update",
                        summary=(
                            f"Update Certification: {new.name}"
                            f" ({new.issuer or 'unknown issuer'})"
                        ),
                        payload=payload,
                    )
                )
        else:
            payload = {"new": new.model_dump(mode="json")}
            out.append(
                ChangeProposal(
                    id=_stable_id("certifications", key, payload),
                    section="certifications",
                    kind="add",
                    summary=f"Add Certification: {new.name}",
                    payload=payload,
                )
            )
    for old in existing:
        key = _cert_key(old)
        if key not in incoming_keys:
            payload = {"match_key": list(key), "cert_id": old.id}
            out.append(
                ChangeProposal(
                    id=_stable_id("certifications", key, payload),
                    section="certifications",
                    kind="remove",
                    summary=f"Remove Certification: {old.name}",
                    payload=payload,
                )
            )
    return out


def _languages_proposals(
    incoming: list[Language], existing: list[Language]
) -> list[ChangeProposal]:
    out: list[ChangeProposal] = []
    existing_by_key = {_lang_key(lang): lang for lang in existing}
    incoming_keys = {_lang_key(lang) for lang in incoming}
    for new in incoming:
        key = (_lang_key(new),)
        if new.name.lower() in existing_by_key:
            diff = _structural_diff(
                existing_by_key[new.name.lower()], new, _PRESERVED_FIELDS["languages"]
            )
            if diff:
                payload = {"match_key": list(key), "new": diff}
                out.append(
                    ChangeProposal(
                        id=_stable_id("languages", key, payload),
                        section="languages",
                        kind="update",
                        summary=f"Update Language: {new.name} ({new.level})",
                        payload=payload,
                    )
                )
        else:
            payload = {"new": new.model_dump(mode="json")}
            out.append(
                ChangeProposal(
                    id=_stable_id("languages", key, payload),
                    section="languages",
                    kind="add",
                    summary=f"Add Language: {new.name} ({new.level})",
                    payload=payload,
                )
            )
    for old in existing:
        if _lang_key(old) not in incoming_keys:
            key = (_lang_key(old),)
            payload = {"match_key": list(key), "language_id": old.id}
            out.append(
                ChangeProposal(
                    id=_stable_id("languages", key, payload),
                    section="languages",
                    kind="remove",
                    summary=f"Remove Language: {old.name}",
                    payload=payload,
                )
            )
    return out


def _skills_proposals(
    incoming_names: list[str], existing: list[SkillGroup]
) -> list[ChangeProposal]:
    """Skills produce at most two proposals: one 'add' carrying
    the new names, one 'remove' carrying the missing names."""
    incoming_set = {n.lower() for n in incoming_names}
    existing_flat = [it for g in existing for it in g.items]
    existing_set = {n.lower() for n in existing_flat}

    new_names = [n for n in incoming_names if n.lower() not in existing_set]
    missing_names = [n for n in existing_flat if n.lower() not in incoming_set]

    out: list[ChangeProposal] = []
    if new_names:
        payload: dict[str, Any] = {"names": new_names}
        out.append(
            ChangeProposal(
                id=_stable_id("skills", ("add",), payload),
                section="skills",
                kind="add",
                summary=f"Add Skills: {', '.join(new_names)}",
                payload=payload,
            )
        )
    if missing_names:
        payload = {"names": missing_names}
        out.append(
            ChangeProposal(
                id=_stable_id("skills", ("remove",), payload),
                section="skills",
                kind="remove",
                summary=f"Remove Skills: {', '.join(missing_names)}",
                payload=payload,
            )
        )
    return out


def _header_and_body_proposals(
    profile: dict[str, str | None],
    existing: ResumeData,
    existing_body: str,
    page_title: str | None,
    page_is_new: bool,
) -> list[ChangeProposal]:
    out: list[ChangeProposal] = []
    new_tagline = profile.get("headline")
    new_location = profile.get("location")
    if new_tagline != existing.header.tagline:
        payload: dict[str, Any] = {"field": "tagline", "new": new_tagline}
        out.append(
            ChangeProposal(
                id=_stable_id("header", ("tagline",), payload),
                section="header",
                kind="update",
                summary=f"Update header tagline: '{new_tagline or '(blank)'}'",
                payload=payload,
            )
        )
    if new_location != existing.header.location:
        payload = {"field": "location", "new": new_location}
        out.append(
            ChangeProposal(
                id=_stable_id("header", ("location",), payload),
                section="header",
                kind="update",
                summary=f"Update header location: '{new_location or '(blank)'}'",
                payload=payload,
            )
        )
    if page_is_new and profile.get("full_name") and not page_title:
        payload = {"field": "title", "new": profile["full_name"]}
        out.append(
            ChangeProposal(
                id=_stable_id("header", ("title",), payload),
                section="header",
                kind="add",
                summary=f"Set page title to '{profile['full_name']}'",
                payload=payload,
            )
        )
    if page_is_new and profile.get("summary") and not existing_body:
        payload = {"new": profile["summary"]}
        out.append(
            ChangeProposal(
                id=_stable_id("body", ("summary",), payload),
                section="body",
                kind="add",
                summary="Populate page body (Summary)",
                payload=payload,
            )
        )
    return out


# --------------------------------------------------------------
# Public entrypoints
# --------------------------------------------------------------


def generate_proposals(
    incoming: ResumeData,
    existing: ResumeData,
    profile: dict[str, str | None],
    *,
    page_is_new: bool,
    incoming_skills: list[str] | None = None,
    existing_body: str = "",
    page_title: str | None = None,
) -> list[ChangeProposal]:
    """Emit one ChangeProposal per concrete difference between
    incoming and existing ResumeData.

    `incoming` carries the parsed list-sections (experience,
    education, projects, certifications, languages); `incoming_skills`
    is passed separately as a flat list of skill names because
    the importer treats skills as a special section.
    """
    proposals: list[ChangeProposal] = []
    proposals.extend(_experience_proposals(incoming.experience, existing.experience))
    proposals.extend(_education_proposals(incoming.education, existing.education))
    proposals.extend(_projects_proposals(incoming.projects, existing.projects))
    proposals.extend(_certifications_proposals(incoming.certifications, existing.certifications))
    proposals.extend(_languages_proposals(incoming.languages, existing.languages))
    proposals.extend(_skills_proposals(incoming_skills or [], existing.skills))
    proposals.extend(
        _header_and_body_proposals(
            profile,
            existing,
            existing_body,
            page_title,
            page_is_new,
        )
    )
    return proposals


def apply_skills_merge(existing: list[SkillGroup], incoming_names: list[str]) -> list[SkillGroup]:
    """Reconcile a flat incoming skill list against operator-
    defined groupings.

    Algorithm:
    1. Keep items in each group that still appear in incoming.
    2. Skills new to incoming (not in any existing group) are
       appended to the first group, or become a new "Skills"
       group if there are no existing groups.
    3. Empty groups are dropped.

    This preserves operator curation: if an operator moved
    "Terraform" from "Languages" to "Tools", a re-import that
    still includes "Terraform" leaves it in "Tools" unmoved.
    """
    incoming_set_ci = {n.lower() for n in incoming_names}
    existing_set_ci = {n.lower() for g in existing for n in g.items}

    # Retain known items that are still present in incoming.
    merged: list[SkillGroup] = []
    for g in existing:
        kept = [n for n in g.items if n.lower() in incoming_set_ci]
        merged.append(SkillGroup(group_label=g.group_label, items=kept))

    # Brand-new skills (not in any existing group) land in the
    # first group so the operator can redistribute them manually.
    new_skills = [n for n in incoming_names if n.lower() not in existing_set_ci]
    if merged and new_skills:
        merged[0] = SkillGroup(
            group_label=merged[0].group_label,
            items=[*merged[0].items, *new_skills],
        )
    elif new_skills:
        merged = [SkillGroup(group_label="Skills", items=new_skills)]

    # Drop groups that became empty after pruning.
    return [g for g in merged if g.items]
