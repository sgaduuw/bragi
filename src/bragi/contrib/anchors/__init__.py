"""Heading-anchor HTML transform.

Walks rendered HTML and injects `id="<slug>"` on `<h1>` through
`<h6>` elements that don't already carry one. Slug derivation is
the standard "lowercase ASCII letters and digits, everything else
becomes a hyphen, runs of hyphens collapse, trim ends". Duplicate
slugs within a single document gain `-2`, `-3`, ... suffixes so
deep links remain unique.

Headings that contain only non-sluggable characters (e.g., a single
emoji or punctuation) are left without an id; an empty slug would
be useless as a deep-link target.
"""
