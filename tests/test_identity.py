"""游客与账号的会话归属、显式导入和跨身份访问验证。"""

import unittest
from uuid import UUID

import httpx
from sqlalchemy import delete, select

from app.conversations.models import Conversation, ModelCall, Principal
from app.core.config import Settings
from app.main import create_app, lifespan


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        settings = Settings(app_env="test")
        if not settings.test_database_url.get_secret_value():
            self.skipTest("TEST_DATABASE_URL is not configured")
        self.app = create_app(settings)
        self.lifespan = lifespan(self.app)
        await self.lifespan.__aenter__()
        transport = httpx.ASGITransport(app=self.app)
        self.guest_client = httpx.AsyncClient(transport=transport, base_url="http://test")
        self.other_client = httpx.AsyncClient(transport=transport, base_url="http://test")
        self.principal_ids: list[UUID] = []
        for client in (self.guest_client, self.other_client):
            response = await client.post("/api/v1/identity/bootstrap")
            self.assertEqual(response.status_code, 200)
            self.principal_ids.append(UUID(response.json()["id"]))

    async def asyncTearDown(self) -> None:
        async with self.app.state.database_manager.session() as session:
            await session.execute(delete(Conversation).where(Conversation.owner_id.in_(self.principal_ids)))
            await session.execute(delete(Principal).where(Principal.id.in_(self.principal_ids)))
            await session.commit()
        await self.guest_client.aclose()
        await self.other_client.aclose()
        await self.lifespan.__aexit__(None, None, None)

    async def test_cross_guest_access_is_denied_for_every_conversation_operation(self) -> None:
        created = await self.guest_client.post("/api/v1/conversations", json={"title": "私有会话"})
        conversation_id = created.json()["id"]
        self.assertEqual(created.status_code, 201)
        self.assertEqual((await self.other_client.get("/api/v1/conversations")).json()["items"], [])
        for method, path, payload in (
            ("get", f"/api/v1/conversations/{conversation_id}", None),
            ("get", f"/api/v1/conversations/{conversation_id}/messages", None),
            ("delete", f"/api/v1/conversations/{conversation_id}", None),
            ("post", f"/api/v1/conversations/{conversation_id}/messages:stream", {"message": "越权"}),
        ):
            response = await getattr(self.other_client, method)(path, **({"json": payload} if payload else {}))
            self.assertEqual(response.status_code, 404)

    async def test_account_login_rejects_wrong_password_and_other_account_access(self) -> None:
        first = await self.guest_client.post("/api/v1/identity/register", json={
            "username": "identity_first_account", "password": "first-password-123",
        })
        second = await self.other_client.post("/api/v1/identity/register", json={
            "username": "identity_second_account", "password": "second-password-123",
        })
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        duplicate = await self.other_client.post("/api/v1/identity/register", json={
            "username": "IDENTITY_FIRST_ACCOUNT", "password": "another-password-123",
        })
        self.assertEqual(duplicate.status_code, 409)
        self.principal_ids.extend((UUID(first.json()["id"]), UUID(second.json()["id"])))
        created = await self.guest_client.post("/api/v1/conversations", json={"title": "账号私有"})
        conversation_id = created.json()["id"]
        self.assertEqual((await self.other_client.get(f"/api/v1/conversations/{conversation_id}")).status_code, 404)
        bad_login = await self.other_client.post("/api/v1/identity/login", json={
            "username": "identity_first_account", "password": "wrong-password",
        })
        self.assertEqual(bad_login.status_code, 401)
        self.assertEqual((await self.other_client.get(f"/api/v1/conversations/{conversation_id}")).status_code, 404)
        logged_out = await self.guest_client.post("/api/v1/identity/logout")
        self.assertEqual(logged_out.json()["kind"], "guest")
        self.principal_ids.append(UUID(logged_out.json()["id"]))
        self.assertEqual((await self.guest_client.get(f"/api/v1/conversations/{conversation_id}")).status_code, 404)
        logged_in = await self.guest_client.post("/api/v1/identity/login", json={
            "username": "identity_first_account", "password": "first-password-123",
        })
        self.assertEqual(logged_in.status_code, 200)
        self.assertEqual((await self.guest_client.get(f"/api/v1/conversations/{conversation_id}")).status_code, 200)

    async def test_cross_site_write_request_is_rejected(self) -> None:
        response = await self.guest_client.post(
            "/api/v1/conversations", json={"title": "不应创建"},
            headers={"Origin": "https://untrusted.example"},
        )
        self.assertEqual(response.status_code, 403)

    async def test_allowed_browser_origin_can_send_cookie_credentials(self) -> None:
        response = await self.guest_client.post(
            "/api/v1/conversations", json={"title": "浏览器请求"},
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["access-control-allow-credentials"], "true")
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:3000")

    async def test_confirmed_import_keeps_history_usage_owner_and_original_id(self) -> None:
        created = await self.guest_client.post("/api/v1/conversations", json={"title": "游客会话"})
        conversation_id = UUID(created.json()["id"])
        guest_id = self.principal_ids[0]
        async with self.app.state.database_manager.session() as session:
            session.add(ModelCall(conversation_id=conversation_id, purpose="reply", status="completed",
                                  usage_owner_id=guest_id, usage_owner_kind="guest", total_tokens=10))
            await session.commit()
        credentials = {"username": "identity_test_account", "password": "strong-test-password"}
        registered = await self.guest_client.post("/api/v1/identity/register", json=credentials)
        self.assertEqual(registered.status_code, 200)
        account_id = UUID(registered.json()["id"])
        self.principal_ids.append(account_id)
        self.assertEqual(registered.json()["importable_count"], 1)
        self.assertEqual((await self.guest_client.get("/api/v1/conversations")).json()["items"], [])
        imported = await self.guest_client.post("/api/v1/identity/import")
        self.assertEqual(imported.json(), {"imported_count": 1})
        self.assertEqual((await self.guest_client.get(f"/api/v1/conversations/{conversation_id}")).status_code, 200)
        self.assertEqual((await self.guest_client.post("/api/v1/identity/import")).status_code, 409)
        async with self.app.state.database_manager.session() as session:
            call = await session.scalar(select(ModelCall).where(ModelCall.conversation_id == conversation_id))
            self.assertEqual((call.usage_owner_id, call.usage_owner_kind), (guest_id, "guest"))
