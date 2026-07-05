import os
import unittest
from unittest import mock

from asset_intake.settings import DEFAULT_DATA_ROOT, Settings


class SettingsTests(unittest.TestCase):
    def test_defaults_without_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = Settings()
        self.assertEqual(settings.data_root, DEFAULT_DATA_ROOT)
        self.assertIsNone(settings.database_url)
        self.assertIsNone(settings.migration_database_url)
        self.assertIsNone(settings.reader_database_url)
        self.assertIsNone(settings.tushare_token)

    def test_env_variables_are_read(self) -> None:
        env = {
            "ASSET_INTAKE_DATA_ROOT": "/tmp/intake-data",
            "ASSET_INTAKE_DATABASE_URL": "postgresql+psycopg://intake_app@/invest_engine",
            "ASSET_INTAKE_MIGRATION_DATABASE_URL": "postgresql+psycopg://intake_owner@/invest_engine",
            "ASSET_INTAKE_READER_DATABASE_URL": "postgresql+psycopg://intake_reader@/invest_engine",
            "TUSHARE_TOKEN": "secret-token",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = Settings()
        self.assertEqual(str(settings.data_root), "/tmp/intake-data")
        assert settings.database_url is not None
        self.assertIn("intake_app", settings.database_url.get_secret_value())
        assert settings.tushare_token is not None
        self.assertEqual(settings.tushare_token.get_secret_value(), "secret-token")

    def test_secrets_do_not_leak_in_repr(self) -> None:
        env = {"TUSHARE_TOKEN": "secret-token", "ASSET_INTAKE_DATABASE_URL": "postgresql://u:pw@h/db"}
        with mock.patch.dict(os.environ, env, clear=True):
            settings = Settings()
        self.assertNotIn("secret-token", repr(settings))
        self.assertNotIn("pw", repr(settings))


if __name__ == "__main__":
    unittest.main()
