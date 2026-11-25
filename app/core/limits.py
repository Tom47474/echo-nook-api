"""对话额度、进程内并发和登录失败保护。"""

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from uuid import UUID

from sqlalchemy import func, select

from app.conversations.models import ModelCall
from app.core.config import Settings
from app.db.session import DatabaseManager


class LimitRejected(Exception):
    """携带可直接转换为 HTTP 错误的限额拒绝原因。"""

    def __init__(self, code: str, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass
class GenerationLease:
    """一次生成持有的进程内名额；关闭 SSE 时必须且只释放一次。"""

    manager: "ConversationLimitManager"
    identity_id: UUID
    limited: bool
    released: bool = False

    async def release(self) -> None:
        """幂等释放身份与全局并发名额。"""
        if self.released:
            return
        self.released = True
        if self.limited:
            await self.manager._release(self.identity_id)


class ConversationLimitManager:
    """应用级限额器；数据库负责日额度，内存负责短窗口与在途请求。"""

    def __init__(self, settings: Settings, database: DatabaseManager) -> None:
        self.settings = settings
        self.database = database
        self._lock = asyncio.Lock()
        self._active_identities: set[UUID] = set()
        self._active_total = 0
        self._minute_requests: dict[UUID, deque[float]] = defaultdict(deque)
        self._login_failures: dict[str, deque[float]] = defaultdict(deque)
        self._last_cleanup = monotonic()

    def _cleanup_expired(self, now: float) -> None:
        """定期删除不再有效的窗口，避免大量一次性身份或用户名长期占内存。"""
        if now - self._last_cleanup < 60:
            return
        for identity_id, requests in list(self._minute_requests.items()):
            while requests and now - requests[0] >= 60:
                requests.popleft()
            if not requests:
                self._minute_requests.pop(identity_id, None)
        window = self.settings.login_failure_window_seconds
        for key, failures in list(self._login_failures.items()):
            while failures and now - failures[0] >= window:
                failures.popleft()
            if not failures:
                self._login_failures.pop(key, None)
        self._last_cleanup = now

    def _limits_for(self, identity_kind: str) -> tuple[int, int]:
        """按身份类型返回（日回复数、每分钟请求数）。"""
        if identity_kind == "account":
            return (
                self.settings.conversation_account_daily_limit,
                self.settings.conversation_account_minute_limit,
            )
        return (
            self.settings.conversation_guest_daily_limit,
            self.settings.conversation_guest_minute_limit,
        )

    async def _daily_reply_count(self, identity_id: UUID) -> int:
        """读取 UTC 当天已开始并留有记录的主回复次数；清空消息不会重置。"""
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.database.session() as session:
            count = await session.scalar(
                select(func.count(ModelCall.id)).where(
                    ModelCall.usage_owner_id == identity_id,
                    ModelCall.purpose == "reply",
                    ModelCall.created_at >= day_start,
                )
            )
        return int(count or 0)

    async def acquire_generation(self, identity_id: UUID, identity_kind: str) -> GenerationLease:
        """在写入消息前校验日额度、分钟频率和并发，并返回流生命周期租约。"""
        if not self.settings.conversation_limit_enabled:
            return GenerationLease(self, identity_id, limited=False)

        daily_limit, minute_limit = self._limits_for(identity_kind)
        if await self._daily_reply_count(identity_id) >= daily_limit:
            raise LimitRejected(
                "conversation_daily_limit",
                "今日对话额度已用完，请明天再试",
            )

        now = monotonic()
        async with self._lock:
            self._cleanup_expired(now)
            requests = self._minute_requests[identity_id]
            while requests and now - requests[0] >= 60:
                requests.popleft()
            if len(requests) >= minute_limit:
                retry_after = max(1, round(60 - (now - requests[0])))
                raise LimitRejected(
                    "conversation_rate_limited",
                    "发送过于频繁，请稍后重试",
                    retry_after=retry_after,
                )
            if identity_id in self._active_identities:
                raise LimitRejected(
                    "conversation_in_progress",
                    "当前已有回复正在生成，请等待完成或先停止",
                )
            if self._active_total >= self.settings.conversation_global_concurrency_limit:
                raise LimitRejected(
                    "conversation_server_busy",
                    "当前使用人数较多，请稍后重试",
                    retry_after=3,
                )
            requests.append(now)
            self._active_identities.add(identity_id)
            self._active_total += 1
        return GenerationLease(self, identity_id, limited=True)

    async def _release(self, identity_id: UUID) -> None:
        """在锁内同步更新两个并发计数，防止取消和完成重复扣减。"""
        async with self._lock:
            if identity_id in self._active_identities:
                self._active_identities.remove(identity_id)
                self._active_total = max(0, self._active_total - 1)

    async def ensure_login_allowed(self, key: str) -> None:
        """密码校验前阻止同一来源与用户名组合继续暴力尝试。"""
        if not self.settings.conversation_limit_enabled:
            return
        now = monotonic()
        async with self._lock:
            self._cleanup_expired(now)
            failures = self._login_failures[key]
            window = self.settings.login_failure_window_seconds
            while failures and now - failures[0] >= window:
                failures.popleft()
            if len(failures) >= self.settings.login_failure_limit:
                retry_after = max(1, round(window - (now - failures[0])))
                raise LimitRejected(
                    "login_rate_limited",
                    "登录失败次数过多，请稍后重试",
                    retry_after=retry_after,
                )

    async def record_login_failure(self, key: str) -> None:
        """仅记录密码校验失败；参数校验错误和服务端错误不计入。"""
        if not self.settings.conversation_limit_enabled:
            return
        async with self._lock:
            now = monotonic()
            self._cleanup_expired(now)
            self._login_failures[key].append(now)

    async def clear_login_failures(self, key: str) -> None:
        """登录成功后清除该来源与账号的连续失败记录。"""
        if not self.settings.conversation_limit_enabled:
            return
        async with self._lock:
            self._login_failures.pop(key, None)
