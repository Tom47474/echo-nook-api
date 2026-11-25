"""为长对话保存滚动摘要及覆盖边界。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003_conversation_summary"
down_revision: str | Sequence[str] | None = "0002_conversation_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("summary", sa.Text(), server_default="", nullable=False))
    op.add_column("conversations", sa.Column("summary_through_sequence", sa.Integer(), server_default="-1", nullable=False))


def downgrade() -> None:
    op.drop_column("conversations", "summary_through_sequence")
    op.drop_column("conversations", "summary")
