"""Pydantic models for the resume page type.

`ResumeData` is the validated shape of the JSON blob stored in
`pages.resume_data`. Per-section item models (`Position`,
`Project`, `Education`, `SkillGroup`, `Certification`, `Language`,
`ResumeHeader`, `ProfileLink`) define the per-row shapes.

Each repeating-item type carries a stable `id` (12-char hex from a
UUID4) so cross-references like `Project.linked_position_id` survive
reordering and edits. IDs are generated client-side by the admin
form's `+ Add` JS; server-side, Pydantic's `default_factory` fills
any missing IDs on validation (useful for non-JS clients like a
future API caller or `cms` CLI importer).
"""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl, StringConstraints

# YYYY-MM, month-precision dates. Matches the author's CV idiom
# ("Apr 2024 – Present") and HTML5 `<input type="month">` output.
# Day precision was rejected (authors don't know exact start days);
# free-form strings were rejected (no semantic value for microdata).
YearMonth = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
]


def _new_id() -> str:
    """12-char hex from a UUID4 -- short enough to read in the DOM,
    long enough for collision-free per-row identity on a single resume."""
    return uuid4().hex[:12]


class ProfileLink(BaseModel):
    """Single external profile link (e.g. LinkedIn, GitHub)."""

    label: str
    url: HttpUrl


class ResumeHeader(BaseModel):
    """Singleton header block. Name comes from `page.title`."""

    tagline: str | None = None
    location: str | None = None
    profile_links: list[ProfileLink] = Field(default_factory=list)


class Position(BaseModel):
    """One work experience entry."""

    id: str = Field(default_factory=_new_id)
    company: str
    role: str
    location: str | None = None
    start_date: YearMonth | None = None
    end_date: YearMonth | None = None  # None = "Present"
    description_markdown: str = ""
    impacts: list[str] = Field(default_factory=list)


class Project(BaseModel):
    """One project entry. Can optionally link to a Position via
    `linked_position_id` (the linked Position's `id`); the delivery
    template renders the annotation 'at <company>' and an anchor
    link when set."""

    id: str = Field(default_factory=_new_id)
    name: str
    role: str | None = None
    url: HttpUrl | None = None
    linked_position_id: str | None = None
    location: str | None = None
    start_date: YearMonth | None = None
    end_date: YearMonth | None = None
    description_markdown: str = ""
    impacts: list[str] = Field(default_factory=list)


class Education(BaseModel):
    """One education entry. All date / location fields optional."""

    id: str = Field(default_factory=_new_id)
    institution: str
    degree: str
    location: str | None = None
    start_date: YearMonth | None = None
    end_date: YearMonth | None = None
    description_markdown: str = ""


class SkillGroup(BaseModel):
    """One grouped skills entry -- a label plus an ordered list of items."""

    id: str = Field(default_factory=_new_id)
    group_label: str
    items: list[str]


class Certification(BaseModel):
    """One certification (or course; courses fold in here in v1)."""

    id: str = Field(default_factory=_new_id)
    name: str
    issuer: str | None = None
    year: int | None = None
    url: HttpUrl | None = None


class Language(BaseModel):
    """One language entry. `level` is free-form (`Native`, `C1`,
    `Conversational`) so authors aren't forced into a taxonomy."""

    id: str = Field(default_factory=_new_id)
    name: str
    level: str


class ResumeData(BaseModel):
    """The full structured payload for a resume page.

    Stored at `pages.resume_data`. The narrative Summary lives
    separately in `pages.body_markdown` (the existing column,
    repurposed for resume kind).
    """

    header: ResumeHeader = Field(default_factory=ResumeHeader)
    highlights: list[str] = Field(default_factory=list)
    experience: list[Position] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[SkillGroup] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
