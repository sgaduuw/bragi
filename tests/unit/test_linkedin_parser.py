"""Unit tests for the LinkedIn CSV parser layer."""

from __future__ import annotations

import io
import zipfile

from bragi.contrib.import_linkedin.parser import (
    clean_linkedin_description,
    parse_certifications,
    parse_education,
    parse_languages,
    parse_positions,
    parse_profile,
    parse_projects,
    parse_skills,
    parse_year_month,
)


def test_parse_year_month_short_month() -> None:
    assert parse_year_month("Apr 2024") == "2024-04"


def test_parse_year_month_long_month() -> None:
    assert parse_year_month("January 2020") == "2020-01"


def test_parse_year_month_blank() -> None:
    assert parse_year_month("") is None
    assert parse_year_month("   ") is None


def test_parse_year_month_none_input() -> None:
    assert parse_year_month(None) is None


def test_parse_year_month_unparseable() -> None:
    assert parse_year_month("Spring 2024") is None
    assert parse_year_month("2024") is None
    assert parse_year_month("garbage") is None


def test_parse_year_month_extra_whitespace() -> None:
    assert parse_year_month("  Apr 2024  ") == "2024-04"


def test_parse_year_month_december_short() -> None:
    assert parse_year_month("Dec 2023") == "2023-12"


def test_parse_year_month_september_long() -> None:
    assert parse_year_month("September 2021") == "2021-09"


