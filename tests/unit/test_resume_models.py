"""Pydantic model validation for ResumeData and per-section item shapes.

Pure-logic tests; no DB, no Flask. Covers the validation contract the
page admin save flow relies on.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bragi.contrib.page.resume import (
    Certification,
    Education,
    Language,
    Position,
    ProfileLink,
    Project,
    ResumeData,
    ResumeHeader,
    SkillGroup,
)
from bragi.contrib.page.resume_jsonld import build_jsonld

# ============================================================
# YearMonth pattern
# ============================================================


@pytest.mark.parametrize("good", ["2024-01", "2024-04", "2024-12", "1999-09"])
def test_yearmonth_accepts_valid_strings(good: str) -> None:
    Position(company="X", role="Y", start_date=good)


@pytest.mark.parametrize("bad", ["2024-4", "2024-13", "2024-00", "04-2024", "present", "2024", ""])
def test_yearmonth_rejects_invalid_strings(bad: str) -> None:
    with pytest.raises(ValidationError):
        Position(company="X", role="Y", start_date=bad)


def test_yearmonth_none_is_allowed() -> None:
    """end_date=None means 'Present'; start_date=None is also valid."""
    p = Position(company="X", role="Y")
    assert p.start_date is None
    assert p.end_date is None


# ============================================================
# Stable IDs
# ============================================================


def test_each_item_gets_a_stable_id_by_default() -> None:
    p1 = Position(company="A", role="x")
    p2 = Position(company="B", role="y")
    assert p1.id and p2.id
    assert p1.id != p2.id
    assert len(p1.id) == 12
    assert all(c in "0123456789abcdef" for c in p1.id)


def test_explicit_id_is_preserved() -> None:
    """When clients send an existing id, Pydantic keeps it (round-trip stable)."""
    p = Position(id="abc123def456", company="A", role="x")
    assert p.id == "abc123def456"


@pytest.mark.parametrize(
    "model,kwargs",
    [
        (Position, {"company": "x", "role": "y"}),
        (Project, {"name": "x"}),
        (Education, {"institution": "x", "degree": "y"}),
        (SkillGroup, {"group_label": "x", "items": ["y"]}),
        (Certification, {"name": "x"}),
        (Language, {"name": "x", "level": "y"}),
    ],
)
def test_all_repeating_types_carry_stable_ids(model: type, kwargs: dict[str, object]) -> None:
    assert model(**kwargs).id


# ============================================================
# ResumeData round-trip
# ============================================================


def test_resume_data_default_is_all_empty() -> None:
    rd = ResumeData()
    assert rd.header == ResumeHeader()
    assert rd.highlights == []
    assert rd.experience == []
    assert rd.projects == []
    assert rd.education == []
    assert rd.skills == []
    assert rd.certifications == []
    assert rd.languages == []


def test_empty_resume_data_dumps_to_empty_dict_with_exclude_defaults() -> None:
    """Storing the JSON minimally: defaults are excluded so empty resumes
    don't bloat the DB."""
    rd = ResumeData()
    dumped = rd.model_dump(mode="json", exclude_defaults=True)
    assert dumped == {}


def test_round_trip_preserves_all_fields() -> None:
    rd = ResumeData(
        header=ResumeHeader(tagline="Engineer", location="NL"),
        highlights=["one", "two"],
        experience=[
            Position(
                company="Acme",
                role="SRE",
                start_date="2020-01",
                end_date="2024-03",
                description_markdown="- did things",
                impacts=["impact 1"],
            )
        ],
        projects=[
            Project(name="Homelab", url="https://example.test"),
        ],
        skills=[SkillGroup(group_label="Stack", items=["Python", "Go"])],
    )
    dumped = rd.model_dump(mode="json", exclude_defaults=True)
    reloaded = ResumeData.model_validate(dumped)
    assert reloaded.header.tagline == "Engineer"
    assert reloaded.experience[0].company == "Acme"
    assert reloaded.projects[0].name == "Homelab"
    assert str(reloaded.projects[0].url) == "https://example.test/"
    assert reloaded.skills[0].items == ["Python", "Go"]


# ============================================================
# Project -> Position linkage
# ============================================================


def test_project_linked_position_id_accepts_any_string() -> None:
    """Pydantic doesn't FK-validate; the template gracefully skips dangling
    references at render time. This keeps the validation layer simple
    and lets reordering / deleting positions never invalidate the JSON."""
    p = Project(name="x", linked_position_id="some-nonexistent-id")
    assert p.linked_position_id == "some-nonexistent-id"


def test_project_linked_position_id_defaults_to_none() -> None:
    assert Project(name="x").linked_position_id is None


# ============================================================
# JSON-LD builder
# ============================================================


