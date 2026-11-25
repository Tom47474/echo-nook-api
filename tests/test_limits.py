"""S5-2 对话额度、并发租约和登录失败保护。"""

import asyncio
import unittest
from uuid import UUID, uuid4
from unittest.mock import patch

import httpx
from sqlalchemy import delete, select

from app.conversations.models import Conversation, Message, ModelCall, Principal
from app.core.config import Settings
from app.core.limits import LimitRejected
from app.main import create_app, lifespan


class LimitApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        settings = Settings(app_env="test", conversation_limit_enabled=False)
        if not settings.test_database_url.get_secret_value():
            self.skipTest("TEST_DATABASE_URL is not configured")
        self.app = create_app(settings)
        self.lifespan = lifespan(self.app)
        await self.lifespan.__aenter__()
        transport = httpx.ASGITransport(app=self.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://test")
        bootstrap = await self.client.post("/api/v1/identity/bootstrap")
        self.identity_id = UUID(bootstrap.json()["id"])
        current = await self.client.get("/api/v1/conversations/current")
        self.conversation_id = UUID(current.json()["id"])

    async def asyncTearDown(self) -> None:
        async with self.app.state.database_manager.session() as session:
            await session.execute(delete(Conversation).where(Conversation.owner_id == self.identity_id))
            await session.execute(delete(Principal).where(Principal.id == self.identity_id))
            await session.commit()
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)

    def enable_limits(self, **updates: object) -> None:
        """测试内同时更新路由配置和应用级限额器读取的配置。"""
        settings = self.app.state.settings.model_copy(update={
            "conversation_limit_enabled": True,
            "conversation_guest_daily_limit": 20,
            "conversation_guest_minute_limit": 10,
            "conversation_global_concurrency_limit": 8,
            **updates,
        })
        self.app.state.settings = settings
        self.app.state.conversation_limit_manager.settings = settings

    async def test_guest_daily_limit_is_configurable_and_rejects_before_message_write(self) -> None:
        self.enable_limits(conversation_guest_daily_limit=1)
        async with self.app.state.database_manager.session() as session:
            session.add(ModelCall(
                conversation_id=self.conversation_id,
                purpose="reply",
                status="completed",
                usage_owner_id=self.identity_id,
                usage_owner_kind="guest",
            ))
            await session.commit()

        response = await self.client.post(
            f"/api/v1/conversations/{self.conversation_id}/messages:stream",
            json={"message": "不应写入"},
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["detail"]["code"], "conversation_daily_limit")
        async with self.app.state.database_manager.session() as session:
            stored = list((await session.scalars(
                select(Message).where(Message.conversation_id == self.conversation_id)
            )).all())
        self.assertEqual(stored, [])

    async def test_guest_and_account_use_different_configured_quotas(self) -> None:
        self.enable_limits(
            conversation_guest_daily_limit=2,
            conversation_account_daily_limit=7,
            conversation_guest_minute_limit=3,
            conversation_account_minute_limit=11,
        )
        manager = self.app.state.conversation_limit_manager
        self.assertEqual(manager._limits_for("guest"), (2, 3))
        self.assertEqual(manager._limits_for("account"), (7, 11))

    async def test_minute_limit_returns_retry_after_and_master_switch_can_disable_it(self) -> None:
        self.enable_limits(conversation_guest_minute_limit=1)

        class ImmediateProvider:
            async def stream_reply(self, messages, system_prompt):
                yield "完成"

        with patch("app.api.routes.conversations.create_llm_provider", return_value=ImmediateProvider()):
            first = await self.client.post(
                f"/api/v1/conversations/{self.conversation_id}/messages:stream",
                json={"message": "第一次"},
            )
            second = await self.client.post(
                f"/api/v1/conversations/{self.conversation_id}/messages:stream",
                json={"message": "第二次"},
            )
            self.app.state.settings.conversation_limit_enabled = False
            self.app.state.conversation_limit_manager.settings.conversation_limit_enabled = False
            unlimited = await self.client.post(
                f"/api/v1/conversations/{self.conversation_id}/messages:stream",
                json={"message": "关闭开关后"},
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["detail"]["code"], "conversation_rate_limited")
        self.assertIn("Retry-After", second.headers)
        self.assertEqual(unlimited.status_code, 200)

    async def test_one_identity_and_global_concurrency_are_released_by_lease(self) -> None:
        self.enable_limits(conversation_global_concurrency_limit=1)
        manager = self.app.state.conversation_limit_manager
        first = await manager.acquire_generation(self.identity_id, "guest")
        with self.assertRaises(LimitRejected) as same_identity:
            await manager.acquire_generation(self.identity_id, "guest")
        self.assertEqual(same_identity.exception.code, "conversation_in_progress")
        with self.assertRaises(LimitRejected) as global_limit:
            await manager.acquire_generation(uuid4(), "account")
        self.assertEqual(global_limit.exception.code, "conversation_server_busy")
        await first.release()
        replacement = await manager.acquire_generation(uuid4(), "account")
        await replacement.release()

    async def test_reply_timeout_marks_failure_and_releases_generation_lease(self) -> None:
        self.enable_limits(llm_reply_timeout_seconds=0.01)

        class SlowProvider:
            async def stream_reply(self, messages, system_prompt):
                await asyncio.sleep(1)
                yield "不会到达"

        class ImmediateProvider:
            async def stream_reply(self, messages, system_prompt):
                yield "恢复后的回答"

        with patch("app.api.routes.conversations.create_llm_provider", return_value=SlowProvider()):
            timed_out = await self.client.post(
                f"/api/v1/conversations/{self.conversation_id}/messages:stream",
                json={"message": "触发总超时"},
            )
        # 上一次 SSE 完成后，同一身份应已释放租约，可以立即再次发送。
        with patch("app.api.routes.conversations.create_llm_provider", return_value=ImmediateProvider()):
            recovered = await self.client.post(
                f"/api/v1/conversations/{self.conversation_id}/messages:stream",
                json={"message": "超时后再次发送"},
            )
        self.assertIn("provider_timeout", timed_out.text)
        self.assertIn("message.completed", recovered.text)
        async with self.app.state.database_manager.session() as session:
            assistants = list((await session.scalars(
                select(Message)
                .where(Message.conversation_id == self.conversation_id, Message.role == "assistant")
                .order_by(Message.sequence)
            )).all())
        self.assertEqual((assistants[0].status, assistants[0].error_code), ("failed", "provider_timeout"))
        self.assertEqual(assistants[1].status, "completed")

    async def test_login_failures_are_limited_when_switch_is_enabled(self) -> None:
        self.enable_limits(login_failure_limit=1, login_failure_window_seconds=60)
        credentials = {"username": "missing_account", "password": "wrong-password"}
        first = await self.client.post("/api/v1/identity/login", json=credentials)
        second = await self.client.post("/api/v1/identity/login", json=credentials)
        self.assertEqual(first.status_code, 401)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["detail"]["code"], "login_rate_limited")
        self.assertIn("Retry-After", second.headers)

    async def test_legacy_stream_is_disabled_while_limits_are_enabled(self) -> None:
        self.enable_limits()
        response = await self.client.post(
            "/api/v1/conversations/messages:stream",
            json={"message": "旧接口", "history": []},
        )
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["detail"]["code"], "legacy_stream_disabled")


if __name__ == "__main__":
    unittest.main()
