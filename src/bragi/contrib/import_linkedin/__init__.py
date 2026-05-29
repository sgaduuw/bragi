"""Importer for LinkedIn "Download your data" export ZIPs.

Reads the seven resume-relevant CSVs in the export
(`Profile.csv`, `Positions.csv`, `Education.csv`, `Skills.csv`,
`Languages.csv`, `Certifications.csv`, `Projects.csv`) and
populates a Resume page's `resume_data`. Two-phase workflow:
plan() emits ChangeProposal instances; the operator approves a
subset via a JSON plan file (CLI) or a review page (admin UI);
apply() filters to approved ids and enacts the changes.

See the design spec at
`_claude/specs/2026-05-29-linkedin-importer-design.md` for the
full mapping and re-import semantics.

Plugin boundary (see `_claude/CLAUDE.md` Conventions): imports
only from `bragi.api`, `bragi.core`, `bragi.core.models`. The
resume model classes live in `bragi.api` (promoted from
`bragi.contrib.page.resume` for exactly this reason).
"""
