import os
import unittest
from pathlib import Path
from unittest import mock

from asset_intake.cli.doctor import collect_checks
from asset_intake.settings import Settings


def _settings(**env: str) -> Settings:
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings()


class DoctorTests(unittest.TestCase):
    def test_envelope_kernel_check_passes(self) -> None:
        checks = collect_checks(_settings())
        kernel = next(c for c in checks if c.name == "envelope_kernel")
        self.assertTrue(kernel.ok)
        self.assertIn("data_asset.v1", kernel.detail)

    def test_unset_dsns_warn_but_are_reported(self) -> None:
        checks = {c.name: c for c in collect_checks(_settings())}
        for name in ("database_url", "migration_database_url", "reader_database_url", "tushare_token"):
            self.assertFalse(checks[name].ok)
            self.assertIn("unset", checks[name].detail)

    def test_set_dsns_and_existing_data_root_pass(self) -> None:
        checks = {
            c.name: c
            for c in collect_checks(
                _settings(
                    ASSET_INTAKE_DATA_ROOT=str(Path(__file__).parent),
                    ASSET_INTAKE_DATABASE_URL="postgresql+psycopg://intake_app@/invest_engine",
                    ASSET_INTAKE_MIGRATION_DATABASE_URL="postgresql+psycopg://intake_owner@/invest_engine",
                    ASSET_INTAKE_READER_DATABASE_URL="postgresql+psycopg://intake_reader@/invest_engine",
                    TUSHARE_TOKEN="t",
                )
            )
        }
        self.assertTrue(all(c.ok for c in checks.values()), checks)


if __name__ == "__main__":
    unittest.main()
