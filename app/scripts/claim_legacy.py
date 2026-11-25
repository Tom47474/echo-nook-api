"""开发环境一次性把旧版无归属会话绑定到已创建的本机身份。"""

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import delete, func, select, update

from app.conversations.models import Conversation, Message, ModelCall, Principal
from app.core.config import Settings
from app.db.session import DatabaseManager


async def claim(owner_id: UUID, apply: bool) -> None:
    settings = Settings()
    if settings.app_env != "development":
        raise RuntimeError("旧会话认领命令只允许在 development 环境运行")
    manager = DatabaseManager(settings.active_database_url())
    try:
        async with manager.session() as session:
            owner = await session.get(Principal, owner_id)
            if owner is None:
                raise ValueError("目标身份不存在；先在浏览器打开页面建立游客身份")
            count = await session.scalar(select(func.count()).select_from(Conversation).where(Conversation.owner_id.is_(None)))
            print(f"待认领的无归属会话：{count or 0}；目标身份类型：{owner.kind}")
            if apply and count:
                # 身份锁与 current() 共用，防止认领时另一个请求创建当前窗口。
                await session.scalar(select(Principal.id).where(Principal.id == owner_id).with_for_update())
                legacy = list((await session.scalars(
                    select(Conversation).where(Conversation.owner_id.is_(None))
                    .order_by(Conversation.created_at, Conversation.id).with_for_update()
                )).all())
                if not legacy:
                    print("无剩余待认领会话。")
                    return
                current = await session.scalar(select(Conversation).where(
                    Conversation.owner_id == owner_id, Conversation.is_current.is_(True)
                ).with_for_update())
                target = current or legacy[-1]
                source_ids = [item.id for item in legacy]
                if current:
                    source_ids.insert(0, current.id)
                messages = list((await session.scalars(select(Message).where(
                    Message.conversation_id.in_(source_ids)
                ).order_by(Message.created_at, Message.conversation_id, Message.sequence, Message.id))).all())
                # 先换成全局唯一负序号，再迁入当前窗口并从零重新编号。
                for index, message in enumerate(messages):
                    await session.execute(update(Message).where(Message.id == message.id).values(sequence=-(index + 1)))
                for index, message in enumerate(messages):
                    await session.execute(update(Message).where(Message.id == message.id).values(
                        conversation_id=target.id, sequence=index
                    ))
                other_ids = [item.id for item in legacy if item.id != target.id]
                if other_ids:
                    await session.execute(update(ModelCall).where(ModelCall.conversation_id.in_(other_ids)).values(conversation_id=target.id))
                    await session.execute(delete(Conversation).where(Conversation.id.in_(other_ids)))
                target.owner_id = owner_id
                target.is_current = True
                # 合并后的旧摘要边界无效；增加版本以拒绝在途旧摘要回写。
                target.summary = ""
                target.summary_through_sequence = -1
                target.content_epoch += 1
                await session.commit()
                print("已完成认领，旧消息已并入目标身份当前窗口。")
            elif not apply:
                print("预览模式；确认目标身份后加 --apply 执行。")
    finally:
        await manager.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="将开发库中旧版无归属会话绑定到当前身份")
    parser.add_argument("--owner", type=UUID, required=True, help="身份初始化接口返回的 id")
    parser.add_argument("--apply", action="store_true", help="确认后实际修改；默认仅预览")
    args = parser.parse_args()
    asyncio.run(claim(args.owner, args.apply))


if __name__ == "__main__":
    main()
