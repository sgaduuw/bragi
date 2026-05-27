"""Schema.org JSON-LD builder for resume pages.

Composes a Person block with nested Organization (worksFor),
EducationalOrganization (alumniOf), EducationalOccupationalCredential
(hasCredential), and knowsLanguage entries from a validated
ResumeData. The builder is a pure function -- no Flask context, no
DB -- so it's unit-testable in isolation.

The delivery template embeds the result as
`<script type="application/ld+json">{{ jsonld | tojson }}</script>`
in the `social_meta` block.

Why also emit inline `itemprop` microdata in the template (in
addition to this JSON-LD)? Redundant by design: inline microdata is
what older / heuristic crawlers parse; JSON-LD is what Google's
structured-data pipeline prefers today. Both come from the same
ResumeData object so they can't drift.
"""

from __future__ import annotations

from typing import Any

from bragi.contrib.page.resume import (
    Position,
    ResumeData,
)


def build_jsonld(*, page_title: str, resume: ResumeData) -> dict[str, Any]:
    """Build the schema.org Person JSON-LD for a resume page.

    Empty / unset sections of ResumeData are omitted from the
    output, so a minimal resume (just a name) produces a minimal
    Person block.
    """
    jsonld: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": page_title,
    }

    if resume.header.tagline:
        jsonld["jobTitle"] = resume.header.tagline
    if resume.header.location:
        jsonld["address"] = resume.header.location
    if resume.header.profile_links:
        jsonld["sameAs"] = [str(link.url) for link in resume.header.profile_links]

    if resume.experience:
        jsonld["worksFor"] = [_position_to_org(p) for p in resume.experience]

    if resume.education:
        jsonld["alumniOf"] = [
            {
                "@type": "EducationalOrganization",
                "name": e.institution,
                "alumni": {
                    "@type": "EducationalOccupationalCredential",
                    "name": e.degree,
                },
            }
            for e in resume.education
        ]

    if resume.certifications:
        jsonld["hasCredential"] = [
            {
                "@type": "EducationalOccupationalCredential",
                "name": c.name,
                **({"recognizedBy": c.issuer} if c.issuer else {}),
                **({"dateCreated": str(c.year)} if c.year is not None else {}),
                **({"url": str(c.url)} if c.url else {}),
            }
            for c in resume.certifications
        ]

    if resume.languages:
        jsonld["knowsLanguage"] = [lang.name for lang in resume.languages]

    return jsonld


def _position_to_org(p: Position) -> dict[str, Any]:
    """Render one Position as a schema.org Organization with a nested
    OccupationalRole holding role / dates. End date omitted for
    'Present' (None) roles."""
    member: dict[str, Any] = {
        "@type": "OccupationalRole",
        "roleName": p.role,
    }
    if p.start_date:
        member["startDate"] = p.start_date
    if p.end_date:
        member["endDate"] = p.end_date

    org: dict[str, Any] = {
        "@type": "Organization",
        "name": p.company,
        "member": member,
    }
    if p.location:
        org["location"] = p.location
    return org