def test_jsonld_minimal_resume_emits_person_block() -> None:
    jsonld = build_jsonld(page_title="Eelco Wesemann", resume=ResumeData())
    assert jsonld["@context"] == "https://schema.org"
    assert jsonld["@type"] == "Person"
    assert jsonld["name"] == "Eelco Wesemann"
    # Empty resume: no optional blocks
    assert "jobTitle" not in jsonld
    assert "worksFor" not in jsonld
    assert "alumniOf" not in jsonld


def test_jsonld_emits_jobTitle_and_address_from_header() -> None:
    jsonld = build_jsonld(
        page_title="Eelco",
        resume=ResumeData(
            header=ResumeHeader(tagline="Platform Engineer", location="Den Haag, NL")
        ),
    )
    assert jsonld["jobTitle"] == "Platform Engineer"
    assert jsonld["address"] == "Den Haag, NL"


def test_jsonld_emits_sameAs_from_profile_links() -> None:
    jsonld = build_jsonld(
        page_title="Eelco",
        resume=ResumeData(
            header=ResumeHeader(
                profile_links=[
                    ProfileLink(label="LinkedIn", url="https://linkedin.test/x"),
                    ProfileLink(label="GitHub", url="https://github.test/x"),
                ]
            )
        ),
    )
    assert jsonld["sameAs"] == [
        "https://linkedin.test/x",
        "https://github.test/x",
    ]


def test_jsonld_emits_worksFor_from_experience() -> None:
    jsonld = build_jsonld(
        page_title="Eelco",
        resume=ResumeData(
            experience=[
                Position(
                    company="Acme",
                    role="SRE",
                    start_date="2020-01",
                    end_date="2024-03",
                ),
                Position(
                    company="Logius",
                    role="Platform Engineer",
                    start_date="2024-04",
                    end_date=None,  # Present
                ),
            ],
        ),
    )
    assert len(jsonld["worksFor"]) == 2
    acme = jsonld["worksFor"][0]
    assert acme["@type"] == "Organization"
    assert acme["name"] == "Acme"
    # OccupationalRole nested for start/end dates
    role = acme["member"]
    assert role["@type"] == "OccupationalRole"
    assert role["roleName"] == "SRE"
    assert role["startDate"] == "2020-01"
    assert role["endDate"] == "2024-03"
    # Present: no endDate
    logius = jsonld["worksFor"][1]
    assert "endDate" not in logius["member"]


def test_jsonld_emits_alumniOf_from_education() -> None:
    jsonld = build_jsonld(
        page_title="Eelco",
        resume=ResumeData(
            education=[
                Education(institution="Hogeschool Utrecht", degree="HBO Informatica"),
            ],
        ),
    )
    assert jsonld["alumniOf"][0]["@type"] == "EducationalOrganization"
    assert jsonld["alumniOf"][0]["name"] == "Hogeschool Utrecht"
    assert jsonld["alumniOf"][0]["alumni"]["@type"] == "EducationalOccupationalCredential"
    assert jsonld["alumniOf"][0]["alumni"]["name"] == "HBO Informatica"


def test_jsonld_emits_hasCredential_from_certifications() -> None:
    jsonld = build_jsonld(
        page_title="Eelco",
        resume=ResumeData(
            certifications=[
                Certification(name="CKA", issuer="CNCF", year=2024),
            ],
        ),
    )
    assert jsonld["hasCredential"][0]["@type"] == "EducationalOccupationalCredential"
    assert jsonld["hasCredential"][0]["name"] == "CKA"
    assert jsonld["hasCredential"][0]["recognizedBy"] == "CNCF"
    assert jsonld["hasCredential"][0]["dateCreated"] == "2024"


def test_jsonld_emits_knowsLanguage() -> None:
    jsonld = build_jsonld(
        page_title="Eelco",
        resume=ResumeData(
            languages=[
                Language(name="Dutch", level="Native"),
                Language(name="English", level="C2"),
            ],
        ),
    )
    assert jsonld["knowsLanguage"] == ["Dutch", "English"]


# ============================================================
# lead_with_role display toggle
# ============================================================


def test_lead_with_role_defaults_to_false() -> None:
    """Default preserves Company  Role rendering on existing resumes."""
    rd = ResumeData()
    assert rd.lead_with_role is False


def test_lead_with_role_backwards_compat_on_missing_key() -> None:
    """Existing resume_data JSON blobs predate the field; deserialising
    must fill the default rather than raise. This is the invariant that
    lets us ship without an alembic migration."""
    rd = ResumeData.model_validate({})
    assert rd.lead_with_role is False


def test_lead_with_role_round_trips_true() -> None:
    rd = ResumeData.model_validate({"lead_with_role": True})
    assert rd.lead_with_role is True
    assert rd.model_dump()["lead_with_role"] is True
