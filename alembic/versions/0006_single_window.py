"""为每个身份建立唯一当前窗口，并合并此前的多个会话。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0006_single_window"
down_revision: str | Sequence[str] | None = "0005_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("is_current", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("conversations", sa.Column("content_epoch", sa.Integer(), server_default="0", nullable=False))
    connection = op.get_bind()
    owners = connection.execute(sa.text("SELECT DISTINCT owner_id FROM conversations WHERE owner_id IS NOT NULL")).scalars()
    for owner_id in owners:
        conversations = list(connection.execute(sa.text(
            "SELECT id FROM conversations WHERE owner_id = :owner ORDER BY created_at, id"
        ), {"owner": owner_id}).scalars())
        target = conversations[-1]
        if len(conversations) > 1:
            # 先用负序号腾出唯一键，再按原会话和原消息顺序写入一个窗口。
            messages = list(connection.execute(sa.text(
                "SELECT id FROM messages WHERE conversation_id = ANY(:ids) "
                "ORDER BY created_at, conversation_id, sequence, id"
            ), {"ids": conversations}).scalars())
            for index, message_id in enumerate(messages):
                connection.execute(sa.text("UPDATE messages SET sequence = :sequence WHERE id = :id"),
                                   {"sequence": -(index + 1), "id": message_id})
            for index, message_id in enumerate(messages):
                connection.execute(sa.text(
                    "UPDATE messages SET conversation_id = :target, sequence = :sequence WHERE id = :id"
                ), {"target": target, "sequence": index, "id": message_id})
            connection.execute(sa.text(
                "UPDATE model_calls SET conversation_id = :target WHERE conversation_id = ANY(:ids)"
            ), {"target": target, "ids": conversations[:-1]})
            connection.execute(sa.text("DELETE FROM conversations WHERE id = ANY(:ids)"),
                               {"ids": conversations[:-1]})
            # 多条摘要的覆盖边界无法直接拼接；下次生成时重新摘要合并后的原文。
            connection.execute(sa.text(
                "UPDATE conversations SET summary = '', summary_through_sequence = -1 WHERE id = :id"
            ), {"id": target})
        connection.execute(sa.text("UPDATE conversations SET is_current = true WHERE id = :id"), {"id": target})
    op.create_index("uq_conversations_current_owner", "conversations", ["owner_id"], unique=True,
                    postgresql_where=sa.text("is_current = true"))


def downgrade() -> None:
    # 合并后的消息不能无损拆回原会话；回滚仅移除当前窗口标记和约束。
    op.drop_index("uq_conversations_current_owner", table_name="conversations")
    op.drop_column("conversations", "content_epoch")
    op.drop_column("conversations", "is_current")
