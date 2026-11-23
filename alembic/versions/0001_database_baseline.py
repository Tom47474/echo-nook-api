"""建立数据库迁移基线。

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
    # 业务表由 S2-2 引入；空基线用于先验证迁移链可以重复执行。
    pass


def downgrade() -> None:
    pass
