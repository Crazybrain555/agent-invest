"""A2 real staged composition with no database connection or provider request."""

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sqlalchemy as sa

from disclosure_anchor.adapters.runtime import staged_worker_v4 as builder
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.settings import Settings
from tests._mineru_package_a_fixture import gate_fixture, private_json


class ExplicitStagedBuilderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def profile_environment(self, root, profile):
        path = root / "profile.json"
        path.write_bytes(profile.exact_bytes)
        path.chmod(0o600)
        keyring = root / "synthetic-keyring.json"
        private_json(
            keyring,
            {
                "format": "disclosure-v4-secret-keyring.v1",
                "primary_kek_id": "test-only",
                "keks": {"test-only": "11" * 32},
            },
        )
        return {
            "DISCLOSURE_V4_PROCESS_PROFILE_FILE": str(path),
            "DISCLOSURE_V4_PROCESS_PROFILE_SHA256": profile.sha256,
            "DISCLOSURE_V4_SECRET_KEYRING_FILE": str(keyring),
            "DISCLOSURE_V4_ARCHIVE_MEMBER_COUNT_LIMIT": "8192",
        }

    def test_real_builder_composes_H14_and_H20_without_DB_or_network_execution(self):
        for http in (14, 20):
            root = self.root / str(http)
            root.mkdir()
            settings, profile, capacity, _ = gate_fixture(root, http)
            environment = self.profile_environment(root, profile)
            settings = Settings(
                **dict(
                    settings.model_dump(),
                    disclosure_v4_secret_keyring_file=Path(
                        environment["DISCLOSURE_V4_SECRET_KEYRING_FILE"]
                    ),
                )
            )
            FileStorePathBuilder(settings).data_path(Path()).mkdir(parents=True)
            engine = sa.create_engine("sqlite+pysqlite:///:memory:")
            try:
                with (
                    self.subTest(http=http),
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(
                        engine,
                        "connect",
                        side_effect=AssertionError("DB connection forbidden"),
                    ),
                    patch(
                        "httpx.Client.send",
                        side_effect=AssertionError("HTTP forbidden"),
                    ),
                    patch(
                        "socket.getaddrinfo",
                        side_effect=AssertionError("DNS forbidden"),
                    ),
                ):
                    runtime = builder.build_staged_worker_v4_runtime(
                        settings=settings,
                        engine=engine,
                        ownership_guard=lambda: None,
                        admission_guard=lambda: None,
                        process_scope_classes=None,
                        progress=lambda _: None,
                        owner_identity="independent-a2-composition-test",
                        **({"expected_capacity": capacity} if http == 20 else {}),
                    )
                    try:
                        self.assertEqual(
                            runtime.owner_identity, "independent-a2-composition-test"
                        )
                        self.assertEqual(
                            runtime.coordinator._limits.credits.documents, 128
                        )
                        self.assertEqual(
                            runtime.coordinator._limits.credits.provider_result_bytes,
                            2 * 1024**3,
                        )
                        self.assertTrue(
                            (
                                settings.disclosure_runtime_root / "staged_v4/scratch"
                            ).is_dir()
                        )
                    finally:
                        runtime.close()
            finally:
                engine.dispose()

    def test_missing_DTO_loads_paired_file_and_drift_rejects_before_scratch(self):
        settings, profile, _, _ = gate_fixture(self.root)
        environment = self.profile_environment(
            self.root, replace(profile, finalizer_slots=2)
        )
        engine = sa.create_engine("sqlite+pysqlite:///:memory:")
        try:
            with (
                patch.dict(os.environ, environment, clear=True),
                patch.object(
                    engine,
                    "connect",
                    side_effect=AssertionError("DB connection forbidden"),
                ),
                patch.object(
                    builder,
                    "MinerUHttpRemoteV4",
                    side_effect=AssertionError(
                        "remote object created before profile rejection"
                    ),
                ),
                self.assertRaisesRegex(
                    Exception, "explicit capacity/profile/configuration drift"
                ),
            ):
                builder.build_staged_worker_v4_runtime(
                    settings=settings,
                    engine=engine,
                    ownership_guard=lambda: None,
                    admission_guard=lambda: None,
                    process_scope_classes=None,
                    progress=lambda _: None,
                )
            self.assertFalse((settings.disclosure_runtime_root / "staged_v4").exists())
        finally:
            engine.dispose()
