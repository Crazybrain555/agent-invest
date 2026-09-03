"""Scratch-PostgreSQL plus real-filesystem transaction-P closure test."""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest

import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres.atomic_document_publisher_v4 import (
    PostgresAtomicWholeDocumentPublisherV4,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.adapters.storage.atomic_publication_artifact_readiness_v4 import (
    FilesystemAtomicPublicationArtifactReadinessV4,
)
from disclosure_anchor.adapters.storage.immutable_artifact_store import (
    ImmutableArtifactStore,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    ATOMIC_PUBLICATION_PREPARATION_FILENAME,
    ATOMIC_PUBLICATION_READINESS_FILENAME,
    AtomicPublicationArtifactReadinessError,
)
from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
)
from disclosure_anchor.application.use_cases.prepare_and_publish_whole_document_v4 import (
    PrepareAndPublishWholeDocumentV4,
)
from tests.integration._remote_parse_v4_factory import (
    build_atomic_publication_request_v4,
    build_v4_authority_fixture,
    install_local_materialized_cycle,
)
from tests.integration._support import engine_or_skip


class _Paths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def data_path(self, relpath: Path) -> Path:
        return self.root / relpath

    @staticmethod
    def _run_root(**values: str) -> Path:
        return (
            Path("derived/document_unit_snapshots")
            / values["provider"]
            / values["security_code"]
            / values["provider_document_id"]
            / values["processing_run_id"]
        )

    def atomic_publication_preparation_relpath(self, **values: str) -> Path:
        return self._run_root(**values) / ATOMIC_PUBLICATION_PREPARATION_FILENAME

    def atomic_publication_readiness_relpath(self, **values: str) -> Path:
        return self._run_root(**values) / ATOMIC_PUBLICATION_READINESS_FILENAME


class _ExactTreePromotion:
    def __init__(
        self,
        *,
        root: Path,
        expected_files: dict[str, bytes],
        inventory_sha256: str,
        byte_count: int,
    ) -> None:
        self.root = root
        self.expected_files = expected_files
        self.inventory_sha256 = inventory_sha256
        self.byte_count = byte_count

    def promote_or_replay(self, **kwargs: object) -> None:
        materialized = kwargs["materialized"]
        source = self.root / materialized.intent.output_relpath  # type: ignore[attr-defined]
        target = self.root / str(kwargs["published_relpath"])
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, target)
        self._verify(target)

    def verify_published(self, **kwargs: object) -> None:
        if (
            kwargs["expected_inventory_sha256"] != self.inventory_sha256
            or kwargs["expected_file_count"] != len(self.expected_files)
            or kwargs["expected_byte_count"] != self.byte_count
        ):
            raise AssertionError("published parser inventory authority drifted")
        self._verify(self.root / str(kwargs["published_relpath"]))

    def _verify(self, root: Path) -> None:
        observed: dict[str, bytes] = {}
        for path in sorted(root.rglob("*")):
            if path.is_dir():
                continue
            identity = path.lstat()
            if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
                raise AssertionError("published parser output is not private")
            observed[path.relative_to(root).as_posix()] = path.read_bytes()
        if observed != self.expected_files:
            raise AssertionError("published parser output bytes drifted")


class _Guard:
    def assert_current_under_resource_lock(self, **kwargs: object) -> None:
        return None


