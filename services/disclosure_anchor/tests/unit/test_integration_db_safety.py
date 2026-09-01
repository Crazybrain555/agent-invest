"""Fail-closed tests for the disposable integration database boundary."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import traceback
import unittest
from unittest import mock

from sqlalchemy.engine import make_url

from disclosure_anchor.settings import load_settings
from tests.integration import _support
from tests.integration._runner import (
    ScratchIntegrationDatabase,
    _run_unittest_child,
)

SCRATCH_NAME = "invest_engine_scratch_1785000035_456_deadbeef"
SCRATCH_MARKER = f"{_support.TEST_DATABASE_COMMENT_PREFIX}1785000035:{SCRATCH_NAME}"
SIBLING_SCRATCH_NAME = "invest_engine_scratch_1785000035_457_feedface"
# Placeholder credentials only; the tests prove they never reach an error.
SCRATCH_URL = (
    "postgresql+psycopg://disclosure_owner:owner-secret@localhost:55432/"
    f"{SCRATCH_NAME}"
)
RUNTIME_URL = (
    "postgresql+psycopg://disclosure_app:app-secret@localhost:55432/invest_engine"
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
        database_name = "invest_engine_scratch_1785000035_456_deadbeef"
        cases = (
            (database_name, None),
            (
                database_name,
                f"{_support.TEST_DATABASE_COMMENT_PREFIX}1785000035:"
                "invest_engine_scratch_1785000035_999_feedface",
            ),
            (
                "invest_engine_scratch_1785000035_backup",
                f"{_support.TEST_DATABASE_COMMENT_PREFIX}1785000035:"
                "invest_engine_scratch_1785000035_backup",
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
        database_name = "invest_engine_scratch_1785000035_456_deadbeef"
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

    def test_conflicting_destination_in_any_database_variable_is_rejected(
        self,
    ) -> None:
        # Direct unittest invocation: the engine would follow the test URL
        # while Alembic/settings follow an inherited sibling. Every mixed
        # shape must stop before an engine or a subprocess exists.
        conflicts = {
            "database": SCRATCH_URL.replace(SCRATCH_NAME, SIBLING_SCRATCH_NAME),
            "host": SCRATCH_URL.replace("@localhost:", "@db.example.internal:"),
            "port": SCRATCH_URL.replace(":55432/", ":5432/"),
            "runtime": RUNTIME_URL,
        }
        for variable in _support.DATABASE_ENV_KEYS:
            if variable == _support.TEST_DATABASE_ENV_KEY:
                continue
            for dimension, conflicting_url in conflicts.items():
                with self.subTest(variable=variable, dimension=dimension):
                    with (
                        mock.patch.dict(
                            os.environ,
                            {
                                _support.TEST_DATABASE_ENV_KEY: SCRATCH_URL,
                                variable: conflicting_url,
                            },
                            clear=True,
                        ),
                        mock.patch.object(
                            _support, "create_db_engine"
                        ) as create_engine,
                        mock.patch.object(_support.subprocess, "run") as run,
                    ):
                        with self.assertRaises(RuntimeError) as caught:
                            _support.engine_or_skip()
                    message = str(caught.exception)
                    self.assertIn(variable, message)
                    self.assertIn(_support.TEST_DATABASE_ENV_KEY, message)
                    self.assertNotIn("owner-secret", message)
                    self.assertNotIn("app-secret", message)
                    create_engine.assert_not_called()
                    run.assert_not_called()

    def test_unparsable_destination_is_rejected_without_exposing_credentials(
        self,
    ) -> None:
        broken_urls = {
            # SQLAlchemy echoes the whole string in its own parse error.
            "missing_scheme": "disclosure_owner:owner-secret@localhost:55432/db",
            "non_numeric_port": (
                "postgresql+psycopg://disclosure_owner:owner-secret@localhost:port/db"
            ),
            "missing_database": (
                "postgresql+psycopg://disclosure_owner:owner-secret@localhost:55432"
            ),
        }
        for variable in (_support.TEST_DATABASE_ENV_KEY, "DATABASE_URL"):
            for shape, broken_url in broken_urls.items():
                with self.subTest(variable=variable, shape=shape):
                    environment = {_support.TEST_DATABASE_ENV_KEY: SCRATCH_URL}
                    environment[variable] = broken_url
                    with (
                        mock.patch.dict(os.environ, environment, clear=True),
                        mock.patch.object(
                            _support, "create_db_engine"
                        ) as create_engine,
                    ):
                        with self.assertRaises(RuntimeError) as caught:
                            _support.engine_or_skip()
                    rendered = "".join(traceback.format_exception(caught.exception))
                    self.assertIn(variable, str(caught.exception))
                    self.assertNotIn("owner-secret", rendered)
                    self.assertNotIn(broken_url, rendered)
                    create_engine.assert_not_called()

    def test_case_ambiguous_database_variable_is_rejected_before_connect(
        self,
    ) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                    _support.TEST_DATABASE_ENV_KEY: SCRATCH_URL,
                    "disclosure_migration_database_url": RUNTIME_URL,
                },
                clear=True,
            ),
            mock.patch.object(_support, "create_db_engine") as create_engine,
            mock.patch.object(_support.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "noncanonical") as caught:
                _support.engine_or_skip()
        self.assertNotIn("app-secret", str(caught.exception))
        create_engine.assert_not_called()
        run.assert_not_called()

    def test_distinct_connection_identity_to_the_same_destination_is_rejected(
        self,
    ) -> None:
        environments = {
            "different_logins_drivers_and_options": {
                _support.TEST_DATABASE_ENV_KEY: SCRATCH_URL,
                "DISCLOSURE_MIGRATION_DATABASE_URL": (
                    "postgresql://migration_login:migration-secret@localhost:55432/"
                    f"{SCRATCH_NAME}?application_name=alembic"
                ),
                "DATABASE_URL": (
                    "postgresql+psycopg2://disclosure_app:app-secret@localhost:55432/"
                    f"{SCRATCH_NAME}"
                ),
                "DISCLOSURE_ADMIN_DATABASE_URL": (
                    "postgresql+psycopg://disclosure_admin@localhost:55432/"
                    f"{SCRATCH_NAME}?sslmode=disable"
                ),
                "DISCLOSURE_READER_DATABASE_URL": (
                    "postgresql+psycopg://disclosure_reader:reader-secret@/"
                    f"{SCRATCH_NAME}?host=localhost&port=55432"
                ),
            },
            "implicit_default_port": {
                _support.TEST_DATABASE_ENV_KEY: (
                    "postgresql://disclosure_owner:owner-secret@localhost/"
                    f"{SCRATCH_NAME}"
                ),
                "DISCLOSURE_MIGRATION_DATABASE_URL": (
                    "postgresql+psycopg://migration_login@localhost:5432/"
                    f"{SCRATCH_NAME}"
                ),
            },
        }
        for shape, environment in environments.items():
            with self.subTest(shape=shape):
                engine = self._engine_with_identity(SCRATCH_NAME, SCRATCH_MARKER)
                with (
                    mock.patch.dict(os.environ, environment, clear=True),
                    mock.patch.object(
                        _support, "create_db_engine", return_value=engine
                    ) as create_engine,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "does not exactly match",
                    ) as caught:
                        _support.engine_or_skip()
                rendered = "".join(traceback.format_exception(caught.exception))
                for secret in (
                    "owner-secret",
                    "migration-secret",
                    "app-secret",
                    "reader-secret",
                ):
                    self.assertNotIn(secret, rendered)
                create_engine.assert_not_called()
                engine.dispose.assert_not_called()

    def test_alembic_environment_pins_every_database_variable_to_the_engine(
        self,
    ) -> None:
        inherited = {
            _support.TEST_DATABASE_ENV_KEY: SCRATCH_URL.replace(
                "disclosure_owner:owner-secret", "tester:tester-secret"
            ),
            "DISCLOSURE_MIGRATION_DATABASE_URL": SCRATCH_URL.replace(
                SCRATCH_NAME, SIBLING_SCRATCH_NAME
            ),
            "DATABASE_URL": RUNTIME_URL,
            "DISCLOSURE_ADMIN_DATABASE_URL": RUNTIME_URL.replace(
                "disclosure_app:app-secret", "disclosure_admin:admin-secret"
            ),
            "DISCLOSURE_READER_DATABASE_URL": RUNTIME_URL.replace(
                "disclosure_app:app-secret", "disclosure_reader:reader-secret"
            ),
            "PYTHONPATH": "/elsewhere/src",
            "DISCLOSURE_DATA_ROOT": "/scratch/data",
            "disclosure_migration_database_url": RUNTIME_URL,
        }
        engine = mock.MagicMock()
        engine.url = make_url(SCRATCH_URL)
        with mock.patch.dict(os.environ, inherited, clear=True):
            environment = _support.alembic_subprocess_environment(engine)
            # The parent process environment is copied, never mutated.
            self.assertEqual(dict(os.environ), inherited)
        for key in _support.DATABASE_ENV_KEYS:
            self.assertEqual(environment[key], SCRATCH_URL)
            if key in inherited:
                self.assertNotIn(inherited[key], environment.values())
        self.assertNotIn("disclosure_migration_database_url", environment)
        self.assertEqual(environment["PYTHONPATH"], "src")
        self.assertEqual(environment["DISCLOSURE_DATA_ROOT"], "/scratch/data")

    def test_run_alembic_uses_the_pinned_environment_in_the_service_root(
        self,
    ) -> None:
        engine = mock.MagicMock()
        engine.url = make_url(SCRATCH_URL)
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        inherited = {
            "DISCLOSURE_MIGRATION_DATABASE_URL": SCRATCH_URL.replace(
                SCRATCH_NAME, SIBLING_SCRATCH_NAME
            ),
            "DATABASE_URL": RUNTIME_URL,
        }
        with (
            mock.patch.dict(os.environ, inherited, clear=True),
            mock.patch.object(
                _support.subprocess, "run", return_value=completed
            ) as run,
        ):
            result = _support.run_alembic(
                engine, "downgrade", "0056_staged_credit_evidence"
            )
        self.assertIs(result, completed)
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args,
            (
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "downgrade",
                    "0056_staged_credit_evidence",
                ],
            ),
        )
        options = run.call_args.kwargs
        self.assertEqual(options["cwd"], _support.SERVICE_ROOT)
        self.assertTrue(options["capture_output"])
        self.assertTrue(options["text"])
        self.assertFalse(options["check"])
        for key in _support.DATABASE_ENV_KEYS:
            self.assertEqual(options["env"][key], SCRATCH_URL)
        for value in inherited.values():
            self.assertNotIn(value, options["env"].values())

    def test_runner_environment_pins_exactly_the_shared_keys_and_is_accepted(
        self,
    ) -> None:
        # The runner and _support must agree on the variable set: a key pinned
        # by only one side would reopen the mixed-destination window.
        scratch = ScratchIntegrationDatabase(
            "postgresql+psycopg://disclosure_owner:owner-secret@localhost:55432/"
            "postgres"
        )
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "DATABASE_URL": RUNTIME_URL,
                    "DISCLOSURE_MIGRATION_DATABASE_URL": RUNTIME_URL.replace(
                        "disclosure_app:app-secret", "disclosure_owner:owner-secret"
                    ),
                },
                clear=True,
            ):
                environment = scratch._test_environment()
        finally:
            scratch.close()
        pinned = {
            key
            for key, value in environment.items()
            if value == scratch.database_url
        }
        self.assertEqual(pinned, set(_support.DATABASE_ENV_KEYS))

        created_epoch = _support.test_database_created_epoch(scratch.database_name)
        engine = self._engine_with_identity(
            scratch.database_name,
            f"{_support.TEST_DATABASE_COMMENT_PREFIX}{created_epoch}:"
            f"{scratch.database_name}",
        )
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(
                _support, "create_db_engine", return_value=engine
            ) as create_engine,
        ):
            self.assertIs(_support.engine_or_skip(), engine)
        create_engine.assert_called_once_with(scratch.database_url)

    def test_runner_removes_case_collisions_before_settings_resolution(
        self,
    ) -> None:
        scratch = ScratchIntegrationDatabase(
            "postgresql+psycopg://disclosure_owner:owner-secret@localhost:55432/"
            "postgres"
        )
        try:
            orderings = (
                (
                    ("DISCLOSURE_MIGRATION_DATABASE_URL", RUNTIME_URL),
                    ("disclosure_migration_database_url", RUNTIME_URL),
                ),
                (
                    ("disclosure_migration_database_url", RUNTIME_URL),
                    ("DISCLOSURE_MIGRATION_DATABASE_URL", RUNTIME_URL),
                ),
            )
            for inherited_items in orderings:
                with self.subTest(inherited_items=inherited_items):
                    inherited = dict(inherited_items)
                    with mock.patch.dict(os.environ, inherited, clear=True):
                        environment = scratch._test_environment()
                    variants = {
                        key
                        for key in environment
                        if key.upper() in _support.DATABASE_ENV_KEYS
                    }
                    self.assertEqual(
                        variants,
                        set(_support.DATABASE_ENV_KEYS),
                    )
                    self.assertNotIn(
                        "disclosure_migration_database_url",
                        environment,
                    )
                    with mock.patch.dict(os.environ, environment, clear=True):
                        settings = load_settings()
                    assert settings.disclosure_migration_database_url is not None
                    self.assertEqual(
                        settings.disclosure_migration_database_url.get_secret_value(),
                        scratch.database_url,
                    )
        finally:
            scratch.close()

    def test_runner_sanitizes_mineru_environment_unless_opted_in(self) -> None:
        inherited = {
            "DISCLOSURE_MINERU_BIN": "/production/mineru",
            "DISCLOSURE_MINERU_BACKEND": "vlm-http-client",
            "DISCLOSURE_MINERU_API_URL": "http://127.0.0.1:30002",
            "DISCLOSURE_MINERU_OBSERVABILITY_URL": "http://127.0.0.1:30001/v1",
            "DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL": (
                "http://mineru-openai-server:30000/v1"
            ),
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
            inherited["DISCLOSURE_MINERU_API_URL"],
            isolated_env.values(),
        )
        self.assertNotIn(
            inherited["MINERU_MODEL_CACHE"],
            isolated_env.values(),
        )
        self.assertEqual(isolated_env["WORKER_PARSE_CONCURRENCY"], "1")
        for key, value in inherited.items():
            self.assertEqual(opted_in_env[key], value)

    def test_runner_bootstrap_commits_schema_grants_before_migration(self) -> None:
        scratch = ScratchIntegrationDatabase(
            "postgresql+psycopg://localhost/postgres"
        )
        schema_engine = mock.MagicMock()
        try:
            with (
                mock.patch.object(scratch, "provision"),
                mock.patch(
                    "tests.integration._runner.sqlalchemy.create_engine",
                    return_value=schema_engine,
                ) as create_engine,
                mock.patch(
                    "tests.integration._runner.ensure_schemas_and_base_grants"
                ) as bootstrap,
            ):
                scratch._provision()
            create_engine.assert_called_once_with(
                scratch.database_url,
                isolation_level="AUTOCOMMIT",
            )
            bootstrap.assert_called_once_with(schema_engine)
            schema_engine.dispose.assert_called_once_with()
        finally:
            scratch.close()

    def test_reaper_uses_parent_lease_as_liveness_signal(self) -> None:
        database_name = "invest_engine_scratch_1785000035_456_deadbeef"
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
