"""Contrib tests for the LinkedIn importer.

End-to-end small surface: detect, plan, apply against the live
schema + DB session. Uses in-memory ZIPs built per test so each
scenario is self-contained.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from bragi.api import Position, ResumeData
from bragi.contrib.import_linkedin.importer import apply, detect, plan
from bragi.core.models.page import Page, PageKind, PageStatus
from tests.conftest import make_test_site, make_test_user


def _build_zip(tmp_path: Path, files: dict[str, str]) -> Path:
    out = tmp_path / "export.zip"
    with zipfile.ZipFile(out, "w") as zf:
        for n, b in files.items():
            zf.writestr(n, b)
    return out


def _make_resume_page(
    db_session: Session,
    site: object,
    user: object,
    *,
    slug: str = "cv",
    title: str = "CV",
    resume_data: dict | None = None,
    body_markdown: str = "",
) -> Page:
    p = Page(
        site_id=site.id,  # type: ignore[attr-defined]
        slug=slug,
        title=title,
        author_id=user.id,  # type: ignore[attr-defined]
        status=PageStatus.PUBLISHED,
        kind=PageKind.RESUME,
        body_markdown=body_markdown,
        body_html="",
        body_excerpt="",
        resume_data=resume_data,
    )
    db_session.add(p)
    db_session.commit()
    return p


# ----------------------- detect -----------------------


def test_detect_accepts_real_shape(tmp_path: Path) -> None:
    zp = _build_zip(
        tmp_path,
        {
            "Profile.csv": "First Name,Last Name\nA,B\n",
            "Positions.csv": "Company Name,Title\nAcme,Engineer\n",
        },
    )
    assert detect(zp)


def test_detect_rejects_unrelated_zip(tmp_path: Path) -> None:
    zp = _build_zip(tmp_path, {"random.csv": "a,b\n1,2\n"})
    assert not detect(zp)


def test_detect_rejects_non_zip(tmp_path: Path) -> None:
    np = tmp_path / "plain.txt"
    np.write_text("not a zip", encoding="utf-8")
    assert not detect(np)


# ----------------------- plan + apply happy path -----


def test_plan_then_apply_populates_resume_data(
    tmp_path: Path, patched_session_locals: sessionmaker, db_session: Session
) -> None:
    site = make_test_site(
        db_session,
        hostname="t.example",
        title="T",
        slug="t",
        canonical_url="https://t.example",
    )
    user = make_test_user(db_session)
    page = _make_resume_page(db_session, site, user)
    zp = _build_zip(
        tmp_path,
        {
            "Profile.csv": (
                "First Name,Last Name,Headline,Geo Location,Summary\n"
                "Eelco,W,Engineer,Amsterdam,Bio\n"
            ),
            "Positions.csv": (
                "Company Name,Title,Started On,Finished On,Description\n"
                "Acme,Engineer,Jan 2020,Mar 2022,Built X.\n"
            ),
            "Education.csv": (
                "School Name,Degree Name,Start Date,End Date,Notes,Activities\n"
                "TUD,BSc,Sep 2010,Jul 2014,,\n"
            ),
            "Skills.csv": "Name\nPython\nGo\n",
            "Languages.csv": "Name,Proficiency\nDutch,Native\n",
            "Certifications.csv": "Name,Authority,Started On,Url\nCKA,LF,Mar 2022,\n",
            "Projects.csv": (
                "Title,Url,Started On,Finished On,Description\n" "Mimir,,Jan 2024,,LKML.\n"
            ),
        },
    )

    p = plan(zp, page)
    assert len(p.proposals) > 0

    selected = {pr.id for pr in p.proposals}
    apply(zp, page, {"selected_change_ids": selected})

    db_session.refresh(page)
    rd = ResumeData.model_validate(page.resume_data)
    assert len(rd.experience) == 1
    assert rd.experience[0].company == "Acme"
    assert rd.experience[0].description_markdown == "Built X."
    assert len(rd.education) == 1
    assert rd.education[0].institution == "TUD"
    assert rd.skills and rd.skills[0].items == ["Python", "Go"]
    assert rd.languages and rd.languages[0].name == "Dutch"
    assert rd.certifications and rd.certifications[0].year == 2022
    assert rd.projects and rd.projects[0].name == "Mimir"


# ----------------------- skip subset preserves the rest -----


def test_apply_with_subset_skips_unselected_changes(
    tmp_path: Path, patched_session_locals: sessionmaker, db_session: Session
) -> None:
    site = make_test_site(
        db_session,
        hostname="t2.example",
        title="T2",
        slug="t2",
        canonical_url="https://t2.example",
    )
    user = make_test_user(db_session)
    page = _make_resume_page(db_session, site, user)
    zp = _build_zip(
        tmp_path,
        {
            "Profile.csv": "First Name,Last Name\nA,B\n",
            "Positions.csv": ("Company Name,Title,Started On\n" "Acme,Engineer,Jan 2020\n"),
            "Skills.csv": "Name\nPython\nGo\n",
        },
    )

    p = plan(zp, page)
    # Only apply skills add; skip the position add.
    selected = {pr.id for pr in p.proposals if pr.section == "skills"}
    apply(zp, page, {"selected_change_ids": selected})

    db_session.refresh(page)
    rd = ResumeData.model_validate(page.resume_data)
    assert rd.experience == []  # not applied
    assert rd.skills and rd.skills[0].items == ["Python", "Go"]


# ----------------------- narrative preserve on re-import ----


def test_reimport_preserves_position_description_markdown(
    tmp_path: Path, patched_session_locals: sessionmaker, db_session: Session
) -> None:
    site = make_test_site(
        db_session,
        hostname="t3.example",
        title="T3",
        slug="t3",
        canonical_url="https://t3.example",
    )
    user = make_test_user(db_session)
    # Pre-existing page with a narrative already written by the operator.
    existing = ResumeData(
        experience=[
            Position(
                id="abc111",
                company="Acme",
                role="Engineer",
                start_date="2020-01",
                description_markdown="Custom narrative the operator wrote.",
                impacts=["Shipped X"],
            ),
        ]
    )
    page = _make_resume_page(
        db_session,
        site,
        user,
        resume_data=existing.model_dump(mode="json"),
    )
    # Incoming ZIP has the SAME position but a different LinkedIn
    # description (we should NOT overwrite the operator's prose).
    zp = _build_zip(
        tmp_path,
        {
            "Profile.csv": "First Name,Last Name\nA,B\n",
            "Positions.csv": (
                "Company Name,Title,Started On,Description\n"
                "Acme,Engineer,Jan 2020,LinkedIn-side description\n"
            ),
        },
    )

    p = plan(zp, page)
    selected = {pr.id for pr in p.proposals}
    apply(zp, page, {"selected_change_ids": selected})

    db_session.refresh(page)
    rd = ResumeData.model_validate(page.resume_data)
    assert len(rd.experience) == 1
    assert rd.experience[0].description_markdown == "Custom narrative the operator wrote."
    assert rd.experience[0].impacts == ["Shipped X"]
    assert rd.experience[0].id == "abc111"  # id preserved across re-import


# ----------------------- source_id + source_meta ----


def test_apply_writes_source_id_and_source_meta(
    tmp_path: Path, patched_session_locals: sessionmaker, db_session: Session
) -> None:
    site = make_test_site(
        db_session,
        hostname="t4.example",
        title="T4",
        slug="t4",
        canonical_url="https://t4.example",
    )
    user = make_test_user(db_session)
    page = _make_resume_page(db_session, site, user, slug="cv")
    zp = _build_zip(
        tmp_path,
        {
            "Profile.csv": "First Name,Last Name\nA,B\n",
            "Skills.csv": "Name\nPython\n",
        },
    )

    p = plan(zp, page)
    apply(zp, page, {"selected_change_ids": {pr.id for pr in p.proposals}})

    db_session.refresh(page)
    assert page.source_id == "linkedin:cv"
    assert page.source_meta is not None
    assert "linkedin_export_filename" in page.source_meta
    assert "imported_at" in page.source_meta
    assert "applied_change_ids" in page.source_meta
