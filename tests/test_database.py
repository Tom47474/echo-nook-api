import unittest

from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.db.session import DatabaseManager, validate_database_url
from app.main import create_app, lifespan


class DatabaseConfigurationTests(unittest.TestCase):
    def test_test_environment_selects_test_url(self) -> None:
        settings = Settings(
            _env_file=None,
            app_env="test",
            database_url="postgresql+asyncpg://user:secret@localhost/app_dev",
            test_database_url="postgresql+asyncpg://user:secret@localhost/app_test",
        )

        self.assertTrue(settings.active_database_url().endswith("/app_test"))
        self.assertNotIn("secret", repr(settings))

    def test_test_database_guard_rejects_development_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "_test"):
            validate_database_url(
                "postgresql+asyncpg://user:secret@localhost/app_dev",
                require_test_database=True,
            )

    def test_asyncpg_driver_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, r"postgresql\+asyncpg"):
            validate_database_url("postgresql://user:secret@localhost/app_dev")


class DatabaseConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_lifespan_connects_and_disposes(self) -> None:
        app = create_app(get_settings())
        async with lifespan(app):
            await app.state.database_manager.ping()

    async def test_local_test_database_connection_and_session(self) -> None:
        settings = get_settings()
        url = settings.test_database_url.get_secret_value()
        if not url:
            self.skipTest("TEST_DATABASE_URL is not configured")

        manager = DatabaseManager(url, require_test_database=True)
        try:
            await manager.ping()
            async with manager.session() as session:
                database_name = await session.scalar(text("SELECT current_database()"))
                database_user = await session.scalar(text("SELECT current_user"))
        finally:
            await manager.dispose()

        self.assertTrue(database_name.endswith("_test"))
        self.assertEqual(database_user, "echo_nook")


if __name__ == "__main__":
    unittest.main()
