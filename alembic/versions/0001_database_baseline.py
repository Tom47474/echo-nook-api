"""Establish the database migration baseline.

Revision ID: 0001_database_baseline
Revises: None
Create Date: 2026-11-23
"""

from collections.abc import Sequence

revision: str = "0001_database_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Business tables are introduced in S2-2; this revision proves that the
    # migration chain can be reproduced before schema design begins.
    pass


def downgrade() -> None:
    pass