class PrepareAndPublishWholeDocumentV4IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.fixture = build_v4_authority_fixture()
        self.request = build_atomic_publication_request_v4(self.fixture)
        context = self.fixture.materialization_intent.provider_envelope_context
        with self.engine.begin() as conn:
            install_local_materialized_cycle(conn, self.fixture)
            conn.execute(
                sa.text(
                    "UPDATE disclosure_core.document SET status='parsed',"
                    "provider=:provider,provider_document_id=:provider_document_id,"
                    "raw_file_relpath=:source_relpath,raw_file_hash=:source_sha "
                    "WHERE document_id=:document_id"
                ),
                {
                    "document_id": self.fixture.document_id,
                    "provider": context.provider,
                    "provider_document_id": context.provider_document_id,
                    "source_relpath": context.source_pdf_relpath,
                    "source_sha": self.fixture.source_pdf_sha256,
                },
            )
            conn.execute(
                sa.text(
                    "UPDATE disclosure_core.processing_run SET "
                    "status='succeeded',artifact_hash=:provider_sha,"
                    "unit_build_status='running' "
                    "WHERE processing_run_id=:processing_run_id"
                ),
                {
                    "processing_run_id": self.fixture.processing_run_id,
                    "provider_sha": (
                        self.request.upstream_evidence.provider_document_sha256
                    ),
                },
            )
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.materialized = MaterializedProviderDocumentV4(
            receipt=self.fixture.local_materialization_receipt,
            intent=self.fixture.materialization_intent,
            provider_envelope=self.fixture.provider_envelope,
            manifest=self.fixture.materialization_manifest,
        )
        self.output_files = {
            LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME: (
                self.fixture.materialization_manifest.canonical_bytes
            ),
            PROVIDER_DOCUMENT_FILENAME: provider_document_envelope_to_bytes(
                self.fixture.provider_envelope
            ),
            **dict(self.fixture.parser_artifact_files),
        }
        source = self.root / self.fixture.materialization_intent.output_relpath
        for relpath, payload in self.output_files.items():
            path = source / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

    def tearDown(self) -> None:
        with self.engine.begin() as conn:
            conn.exec_driver_sql(
                "TRUNCATE TABLE disclosure_ops.remote_parse_attempt CASCADE"
            )
            conn.execute(
                sa.text(
                    "DELETE FROM disclosure_ops.durable_publish_base "
                    "WHERE document_id=:document_id"
                ),
                {"document_id": self.fixture.document_id},
            )
            conn.execute(
                sa.text(
                    "DELETE FROM disclosure_ops.outbox_event "
                    "WHERE document_id=:document_id"
                ),
                {"document_id": self.fixture.document_id},
            )
            conn.execute(
                sa.text(
                    "DELETE FROM disclosure_core.document_unit "
                    "WHERE document_id=:document_id"
                ),
                {"document_id": self.fixture.document_id},
            )
            conn.execute(
                sa.text(
                    "DELETE FROM disclosure_core.processing_run "
                    "WHERE document_id=:document_id"
                ),
                {"document_id": self.fixture.document_id},
            )
            conn.execute(
                sa.text(
                    "DELETE FROM disclosure_core.document "
                    "WHERE document_id=:document_id"
                ),
                {"document_id": self.fixture.document_id},
            )
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_real_files_and_transaction_p_close_one_v2_winner(self) -> None:
        paths = _Paths(self.root)
        promotion = _ExactTreePromotion(
            root=self.root,
            expected_files=self.output_files,
            inventory_sha256=(
                self.fixture.local_materialization_receipt.output_files_sha256
            ),
            byte_count=self.fixture.local_materialization_receipt.output_byte_count,
        )
        readiness = FilesystemAtomicPublicationArtifactReadinessV4(
            paths=paths,  # type: ignore[arg-type]
            immutable_store=ImmutableArtifactStore(paths),  # type: ignore[arg-type]
            output_promotion=promotion,  # type: ignore[arg-type]
        )
        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            claim = uow.remote_parse_v4.load(self.fixture.attempt_id).claim_witness
        use_case = PrepareAndPublishWholeDocumentV4(
            uow_factory=lambda: SqlAlchemyUnitOfWork(engine=self.engine),
            readiness=readiness,
            publisher=publisher,
        )

        winner = use_case.execute(
            request=self.request,
            checkpoint=self.fixture.local_materialized,
            materialized=self.materialized,
            claim=claim,
            claim_guard=_Guard(),
        )

        self.assertEqual(winner.winner_row_version, 2)
        self.assertIsNotNone(winner.artifact_readiness)
        assert winner.artifact_readiness is not None
        verified = readiness.verify_ready(
            reference=winner.artifact_readiness,
            expected_request=self.request,
            expected_winner=winner,
        )
        self.assertEqual(
            tuple(item.asset_id for item in winner.unit_assets),
            tuple(item.asset_id for item in verified.preparation.unit_bindings),
        )
        for label, mutate in (
            (
                "processing run",
                lambda value: object.__setattr__(
                    value,
                    "processing_run_row_sha256",
                    "sha256:" + "f" * 64,
                ),
            ),
            (
                "outbox",
                lambda value: object.__setattr__(
                    value.outbox_commit,
                    "events_sha256",
                    "sha256:" + "f" * 64,
                ),
            ),
            (
                "durable base",
                lambda value: object.__setattr__(
                    value.durable_base_commit,
                    "durable_base_sha256",
                    "sha256:" + "f" * 64,
                ),
            ),
        ):
            with self.subTest(forged_winner=label):
                forged = copy.deepcopy(winner)
                mutate(forged)
                with self.assertRaisesRegex(
                    AtomicPublicationArtifactReadinessError,
                    "winner drifted from its request",
                ):
                    readiness.verify_ready(
                        reference=winner.artifact_readiness,
                        expected_request=self.request,
                        expected_winner=forged,
                    )
        reloaded = publisher.reload_commit_winner_by_processing_run_id(
            processing_run_id=self.fixture.processing_run_id,
        )
        self.assertEqual(reloaded, winner)
        for plan in (
            verified.preparation.provider_document_plan,
            verified.preparation.document_unit_snapshot_plan,
            verified.preparation.semantic_route_receipts_plan,
        ):
            payload = (self.root / plan.relpath).read_bytes()
            self.assertEqual(
                "sha256:" + hashlib.sha256(payload).hexdigest(),
                plan.sha256,
            )


if __name__ == "__main__":
    unittest.main()
