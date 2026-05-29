# linkedin_sample.zip

A small, hand-crafted LinkedIn-shaped data export used by
contrib + integration tests. Anonymised content (Ada Lovelace
quasi-CV). Contains:

- Profile.csv (1 row)
- Positions.csv (2 rows: one completed, one current)
- Education.csv (1 row)
- Skills.csv (3 entries)
- Languages.csv (2 entries)
- Certifications.csv (header only, no rows)
- Projects.csv (1 row)
- Connections.csv (irrelevant; tests should ignore it)

To regenerate: run the shell snippet at the top of Task 15 in
`_claude/plans/2026-05-29-linkedin-importer.md`. Tests should
not depend on byte-exact ZIP contents; structure-level
assertions only.
