"""add_search_fts

Revision ID: 1eff692ffe2b
Revises: 17c7f26e8fde
Create Date: 2026-05-14 20:49:29.026859+00:00

Adds two FTS5 virtual tables (`posts_fts`, `pages_fts`) used by
`bragi.contrib.search`. The rowid is set to the corresponding
`posts.id` / `pages.id` at index time so we can JOIN back to
filter by site_id and status at query time.

Tables are NOT contentless: FTS5's `snippet()` auxiliary function
requires access to the source text, which contentless tables don't
keep. A personal-blog corpus is at the low end of any reasonable
storage budget, so duplicating the body text into the FTS index
costs nothing meaningful and buys us the `<mark>`-decorated
snippets that make search results useful.

Tokenizer is `porter unicode61`: Unicode normalisation across all
scripts plus English stemming. Per-locale tokenizers per row are
a documented future gap; the issue thread on #43 captures the
rationale.

Downgrade drops both tables. The migration is reversible and
covered by the standard upgrade-downgrade-upgrade smoke.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1eff692ffe2b"
down_revision: str | None = "17c7f26e8fde"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The four column names mirror what `SQLiteFTS5SearchBackend.index()`
    # writes into; reorder here only if the backend's INSERT changes
    # shape too.
    op.execute(
        """
        CREATE VIRTUAL TABLE posts_fts USING fts5(
            title,
            body,
            meta_description,
            excerpt,
            tokenize='porter unicode61'
        )
        """
    )
    op.execute(
        """
        CREATE VIRTUAL TABLE pages_fts USING fts5(
            title,
            body,
            meta_description,
            excerpt,
            tokenize='porter unicode61'
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE pages_fts")
    op.execute("DROP TABLE posts_fts")
