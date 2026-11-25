"""开发环境一次性把旧版无归属会话绑定到已创建的本机身份。"""

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import func, select, update

from app.conversations.models import Conversation, Principal
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
                # 只修改 NULL 归属；不覆盖已属于其他身份的会话。
                await session.execute(update(Conversation).where(Conversation.owner_id.is_(None)).values(owner_id=owner_id))
                await session.commit()
                print("已完成认领。")
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
