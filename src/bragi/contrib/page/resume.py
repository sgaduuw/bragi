"""Backward-compat shim: re-export the resume schema from `bragi.api`.

The model classes (`ResumeData`, `Position`, `Project`,
`Education`, `SkillGroup`, `Certification`, `Language`,
`ResumeHeader`, `ProfileLink`) and the `YearMonth` constraint
were promoted to `bragi.api` so resume-source plugins (LinkedIn,
future Notion-CV, etc.) can build instances without crossing the
contrib-to-contrib boundary.

Existing in-tree callsites import from here; this module keeps
their imports working. New code should import from `bragi.api`.
"""

from __future__ import annotations

from bragi.api import (
    Certification,
    Education,
    Language,
    Position,
    ProfileLink,
    Project,
    ResumeData,
    ResumeHeader,
    SkillGroup,
    YearMonth,
    _new_id,
)

__all__ = [
    "Certification",
    "Education",
    "Language",
    "Position",
    "ProfileLink",
    "Project",
    "ResumeData",
    "ResumeHeader",
    "SkillGroup",
    "YearMonth",
    "_new_id",
]
