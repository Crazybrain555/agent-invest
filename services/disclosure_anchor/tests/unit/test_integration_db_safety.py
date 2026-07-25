"""Fail-closed tests for the disposable integration database boundary."""

from __future__ import annotations

import os
import signal
import subprocess
import unittest
from unittest import mock

from tests.integration import _support
from tests.integration._runner import (
    ScratchIntegrationDatabase,
    _run_unittest_child,
)


class IntegrationDatabaseSafetyTests(unittest.TestCase):
    def _engine_with_identity(
        self,
        database_name: str,
        database_comment: str | None,
        *,
        migrated: bool = True,
    ) -> mock.MagicMock:
        identity_result = mock.MagicMock()
        identity_result.one.return_value = (database_name, database_comment)
        migration_result = mock.MagicMock()
        migration_result.scalar.return_value = 1 if migrated else None
        connection = mock.MagicMock()
        connection.execute.side_effect = [identity_result, migration_result]
        engine = mock.MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        return engine

    def test_runtime_database_without_test_url_is_rejected_before_connect(
        self,
    ) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"DATABASE_URL": "postgresql://localhost/invest_engine"},
                clear=True,
            ),
            mock.patch.object(_support, "create_db_engine") as create_engine,
        ):
            with self.assertRaisesRegex(RuntimeError, "refusing integration"):
                _support.engine_or_skip()
        create_engine.assert_not_called()

    def test_test_url_pointing_to_production_database_is_rejected(self) -> None:
        engine = self._engine_with_identity("invest_engine", None)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "DISCLOSURE_TEST_DATABASE_URL": (
                        "postgresql://localhost/invest_engine"
                    )
                },
                clear=True,
            ),
            mock.patch.object(
                _support, "create_db_engine", return_value=engine
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "managed scratch identity"):
                _support.engine_or_skip()
        engine.dispose.assert_called_once_with()

    def test_scratch_name_without_runner_marker_is_rejected(self) -> None:
        database_name = "invest_engine_itest_1785000035_456_deadbeef"
        cases = (
            (database_name, None),
            (
                database_name,
                f"{_support.TEST_DATABASE_COMMENT_PREFIX}1785000035:"
                "invest_engine_itest_1785000035_999_feedface",
            ),
            (
                "invest_engine_itest_1785000035_backup",
                f"{_support.TEST_DATABASE_COMMENT_PREFIX}1785000035:"
                "invest_engine_itest_1785000035_backup",
            ),
        )
        for candidate_name, marker in cases:
            with self.subTest(database_name=candidate_name, marker=marker):
                engine = self._engine_with_identity(candidate_name, marker)
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "DISCLOSURE_TEST_DATABASE_URL": (
                                f"postgresql://localhost/{candidate_name}"
                            )
                        },
                        clear=True,
                    ),
                    mock.patch.object(
                        _support, "create_db_engine", return_value=engine
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "managed scratch identity"
                    ):
                        _support.engine_or_skip()
                engine.dispose.assert_called_once_with()

    def test_managed_migrated_scratch_database_is_accepted(self) -> None:
        database_name = "invest_engine_itest_1785000035_456_deadbeef"
        engine = self._engine_with_identity(
            database_name,
            f"{_support.TEST_DATABASE_COMMENT_PREFIX}1785000035:{database_name}",
        )
        with (
            mock.patch.dict(
                os.environ,
                {
                    "DISCLOSURE_TEST_DATABASE_URL": (
                        f"postgresql://localhost/{database_name}"
                    )
                },
                clear=True,
            ),
            mock.patch.object(
                _support, "create_db_engine", return_value=engine
            ),
        ):
            self.assertIs(_support.engine_or_skip(), engine)
        engine.dispose.assert_not_called()

    def test_runner_sanitizes_mineru_environment_unless_opted_in(self) -> None:
        inherited = {
            "DISCLOSURE_MINERU_BIN": "/production/mineru",
            "DISCLOSURE_MINERU_BACKEND": "vlm-http-client",
            "DISCLOSURE_MINERU_SERVER_URL": "http://gpu.internal:30000",
            "MINERU_MODEL_CACHE": "/production/cache/mineru",
            "HF_HOME": "/production/cache/huggingface",
            "MODELSCOPE_CACHE": "/production/cache/modelscope",
            "WORKER_PARSE_CONCURRENCY": "16",
        }
        with mock.patch.dict(os.environ, inherited, clear=True):
            isolated = ScratchIntegrationDatabase(
                "postgresql+psycopg://localhost/postgres"
            )
            opted_in = ScratchIntegrationDatabase(
                "postgresql+psycopg://localhost/postgres",
                real_mineru=True,
            )
            try:
                isolated_env = isolated._test_environment()
                opted_in_env = opted_in._test_environment()
            finally:
                isolated.close()
                opted_in.close()

        self.assertNotEqual(
            isolated_env["DISCLOSURE_MINERU_BIN"],
            inherited["DISCLOSURE_MINERU_BIN"],
        )
        self.assertNotIn(
            inherited["DISCLOSURE_MINERU_SERVER_URL"],
            isolated_env.values(),
        )
        self.assertNotIn(
            inherited["MINERU_MODEL_CACHE"],
            isolated_env.values(),
        )
        self.assertEqual(isolated_env["WORKER_PARSE_CONCURRENCY"], "1")
        for key, value in inherited.items():
            self.assertEqual(opted_in_env[key], value)

    def test_reaper_uses_parent_lease_as_liveness_signal(self) -> None:
        database_name = "invest_engine_itest_1785000035_456_deadbeef"
        marker = (
            f"{_support.TEST_DATABASE_COMMENT_PREFIX}1785000035:{database_name}"
        )
        scratch = ScratchIntegrationDatabase(
            "postgresql+psycopg://localhost/postgres"
        )
        try:
            for lease_available, should_drop in ((False, False), (True, True)):
                with self.subTest(lease_available=lease_available):
                    rows = mock.MagicMock()
                    rows.all.return_value = [(database_name, marker)]
                    lease_result = mock.MagicMock()
                    lease_result.scalar.return_value = lease_available
                    admin = mock.MagicMock()
                    admin.execute.side_effect = [
                        rows,
                        lease_result,
                        mock.MagicMock(),
                    ]

                    scratch._reap_orphans(admin)

                    if should_drop:
                        admin.exec_driver_sql.assert_called_once_with(
                            f'DROP DATABASE "{database_name}" WITH (FORCE)'
                        )
                    else:
                        admin.exec_driver_sql.assert_not_called()
        finally:
            scratch.close()

    def test_interruption_signals_entire_unittest_process_group(self) -> None:
        child = mock.MagicMock()
        child.pid = 43210
        child.poll.return_value = None
        child.wait.side_effect = [
            KeyboardInterrupt(),
            subprocess.TimeoutExpired(cmd="unittest", timeout=65),
            subprocess.TimeoutExpired(cmd="unittest", timeout=10),
            0,
        ]
        with (
            mock.patch(
                "tests.integration._runner.subprocess.Popen",
                return_value=child,
            ),
            mock.patch("tests.integration._runner.os.killpg") as kill_group,
        ):
            with self.assertRaises(KeyboardInterrupt):
                _run_unittest_child({}, test_names=(), verbose=False)

        self.assertEqual(
            kill_group.call_args_list,
            [
                mock.call(43210, signal.SIGINT),
                mock.call(43210, signal.SIGTERM),
                mock.call(43210, signal.SIGKILL),
            ],
        )


if __name__ == "__main__":
    unittest.main()
