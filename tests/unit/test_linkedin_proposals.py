"""Unit tests for ChangeProposal generation and skills merging.

Pure functions: feed in incoming + existing ResumeData; assert
the proposal list. No Flask, no ZIP, no DB."""

from __future__ import annotations

from bragi.api import (
    Position,
    ResumeData,
    SkillGroup,
)
from bragi.contrib.import_linkedin.proposals import (
    apply_skills_merge,
    generate_proposals,
)


def _position(
    company: str,
    role: str,
    start: str | None,
    description: str = "",
    impacts: list[str] | None = None,
    position_id: str = "abc123",
) -> Position:
    return Position(
        id=position_id,
        company=company,
        role=role,
        start_date=start,
        description_markdown=description,
        impacts=impacts or [],
    )


# ----------------------- experience: add / update / remove ----


def test_experience_all_adds_on_first_import() -> None:
    incoming = ResumeData(
        experience=[
            _position("Acme", "Engineer", "2020-01"),
            _position("Beta", "Senior Engineer", "2022-03"),
        ]
    )
    existing = ResumeData()
    profile = {"full_name": "Test", "headline": None, "location": None, "summary": None}
    proposals = generate_proposals(incoming, existing, profile, page_is_new=True)
    exp = [p for p in proposals if p.section == "experience"]
    assert len(exp) == 2
    assert all(p.kind == "add" for p in exp)


def test_experience_match_no_diff_emits_no_proposal() -> None:
    same = _position("Acme", "Engineer", "2020-01")
    incoming = ResumeData(experience=[same])
    existing = ResumeData(experience=[same])
    proposals = generate_proposals(
        incoming,
        existing,
        {"full_name": None, "headline": None, "location": None, "summary": None},
        page_is_new=False,
    )
    assert [p for p in proposals if p.section == "experience"] == []


def test_experience_renamed_title_emits_remove_and_add() -> None:
    existing = ResumeData(
        experience=[
            _position(
                "Acme",
                "Engineer",
                "2020-01",
                description="Wrote the X platform.",
                position_id="old_id",
            ),
        ]
    )
    incoming = ResumeData(
        experience=[
            _position("Acme", "Senior Engineer", "2020-01"),
        ]
    )
    proposals = generate_proposals(
        incoming,
        existing,
        {"full_name": None, "headline": None, "location": None, "summary": None},
        page_is_new=False,
    )
    exp = [p for p in proposals if p.section == "experience"]
    kinds = sorted(p.kind for p in exp)
    assert kinds == ["add", "remove"]


def test_experience_structural_diff_emits_update() -> None:
    existing = ResumeData(
        experience=[
            _position("Acme", "Engineer", "2020-01", description="Owned X", position_id="keep_id"),
        ]
    )
    incoming_pos = _position("Acme", "Engineer", "2020-01")
    incoming_pos = incoming_pos.model_copy(
        update={
            "location": "NYC",
            "end_date": "2022-03",
        }
    )
    incoming = ResumeData(experience=[incoming_pos])
    proposals = generate_proposals(
        incoming,
        existing,
        {"full_name": None, "headline": None, "location": None, "summary": None},
        page_is_new=False,
    )
    exp = [p for p in proposals if p.section == "experience"]
    assert len(exp) == 1
    assert exp[0].kind == "update"
    # Update payload carries the new structural fields; narrative-
    # preserved fields are NOT in the payload (apply() reads them
    # from the existing row).
    assert exp[0].payload["new"]["location"] == "NYC"
    assert exp[0].payload["new"]["end_date"] == "2022-03"
    assert "description_markdown" not in exp[0].payload["new"]


# ----------------------- skills proposals ---------------------


def test_skills_add_proposal_lists_new_names() -> None:
    incoming = ResumeData()  # parser doesn't put skills in incoming.skills;
    # incoming for skills is passed separately
    existing = ResumeData(skills=[SkillGroup(group_label="Skills", items=["Python"])])
    proposals = generate_proposals(
        incoming,
        existing,
        {"full_name": None, "headline": None, "location": None, "summary": None},
        page_is_new=False,
        incoming_skills=["Python", "Go", "Rust"],
    )
    skills_props = [p for p in proposals if p.section == "skills"]
    add_prop = next(p for p in skills_props if p.kind == "add")
    assert sorted(add_prop.payload["names"]) == ["Go", "Rust"]


def test_skills_remove_proposal_lists_missing_names() -> None:
    existing = ResumeData(skills=[SkillGroup(group_label="Skills", items=["Python", "Rust"])])
    proposals = generate_proposals(
        ResumeData(),
        existing,
        {"full_name": None, "headline": None, "location": None, "summary": None},
        page_is_new=False,
        incoming_skills=["Python"],
    )
    skills_props = [p for p in proposals if p.section == "skills"]
    remove_prop = next(p for p in skills_props if p.kind == "remove")
    assert remove_prop.payload["names"] == ["Rust"]


# ----------------------- skills merge algorithm ---------------


def test_skills_merge_preserves_groupings_on_existing_skills() -> None:
    existing_skills = [
        SkillGroup(group_label="Languages", items=["Python", "Go"]),
        SkillGroup(group_label="Tools", items=["Postgres", "k8s"]),
    ]
    incoming = ["Python", "Go", "Postgres", "k8s"]
    merged = apply_skills_merge(existing_skills, incoming)
    labels = [g.group_label for g in merged]
    assert labels == ["Languages", "Tools"]
    assert merged[0].items == ["Python", "Go"]
    assert merged[1].items == ["Postgres", "k8s"]


def test_skills_merge_appends_new_skills_to_first_group() -> None:
    existing_skills = [
        SkillGroup(group_label="Languages", items=["Python"]),
        SkillGroup(group_label="Tools", items=["k8s"]),
    ]
    incoming = ["Python", "Rust", "k8s", "Terraform"]
    merged = apply_skills_merge(existing_skills, incoming)
    assert merged[0].items == ["Python", "Rust", "Terraform"]
    assert merged[1].items == ["k8s"]


def test_skills_merge_drops_skills_no_longer_in_csv() -> None:
    existing_skills = [
        SkillGroup(group_label="Languages", items=["Python", "Perl"]),
    ]
    incoming = ["Python"]
    merged = apply_skills_merge(existing_skills, incoming)
    assert merged[0].items == ["Python"]


def test_skills_merge_drops_empty_groups() -> None:
    existing_skills = [
        SkillGroup(group_label="Old", items=["X"]),
        SkillGroup(group_label="Keep", items=["Y"]),
    ]
    incoming = ["Y"]
    merged = apply_skills_merge(existing_skills, incoming)
    assert len(merged) == 1
    assert merged[0].group_label == "Keep"


def test_skills_merge_fresh_creates_single_skills_group() -> None:
    merged = apply_skills_merge([], ["Python", "Go"])
    assert len(merged) == 1
    assert merged[0].group_label == "Skills"
    assert merged[0].items == ["Python", "Go"]


# ----------------------- proposal id stability ----------------


def test_proposal_ids_are_deterministic_for_same_input() -> None:
    existing = ResumeData()
    incoming = ResumeData(experience=[_position("Acme", "Engineer", "2020-01")])
    profile = {"full_name": "X", "headline": None, "location": None, "summary": None}
    a = generate_proposals(incoming, existing, profile, page_is_new=True)
    b = generate_proposals(incoming, existing, profile, page_is_new=True)
    assert [p.id for p in a] == [p.id for p in b]
