"""add model-call quota lookup index

Revision ID: 0007_s5_limits
Revises: 0006_single_window
Create Date: 2026-11-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_s5_limits"
down_revision: str | None = "0006_single_window"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为按身份统计 UTC 当日主回复次数建立复合索引。"""
    op.create_index(
        "ix_model_calls_usage_owner_purpose_created",
        "model_calls",
        ["usage_owner_id", "purpose", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """回滚 S5-2 的额度查询索引。"""
    op.drop_index("ix_model_calls_usage_owner_purpose_created", table_name="model_calls")
