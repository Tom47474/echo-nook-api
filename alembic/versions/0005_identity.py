"""建立游客、账号及服务端会话身份表。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0005_identity"
down_revision: str | Sequence[str] | None = "0004_model_calls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "principals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("username", sa.String(100), unique=True, nullable=True),
        sa.Column("password_hash", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "identity_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("principal_id", sa.Uuid(), sa.ForeignKey("principals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_identity_sessions_principal_id", "identity_sessions", ["principal_id"])
    # 历史会话保留原样；没有可信凭证时不能自动把 NULL 归属给任一新访客。
    op.create_foreign_key("fk_conversations_owner_id", "conversations", "principals", ["owner_id"], ["id"])
    op.add_column("model_calls", sa.Column("usage_owner_id", sa.Uuid(), nullable=True))
    op.add_column("model_calls", sa.Column("usage_owner_kind", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("model_calls", "usage_owner_kind")
    op.drop_column("model_calls", "usage_owner_id")
    op.drop_constraint("fk_conversations_owner_id", "conversations", type_="foreignkey")
    op.drop_index("ix_identity_sessions_principal_id", table_name="identity_sessions")
    op.drop_table("identity_sessions")
    op.drop_table("principals")