def _zip_with(files: dict[str, str]) -> zipfile.ZipFile:
    """Build an in-memory ZIP containing the given file contents.

    Returns an OPEN ZipFile bound to a BytesIO so the parser can
    `.open(name)` against it the same way it would for a real
    on-disk ZIP.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


# ----------------------- Profile -----------------------


def test_parse_profile_minimal() -> None:
    zf = _zip_with(
        {
            "Profile.csv": (
                "First Name,Last Name,Headline,Geo Location,Summary\n"
                "Eelco,Wesemann,Senior Engineer,Amsterdam,Engineer and writer.\n"
            ),
        }
    )
    profile = parse_profile(zf)
    assert profile["full_name"] == "Eelco Wesemann"
    assert profile["headline"] == "Senior Engineer"
    assert profile["location"] == "Amsterdam"
    assert profile["summary"] == "Engineer and writer."


def test_parse_profile_missing_columns_yield_none() -> None:
    zf = _zip_with(
        {
            "Profile.csv": "First Name,Last Name\nAda,Lovelace\n",
        }
    )
    profile = parse_profile(zf)
    assert profile["full_name"] == "Ada Lovelace"
    assert profile["headline"] is None
    assert profile["location"] is None
    assert profile["summary"] is None


def test_parse_profile_no_csv_returns_empty() -> None:
    zf = _zip_with({"Other.csv": "a,b\n1,2\n"})
    assert parse_profile(zf) == {
        "full_name": None,
        "headline": None,
        "location": None,
        "summary": None,
    }


# ----------------------- Positions -----------------------


def test_parse_positions_orders_descending_by_start() -> None:
    zf = _zip_with(
        {
            "Positions.csv": (
                "Company Name,Title,Location,Started On,Finished On,Description\n"
                "Acme,Engineer,NYC,Jan 2018,Dec 2019,first job\n"
                "Beta,Senior Engineer,Berlin,Apr 2024,,current\n"
                "Acme,Lead Engineer,NYC,Jan 2020,Mar 2022,mid\n"
            ),
        }
    )
    positions = parse_positions(zf)
    assert [p.start_date for p in positions] == ["2024-04", "2020-01", "2018-01"]
    assert positions[0].end_date is None  # current job


def test_parse_positions_preserves_description_as_narrative() -> None:
    zf = _zip_with(
        {
            "Positions.csv": (
                "Company Name,Title,Location,Started On,Finished On,Description\n"
                "Acme,Engineer,NYC,Jan 2018,Dec 2019,Built the X platform.\n"
            ),
        }
    )
    [p] = parse_positions(zf)
    assert p.description_markdown == "Built the X platform."
    assert p.impacts == []


def test_parse_positions_none_start_date_sorts_last() -> None:
    zf = _zip_with(
        {
            "Positions.csv": (
                "Company Name,Title,Location,Started On,Finished On,Description\n"
                "Acme,Engineer,NYC,Jan 2020,Mar 2022,with date\n"
                "Old,Intern,Anywhere,,Dec 2010,no start\n"
                "Beta,Senior Engineer,Berlin,Apr 2024,,current\n"
            ),
        }
    )
    positions = parse_positions(zf)
    # Dated rows come first (desc), undated rows last
    assert [p.company for p in positions] == ["Beta", "Acme", "Old"]


def test_parse_positions_empty_csv_returns_empty_list() -> None:
    zf = _zip_with({"Other.csv": "a\n"})
    assert parse_positions(zf) == []


# ----------------------- Education -----------------------


def test_parse_education_basic() -> None:
    zf = _zip_with(
        {
            "Education.csv": (
                "School Name,Degree Name,Start Date,End Date,Notes,Activities\n"
                "TU Delft,BSc Computer Science,Sep 2010,Jul 2014,Thesis on X,Hackathons\n"
            ),
        }
    )
    [e] = parse_education(zf)
    assert e.institution == "TU Delft"
    assert e.degree == "BSc Computer Science"
    assert e.start_date == "2010-09"
    assert e.end_date == "2014-07"
    assert "Thesis on X" in e.description_markdown
    assert "Hackathons" in e.description_markdown


def test_parse_education_blank_degree_uses_fallback() -> None:
    zf = _zip_with(
        {
            "Education.csv": (
                "School Name,Degree Name,Start Date,End Date,Notes,Activities\n"
                "TU Delft,,Sep 2010,Jul 2014,,\n"
            ),
        }
    )
    [e] = parse_education(zf)
    assert e.degree == "(unspecified)"


# ----------------------- Skills -----------------------


def test_parse_skills_returns_flat_list() -> None:
    zf = _zip_with(
        {
            "Skills.csv": "Name\nPython\nGo\nRust\n",
        }
    )
    assert parse_skills(zf) == ["Python", "Go", "Rust"]


def test_parse_skills_empty_csv_returns_empty_list() -> None:
    zf = _zip_with({"Other.csv": "a\n"})
    assert parse_skills(zf) == []


# ----------------------- Languages -----------------------


def test_parse_languages_basic() -> None:
    zf = _zip_with(
        {
            "Languages.csv": (
                "Name,Proficiency\n" "Dutch,Native\n" "English,Professional working\n"
            ),
        }
    )
    langs = parse_languages(zf)
    assert [(lang.name, lang.level) for lang in langs] == [
        ("Dutch", "Native"),
        ("English", "Professional working"),
    ]


# ----------------------- Certifications -----------------------


def test_parse_certifications_extracts_year() -> None:
    zf = _zip_with(
        {
            "Certifications.csv": (
                "Name,Authority,Started On,Url\n"
                "CKA,The Linux Foundation,Mar 2022,https://example/cert\n"
            ),
        }
    )
    [c] = parse_certifications(zf)
    assert c.name == "CKA"
    assert c.issuer == "The Linux Foundation"
    assert c.year == 2022
    assert str(c.url) == "https://example/cert"


def test_parse_certifications_unparseable_date_drops_year() -> None:
    zf = _zip_with(
        {
            "Certifications.csv": (
                "Name,Authority,Started On,Url\n" "Course,Provider,Spring 2022,\n"
            ),
        }
    )
    [c] = parse_certifications(zf)
    assert c.year is None


# ----------------------- Projects -----------------------


def test_parse_projects_basic() -> None:
    zf = _zip_with(
        {
            "Projects.csv": (
                "Title,Url,Started On,Finished On,Description\n"
                "Mimir,https://example/mimir,Jan 2024,,LKML indexer\n"
            ),
        }
    )
    [p] = parse_projects(zf)
    assert p.name == "Mimir"
    assert str(p.url) == "https://example/mimir"
    assert p.start_date == "2024-01"
    assert p.end_date is None
    assert p.description_markdown == "LKML indexer"


# ----------------------- clean_linkedin_description ----


def test_clean_linkedin_description_empty_input() -> None:
    assert clean_linkedin_description("") == ""
    assert clean_linkedin_description("   \n  ") == ""


def test_clean_linkedin_description_plain_text_unchanged() -> None:
    assert clean_linkedin_description("Built X.") == "Built X."


def test_clean_linkedin_description_normalises_crlf() -> None:
    assert clean_linkedin_description("a\r\nb\r\nc") == "a\nb\nc"
    assert clean_linkedin_description("a\rb") == "a\nb"


def test_clean_linkedin_description_converts_bullet_glyph() -> None:
    raw = "Key wins:\n• Shipped X\n• Led Y"
    expected = "Key wins:\n- Shipped X\n- Led Y"
    assert clean_linkedin_description(raw) == expected


def test_clean_linkedin_description_accepts_multiple_bullet_glyphs() -> None:
    raw = "• a\n‣ b\n▪ c\n◦ d"
    expected = "- a\n- b\n- c\n- d"
    assert clean_linkedin_description(raw) == expected


def test_clean_linkedin_description_handles_bullet_without_space() -> None:
    raw = "•item one\n•item two"
    expected = "- item one\n- item two"
    assert clean_linkedin_description(raw) == expected


def test_clean_linkedin_description_preserves_indented_bullets() -> None:
    raw = "Main:\n  • Sub a\n  • Sub b"
    expected = "Main:\n  - Sub a\n  - Sub b"
    assert clean_linkedin_description(raw) == expected


def test_clean_linkedin_description_preserves_paragraph_breaks() -> None:
    raw = "First paragraph.\n\nSecond paragraph."
    assert clean_linkedin_description(raw) == raw


def test_clean_linkedin_description_strips_trailing_whitespace_per_line() -> None:
    raw = "line one   \nline two\t\n"
    assert clean_linkedin_description(raw) == "line one\nline two"


def test_clean_linkedin_description_does_not_touch_inline_bullet_glyph() -> None:
    # Mid-line bullets used as ornamental separators must be left alone.
    raw = "Foo • Bar • Baz"
    assert clean_linkedin_description(raw) == "Foo • Bar • Baz"


def test_clean_linkedin_description_full_example() -> None:
    raw = (
        "Built the X platform from scratch.\r\n"
        "\r\n"
        "Key responsibilities:\r\n"
        "• Designed the architecture\r\n"
        "• Led a team of 5 engineers\r\n"
        "• Shipped v1 in 6 months\r\n"
        "\r\n"
        "Then got the team to ship faster.\r\n"
    )
    expected = (
        "Built the X platform from scratch.\n"
        "\n"
        "Key responsibilities:\n"
        "- Designed the architecture\n"
        "- Led a team of 5 engineers\n"
        "- Shipped v1 in 6 months\n"
        "\n"
        "Then got the team to ship faster."
    )
    assert clean_linkedin_description(raw) == expected


# ----------------------- cleanup applies in parsers -----


def test_parse_positions_cleans_bulleted_description() -> None:
    zf = _zip_with(
        {
            "Positions.csv": (
                "Company Name,Title,Location,Started On,Finished On,Description\n"
                'Acme,Engineer,NYC,Jan 2018,Dec 2019,"Built X.\n\n• Shipped Y\n• Led Z"\n'
            ),
        }
    )
    [p] = parse_positions(zf)
    assert "- Shipped Y" in p.description_markdown
    assert "- Led Z" in p.description_markdown
    assert "• " not in p.description_markdown


def test_parse_projects_cleans_bulleted_description() -> None:
    zf = _zip_with(
        {
            "Projects.csv": (
                "Title,Url,Started On,Finished On,Description\n"
                'Mimir,,Jan 2024,,"LKML indexer\n• fast\n• indexed"\n'
            ),
        }
    )
    [p] = parse_projects(zf)
    assert "- fast" in p.description_markdown
    assert "- indexed" in p.description_markdown


def test_parse_education_cleans_joined_notes_and_activities() -> None:
    zf = _zip_with(
        {
            "Education.csv": (
                "School Name,Degree Name,Start Date,End Date,Notes,Activities\n"
                'TUD,BSc,Sep 2010,Jul 2014,"Thesis on X\n• prize-winning",'
                '"Hackathons\n• demo team"\n'
            ),
        }
    )
    [e] = parse_education(zf)
    # Both notes and activities should end up cleaned.
    assert "- prize-winning" in e.description_markdown
    assert "- demo team" in e.description_markdown


def test_parse_profile_cleans_summary() -> None:
    zf = _zip_with(
        {
            "Profile.csv": (
                "First Name,Last Name,Summary\n" 'Eelco,W,"Engineer\n• Loves Python\n• Hates JS"\n'
            ),
        }
    )
    profile = parse_profile(zf)
    assert "- Loves Python" in (profile["summary"] or "")
    assert "• " not in (profile["summary"] or "")
