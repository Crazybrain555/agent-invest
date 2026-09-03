"""Scratch-PostgreSQL tests for atomic whole-document publication V4."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import os
import stat
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

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
    AtomicPublicationArtifactConflict,
    AtomicPublicationArtifactReadinessError,
)
from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
    PreviousActiveUnitV4,
    UpstreamPublicationEvidenceV4,
    seal_atomic_publication_request_v4,
    seal_pre_id_unit_publication_v4,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    semantic_route_receipts_file_bytes_v3,
)
from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationCommitResponseLost,
    AtomicPublicationUniqueConflict,
    decode_atomic_publication_winner_v4,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
    V4ClaimWitness,
)
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.services.unit_hashing import query_projection
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


class AtomicDocumentPublisherV4IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.fixture = build_v4_authority_fixture()
        self.request = build_atomic_publication_request_v4(self.fixture)
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
        paths = _Paths(self.root)
        self.readiness = FilesystemAtomicPublicationArtifactReadinessV4(
            paths=paths,  # type: ignore[arg-type]
            immutable_store=ImmutableArtifactStore(paths),  # type: ignore[arg-type]
            output_promotion=_ExactTreePromotion(
                root=self.root,
                expected_files=self.output_files,
                inventory_sha256=(
                    self.fixture.local_materialization_receipt.output_files_sha256
                ),
                byte_count=(
                    self.fixture.local_materialization_receipt.output_byte_count
                ),
            ),  # type: ignore[arg-type]
        )
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
                    "provider_sha": self.request.upstream_evidence.provider_document_sha256,
                },
            )

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

    def _claim(self) -> V4ClaimWitness:
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            return uow.remote_parse_v4.load(self.fixture.attempt_id).claim_witness

    def _ready(self, request: AtomicPublicationRequestV4 | None = None):  # type: ignore[no-untyped-def]
        selected = self.request if request is None else request
        reference = self.readiness.prepare_or_replay(
            request=selected,
            checkpoint=self.fixture.local_materialized,
            materialized=self.materialized,
            claim=self._claim(),
            claim_guard=_Guard(),
        )
        return self.readiness.verify_ready(
            reference=reference,
            expected_request=selected,
        )

    def _request_with_unit_pages(
        self,
        page_numbers: tuple[int, ...],
    ) -> AtomicPublicationRequestV4:
        unit_values = asdict(self.request.units[0])
        unit_values.pop("routed_draft_sha256")
        unit_values["page_numbers"] = page_numbers
        unit = seal_pre_id_unit_publication_v4(**unit_values)
        route = replace(
            self.request.semantic_route_receipts[0],
            routed_draft_sha256=unit.routed_draft_sha256,
        )
        semantic_projection = semantic_route_receipts_file_bytes_v3((route,))
        projection = json.loads(self.request.processing_run_projection_json)
        projection["semantic_route_receipts_sha256"] = (
            "sha256:" + hashlib.sha256(semantic_projection).hexdigest()
        )
        projection_json = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return seal_atomic_publication_request_v4(
            identity=self.request.identity,
            upstream_evidence=self.request.upstream_evidence,
            source_page_count=self.request.source_page_count,
            processing_run_projection_json=projection_json,
            processing_run_projection_sha256=(
                "sha256:" + hashlib.sha256(projection_json.encode()).hexdigest()
            ),
            semantic_route_receipts_contract_version=(
                self.request.semantic_route_receipts_contract_version
            ),
            semantic_route_receipts=(route,),
            expected_unit_build_status_before=(
                self.request.expected_unit_build_status_before
            ),
            expected_unit_build_attempt_count_before=(
                self.request.expected_unit_build_attempt_count_before
            ),
            previous_active_units=self.request.previous_active_units,
            previous_active_units_sha256=(self.request.previous_active_units_sha256),
            units=(unit,),
            contract_version=self.request.contract_version,
        )

    def _install_previous_active_run(self) -> AtomicPublicationRequestV4:
        unit = self.request.units[0]
        payload = json.loads(unit.canonical_payload_json)
        query = query_projection(
            payload_kind=unit.payload_kind,
            title=unit.title,
            heading_path=list(unit.heading_path),
            semantic_keys=(
                None if unit.semantic_keys is None else list(unit.semantic_keys)
            ),
            section_keys=(
                None if unit.section_keys is None else list(unit.section_keys)
            ),
            quality_status=unit.quality_status,
            applicability=unit.applicability,
            payload=payload,
        )
        canonical_query = json.dumps(
            query,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        previous_run_id = str(ids.new_processing_run_id())
        previous_asset_id = str(ids.new_asset_id())
        previous = PreviousActiveUnitV4(
            asset_id=previous_asset_id,
            processing_run_id=previous_run_id,
            order_index=unit.unit_index,
            payload_kind=unit.payload_kind,
            heading_path=unit.heading_path,
            content_hash=unit.content_hash,
            query_projection_hash=unit.query_projection_hash,
            canonical_query_projection_json=canonical_query,
        )
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO disclosure_core.processing_run "
                    "(processing_run_id,document_id,artifact_owner_processing_run_id,"
                    "run_kind,status,input_raw_file_hash,provider_document_relpath,"
                    "is_active,unit_build_status) VALUES "
                    "(:run_id,:document_id,:run_id,'parse','succeeded',:source_sha,"
                    "'scratch/old-provider.json',true,'succeeded')"
                ),
                {
                    "run_id": previous_run_id,
                    "document_id": self.fixture.document_id,
                    "source_sha": self.fixture.source_pdf_sha256,
                },
            )
            conn.execute(
                sa.text(
                    "INSERT INTO disclosure_core.document_unit "
                    "(asset_id,document_id,processing_run_id,provider_document_id,"
                    "payload_kind,heading_path,title,order_index,semantic_keys,"
                    "section_keys,payload,content_hash,structure_hash,quality_status,"
                    "applicability,page_no,query_projection_hash,artifact_locator) "
                    "VALUES (:asset_id,:document_id,:run_id,:provider_document_id,"
                    ":payload_kind,CAST(:heading_path AS jsonb),:title,:order_index,"
                    "CAST(:semantic_keys AS jsonb),CAST(:section_keys AS jsonb),"
                    "CAST(:payload AS jsonb),:content_hash,:structure_hash,"
                    ":quality_status,:applicability,:page_no,:query_hash,"
                    "CAST(:locator AS jsonb))"
                ),
                {
                    "asset_id": previous_asset_id,
                    "document_id": self.fixture.document_id,
                    "run_id": previous_run_id,
                    "provider_document_id": unit.provider_document_id,
                    "payload_kind": unit.payload_kind,
                    "heading_path": json.dumps(list(unit.heading_path)),
                    "title": unit.title,
                    "order_index": unit.unit_index,
                    "semantic_keys": (
                        None
                        if unit.semantic_keys is None
                        else json.dumps(unit.semantic_keys)
                    ),
                    "section_keys": (
                        None
                        if unit.section_keys is None
                        else json.dumps(unit.section_keys)
                    ),
                    "payload": unit.canonical_payload_json,
                    "content_hash": unit.content_hash,
                    "structure_hash": unit.structure_hash,
                    "quality_status": unit.quality_status,
                    "applicability": unit.applicability,
                    "page_no": unit.page_no,
                    "query_hash": unit.query_projection_hash,
                    "locator": unit.canonical_artifact_locator_json,
                },
            )
            conn.execute(
                sa.text(
                    "UPDATE disclosure_core.document SET "
                    "current_processing_run_id=:run_id,status='published' "
                    "WHERE document_id=:document_id"
                ),
                {
                    "run_id": previous_run_id,
                    "document_id": self.fixture.document_id,
                },
            )
        return build_atomic_publication_request_v4(
            self.fixture,
            previous_active_run_id=previous_run_id,
            previous_active_units=(previous,),
        )

    def test_initial_commit_replay_and_reload_close_exactly(self) -> None:
        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        claim = self._claim()
        ready = self._ready()
        winner = publisher.commit_whole_document(
            self.request,
            claim=claim,
            artifacts_ready=ready,
        )

        with self.engine.connect() as conn:
            counts_before = tuple(
                conn.execute(
                    sa.text(
                        "SELECT "
                        "(SELECT count(*) FROM disclosure_core.document_unit "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.outbox_event "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.durable_publish_base "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.atomic_publication_winner_v4 "
                        " WHERE document_id=:document_id)"
                    ),
                    {"document_id": self.fixture.document_id},
                ).one()
            )
        self.assertEqual(counts_before, (1, 2, 1, 1))
        self.assertEqual(winner.inserted_count, 1)
        self.assertEqual(winner.updated_count, 0)
        self.assertEqual(winner.deleted_count, 0)
        self.assertEqual(winner.winner_row_version, 2)
        self.assertIsNotNone(winner.artifact_readiness)
        self.assertEqual(
            decode_atomic_publication_winner_v4(winner.canonical_bytes),
            winner,
        )

        replay = publisher.commit_whole_document(
            self.request,
            claim=claim,
            artifacts_ready=ready,
        )
        reloaded = publisher.reload_commit_winner(
            processing_run_id=self.fixture.processing_run_id,
            attempt_id=self.fixture.attempt_id,
        )

        self.assertEqual(replay, winner)
        self.assertEqual(reloaded, winner)
        with self.engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT d.status,d.current_processing_run_id,r.status,"
                    "r.is_active,r.unit_build_status,a.state,"
                    "w.winner_sha256 "
                    "FROM disclosure_core.document d "
                    "JOIN disclosure_core.processing_run r "
                    "ON r.processing_run_id=d.current_processing_run_id "
                    "JOIN disclosure_ops.remote_parse_attempt a "
                    "ON a.processing_run_id=r.processing_run_id "
                    "JOIN disclosure_ops.atomic_publication_winner_v4 w "
                    "ON w.attempt_id=a.attempt_id "
                    "WHERE d.document_id=:document_id"
                ),
                {"document_id": self.fixture.document_id},
            ).one()
            counts_after = tuple(
                conn.execute(
                    sa.text(
                        "SELECT "
                        "(SELECT count(*) FROM disclosure_core.document_unit "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.outbox_event "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.durable_publish_base "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.atomic_publication_winner_v4 "
                        " WHERE document_id=:document_id)"
                    ),
                    {"document_id": self.fixture.document_id},
                ).one()
            )
        self.assertEqual(
            tuple(row),
            (
                "published",
                self.fixture.processing_run_id,
                "succeeded",
                True,
                "succeeded",
                "publish_committed",
                winner.sha256,
            ),
        )
        self.assertEqual(counts_after, counts_before)

    def test_replacement_switches_active_run_with_closed_inventory(self) -> None:
        request = self._install_previous_active_run()
        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)

        winner = publisher.commit_whole_document(
            request,
            claim=self._claim(),
            artifacts_ready=self._ready(request),
        )

        self.assertEqual(
            winner.previous_active_run_id,
            request.identity.expected_previous_processing_run_id,
        )
        self.assertEqual(winner.inserted_count, 1)
        self.assertEqual(winner.updated_count, 0)
        self.assertEqual(winner.deleted_count, 0)
        with self.engine.connect() as conn:
            rows = tuple(
                conn.execute(
                    sa.text(
                        "SELECT processing_run_id,is_active FROM "
                        "disclosure_core.processing_run "
                        "WHERE document_id=:document_id ORDER BY processing_run_id"
                    ),
                    {"document_id": self.fixture.document_id},
                )
            )
            event_kinds = tuple(
                conn.execute(
                    sa.text(
                        "SELECT event_kind FROM disclosure_ops.outbox_event "
                        "WHERE document_id=:document_id ORDER BY seq"
                    ),
                    {"document_id": self.fixture.document_id},
                ).scalars()
            )
        self.assertEqual(sum(bool(row.is_active) for row in rows), 1)
        self.assertIn((self.fixture.processing_run_id, True), rows)
        self.assertEqual(event_kinds, ("processing_run_published",))

    def test_fault_after_db_mutations_rolls_back_every_publication_row(self) -> None:
        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        with patch(
            "disclosure_anchor.adapters.db.postgres.atomic_document_publisher_v4."
            "seal_atomic_publication_winner_v4",
            side_effect=RuntimeError("injected transaction-P fault"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected transaction-P"):
                publisher.commit_whole_document(
                    self.request,
                    claim=self._claim(),
                    artifacts_ready=self._ready(),
                )

        with self.engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT d.status,d.current_processing_run_id,r.is_active,"
                    "r.unit_build_status,a.state,"
                    "(SELECT count(*) FROM disclosure_core.document_unit "
                    " WHERE document_id=d.document_id),"
                    "(SELECT count(*) FROM disclosure_ops.outbox_event "
                    " WHERE document_id=d.document_id),"
                    "(SELECT count(*) FROM disclosure_ops.durable_publish_base "
                    " WHERE document_id=d.document_id),"
                    "(SELECT count(*) FROM disclosure_ops.atomic_publication_winner_v4 "
                    " WHERE document_id=d.document_id) "
                    "FROM disclosure_core.document d "
                    "JOIN disclosure_core.processing_run r "
                    "ON r.document_id=d.document_id "
                    "JOIN disclosure_ops.remote_parse_attempt a "
                    "ON a.processing_run_id=r.processing_run_id "
                    "WHERE d.document_id=:document_id"
                ),
                {"document_id": self.fixture.document_id},
            ).one()
        self.assertEqual(
            tuple(row),
            ("parsed", None, False, "running", "local_materialized", 0, 0, 0, 0),
        )

    def test_previous_inventory_drift_fails_before_publication(self) -> None:
        request = self._install_previous_active_run()
        previous_run_id = request.identity.expected_previous_processing_run_id
        assert previous_run_id is not None
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE disclosure_core.document_unit SET "
                    "content_hash=:drifted WHERE processing_run_id=:run_id"
                ),
                {
                    "drifted": "sha256:" + "f" * 64,
                    "run_id": previous_run_id,
                },
            )

        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        with self.assertRaisesRegex(
            AtomicPublicationUniqueConflict,
            "inventory drifted",
        ):
            publisher.commit_whole_document(
                request,
                claim=self._claim(),
                artifacts_ready=self._ready(request),
            )

        with self.engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT d.current_processing_run_id,"
                    "(SELECT count(*) FROM disclosure_core.document_unit "
                    " WHERE processing_run_id=:candidate),"
                    "(SELECT count(*) FROM disclosure_ops.outbox_event "
                    " WHERE document_id=d.document_id),"
                    "(SELECT count(*) FROM disclosure_ops.durable_publish_base "
                    " WHERE document_id=d.document_id) "
                    "FROM disclosure_core.document d "
                    "WHERE d.document_id=:document_id"
                ),
                {
                    "candidate": self.fixture.processing_run_id,
                    "document_id": self.fixture.document_id,
                },
            ).one()
        self.assertEqual(tuple(row), (previous_run_id, 0, 0, 0))

    def test_reload_rejects_a_tampered_committed_unit_row(self) -> None:
        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        publisher.commit_whole_document(
            self.request,
            claim=self._claim(),
            artifacts_ready=self._ready(),
        )
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE disclosure_core.document_unit SET title='tampered' "
                    "WHERE processing_run_id=:run_id"
                ),
                {"run_id": self.fixture.processing_run_id},
            )

        with self.assertRaisesRegex(
            AtomicPublicationUniqueConflict,
            "Unit row hash drifted",
        ):
            publisher.reload_commit_winner(
                processing_run_id=self.fixture.processing_run_id,
                attempt_id=self.fixture.attempt_id,
            )

    def test_commit_response_loss_requires_reload_of_durable_winner(self) -> None:
        class Disconnected(Exception):
            sqlstate = "08006"

        original_commit = SqlAlchemyUnitOfWork.commit

        def commit_then_disconnect(uow: SqlAlchemyUnitOfWork) -> None:
            original_commit(uow)
            uow.session.connection().invalidate()
            raise DBAPIError(
                "COMMIT",
                {},
                Disconnected("commit response lost"),
                connection_invalidated=True,
            )

        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        with patch.object(
            SqlAlchemyUnitOfWork,
            "commit",
            autospec=True,
            side_effect=commit_then_disconnect,
        ):
            with self.assertRaisesRegex(
                AtomicPublicationCommitResponseLost,
                "reload winner",
            ):
                publisher.commit_whole_document(
                    self.request,
                    claim=self._claim(),
                    artifacts_ready=self._ready(),
                )

        winner = publisher.reload_commit_winner(
            processing_run_id=self.fixture.processing_run_id,
            attempt_id=self.fixture.attempt_id,
        )
        self.assertIsNotNone(winner)
        assert winner is not None
        self.assertEqual(winner.request_sha256, self.request.request_sha256)

    def test_precommit_hashes_the_actual_persisted_unit_row(self) -> None:
        from disclosure_anchor.adapters.db.postgres import (
            atomic_document_publisher_v4 as publisher_module,
        )

        original = publisher_module._document_unit

        def drifted_unit(unit, *, asset_id):  # type: ignore[no-untyped-def]
            return replace(original(unit, asset_id=asset_id), title="mapper drift")

        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        with patch.object(
            publisher_module,
            "_document_unit",
            side_effect=drifted_unit,
        ):
            with self.assertRaisesRegex(
                AtomicPublicationUniqueConflict,
                "Unit row hash drifted",
            ):
                publisher.commit_whole_document(
                    self.request,
                    claim=self._claim(),
                    artifacts_ready=self._ready(),
                )
        with self.engine.connect() as conn:
            counts = tuple(
                conn.execute(
                    sa.text(
                        "SELECT "
                        "(SELECT count(*) FROM disclosure_core.document_unit "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.outbox_event "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.durable_publish_base "
                        " WHERE document_id=:document_id)"
                    ),
                    {"document_id": self.fixture.document_id},
                ).one()
            )
        self.assertEqual(counts, (0, 0, 0))

    def test_upstream_counts_must_match_durable_materialization_evidence(self) -> None:
        values = asdict(self.request.upstream_evidence)
        values.pop("evidence_sha256")
        values["output_total_byte_count"] += 1
        evidence_sha256 = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    values,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        drifted_upstream = UpstreamPublicationEvidenceV4(
            **values,
            evidence_sha256=evidence_sha256,
        )
        drifted_request = seal_atomic_publication_request_v4(
            identity=self.request.identity,
            upstream_evidence=drifted_upstream,
            source_page_count=self.request.source_page_count,
            processing_run_projection_json=(
                self.request.processing_run_projection_json
            ),
            processing_run_projection_sha256=(
                self.request.processing_run_projection_sha256
            ),
            semantic_route_receipts_contract_version=(
                self.request.semantic_route_receipts_contract_version
            ),
            semantic_route_receipts=self.request.semantic_route_receipts,
            expected_unit_build_status_before=(
                self.request.expected_unit_build_status_before
            ),
            expected_unit_build_attempt_count_before=(
                self.request.expected_unit_build_attempt_count_before
            ),
            previous_active_units=self.request.previous_active_units,
            previous_active_units_sha256=(self.request.previous_active_units_sha256),
            units=self.request.units,
            contract_version=self.request.contract_version,
        )

        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        with self.assertRaisesRegex(
            AtomicPublicationArtifactReadinessError,
            "input evidence drifted",
        ):
            publisher.commit_whole_document(
                drifted_request,
                claim=self._claim(),
                artifacts_ready=self._ready(drifted_request),
            )

    def test_document_source_authority_drift_fails_before_publication(self) -> None:
        context = self.fixture.materialization_intent.provider_envelope_context
        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        for column, drifted, restored in (
            ("provider", "provider-other", context.provider),
            (
                "raw_file_hash",
                "sha256:" + "f" * 64,
                self.fixture.source_pdf_sha256,
            ),
        ):
            with self.subTest(column=column):
                with self.engine.begin() as conn:
                    conn.execute(
                        sa.text(
                            f"UPDATE disclosure_core.document SET {column}=:value "
                            "WHERE document_id=:document_id"
                        ),
                        {
                            "value": drifted,
                            "document_id": self.fixture.document_id,
                        },
                    )
                try:
                    with self.assertRaisesRegex(
                        AtomicPublicationUniqueConflict,
                        "document provenance drifted",
                    ):
                        publisher.commit_whole_document(
                            self.request,
                            claim=self._claim(),
                            artifacts_ready=self._ready(),
                        )
                finally:
                    with self.engine.begin() as conn:
                        conn.execute(
                            sa.text(
                                f"UPDATE disclosure_core.document SET {column}=:value "
                                "WHERE document_id=:document_id"
                            ),
                            {
                                "value": restored,
                                "document_id": self.fixture.document_id,
                            },
                        )

        with self.engine.connect() as conn:
            counts = tuple(
                conn.execute(
                    sa.text(
                        "SELECT "
                        "(SELECT count(*) FROM disclosure_core.document_unit "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.outbox_event "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.atomic_publication_winner_v4 "
                        " WHERE document_id=:document_id)"
                    ),
                    {"document_id": self.fixture.document_id},
                ).one()
            )
        self.assertEqual(counts, (0, 0, 0))

    def test_every_nonempty_build_prestate_fails_before_publication(self) -> None:
        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        cases = (
            (
                "normalized_ir_relpath",
                "normalized_ir_relpath='derived/normalized_ir/drift.json',"
                "provider_document_relpath=NULL",
            ),
            (
                "unit_build_error",
                "unit_build_error=jsonb_build_object("
                "'stage','unit_build','error_code','drift',"
                "'retryable',false)",
            ),
            ("document_units_relpath", "document_units_relpath='drift/units.jsonl'"),
            (
                "semantic_route_receipts_hash",
                "document_units_relpath='drift/units.jsonl',"
                "semantic_route_receipts_hash='sha256:" + "f" * 64 + "'",
            ),
            (
                "semantic_route_receipts_relpath",
                "document_units_relpath='drift/units.jsonl',"
                "semantic_route_receipts_hash='sha256:"
                + "f"
                * 64
                + "',semantic_route_receipts_relpath='drift/routes.jsonl',"
                "semantic_route_receipts_contract_version="
                "'semantic_route_receipt.v3'",
            ),
            (
                "semantic_route_receipts_contract_version",
                "document_units_relpath='drift/units.jsonl',"
                "semantic_route_receipts_hash='sha256:"
                + "f"
                * 64
                + "',semantic_route_receipts_relpath='drift/routes.jsonl',"
                "semantic_route_receipts_contract_version="
                "'semantic_route_receipt.v2'",
            ),
            (
                "semantic_adjudication_status",
                "semantic_adjudication_status='not_required'",
            ),
            ("semantic_degraded_unit_count", "semantic_degraded_unit_count=0"),
            ("semantic_failover_group_count", "semantic_failover_group_count=0"),
            (
                "semantic_adjudication_summary",
                "semantic_adjudication_summary='{}'::jsonb",
            ),
            (
                "content_hash_aggregate",
                "content_hash_aggregate='sha256:" + "e" * 64 + "'",
            ),
            ("structure_hash", "structure_hash='sha256:" + "d" * 64 + "'"),
            ("builder_rules_version", "builder_rules_version='drift.v1'"),
            ("unit_built_at", "unit_built_at=now()"),
            ("unit_build_status", "unit_build_status='failed'"),
            ("unit_build_attempt_count", "unit_build_attempt_count=1"),
        )
        with self.engine.connect() as conn:
            baseline_provider_relpath = conn.execute(
                sa.text(
                    "SELECT provider_document_relpath FROM "
                    "disclosure_core.processing_run "
                    "WHERE processing_run_id=:run_id"
                ),
                {"run_id": self.fixture.processing_run_id},
            ).scalar_one()

        reset = sa.text(
            "UPDATE disclosure_core.processing_run SET "
            "provider_document_relpath=:provider_relpath,"
            "normalized_ir_relpath=NULL,unit_build_error=NULL,"
            "document_units_relpath=NULL,semantic_route_receipts_hash=NULL,"
            "semantic_route_receipts_relpath=NULL,"
            "semantic_route_receipts_contract_version=NULL,"
            "semantic_adjudication_status=NULL,"
            "semantic_degraded_unit_count=NULL,"
            "semantic_failover_group_count=NULL,"
            "semantic_adjudication_summary=NULL,content_hash_aggregate=NULL,"
            "structure_hash=NULL,builder_rules_version=NULL,unit_built_at=NULL,"
            "unit_build_status='running',unit_build_attempt_count=0 "
            "WHERE processing_run_id=:run_id"
        )
        for label, assignment in cases:
            with self.subTest(prestate=label):
                with self.engine.begin() as conn:
                    conn.execute(
                        sa.text(
                            "UPDATE disclosure_core.processing_run SET "
                            + assignment
                            + " WHERE processing_run_id=:run_id"
                        ),
                        {"run_id": self.fixture.processing_run_id},
                    )
                try:
                    with self.assertRaisesRegex(
                        AtomicPublicationUniqueConflict,
                        "candidate provenance drifted",
                    ):
                        publisher.commit_whole_document(
                            self.request,
                            claim=self._claim(),
                            artifacts_ready=self._ready(),
                        )
                finally:
                    with self.engine.begin() as conn:
                        conn.execute(
                            reset,
                            {
                                "run_id": self.fixture.processing_run_id,
                                "provider_relpath": baseline_provider_relpath,
                            },
                        )

        with self.engine.connect() as conn:
            counts = tuple(
                conn.execute(
                    sa.text(
                        "SELECT "
                        "(SELECT count(*) FROM disclosure_core.document_unit "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.outbox_event "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM "
                        " disclosure_ops.atomic_publication_winner_v4 "
                        " WHERE document_id=:document_id)"
                    ),
                    {"document_id": self.fixture.document_id},
                ).one()
            )
        self.assertEqual(counts, (0, 0, 0))

    def test_postcommit_cleanup_failure_is_typed_for_reload(self) -> None:
        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        claim = self._claim()
        ready = self._ready()
        with patch(
            "disclosure_anchor.adapters.db.postgres.unit_of_work."
            "release_corpus_write_session_lock",
            side_effect=RuntimeError("injected unlock failure"),
        ):
            with self.assertRaisesRegex(
                AtomicPublicationCommitResponseLost,
                "cleanup failed",
            ):
                publisher.commit_whole_document(
                    self.request,
                    claim=claim,
                    artifacts_ready=ready,
                )

        winner = publisher.reload_commit_winner(
            processing_run_id=self.fixture.processing_run_id,
            attempt_id=self.fixture.attempt_id,
        )
        self.assertIsNotNone(winner)

    def test_uow_cleanup_failure_does_not_mask_the_primary_exception(self) -> None:
        primary = RuntimeError("primary transaction failure")
        with patch(
            "disclosure_anchor.adapters.db.postgres.unit_of_work."
            "release_corpus_write_session_lock",
            side_effect=RuntimeError("injected cleanup failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "primary transaction failure",
            ) as raised:
                with SqlAlchemyUnitOfWork(engine=self.engine):
                    raise primary
        self.assertIs(raised.exception, primary)
        self.assertTrue(
            any(
                "cleanup also failed" in note
                for note in getattr(raised.exception, "__notes__", ())
            )
        )

    def test_concurrent_exact_requests_return_one_immutable_winner(self) -> None:
        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        claim = self._claim()
        ready = self._ready()
        barrier = Barrier(2)

        def publish():  # type: ignore[no-untyped-def]
            barrier.wait(timeout=10)
            return publisher.commit_whole_document(
                self.request,
                claim=claim,
                artifacts_ready=ready,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = tuple(pool.map(lambda _item: publish(), range(2)))

        self.assertEqual(winners[0], winners[1])
        with self.engine.connect() as conn:
            counts = tuple(
                conn.execute(
                    sa.text(
                        "SELECT "
                        "(SELECT count(*) FROM disclosure_core.document_unit "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.outbox_event "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.durable_publish_base "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.atomic_publication_winner_v4 "
                        " WHERE document_id=:document_id)"
                    ),
                    {"document_id": self.fixture.document_id},
                ).one()
            )
        self.assertEqual(counts, (1, 2, 1, 1))

    def test_concurrent_different_requests_admit_only_one_winner(self) -> None:
        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        requests = (self.request, self._request_with_unit_pages((1, 2)))
        claim = self._claim()
        first_ready = self._ready()
        barrier = Barrier(2)

        def publish(request):  # type: ignore[no-untyped-def]
            barrier.wait(timeout=10)
            try:
                return (
                    "winner",
                    publisher.commit_whole_document(
                        request,
                        claim=claim,
                        artifacts_ready=(
                            first_ready
                            if request is self.request
                            else self._ready(request)
                        ),
                    ),
                )
            except (
                AtomicPublicationArtifactConflict,
                AtomicPublicationUniqueConflict,
            ) as exc:
                return ("conflict", str(exc))

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = tuple(pool.map(publish, requests))

        self.assertEqual(
            sorted(kind for kind, _value in outcomes), ["conflict", "winner"]
        )
        with self.engine.connect() as conn:
            counts = tuple(
                conn.execute(
                    sa.text(
                        "SELECT "
                        "(SELECT count(*) FROM disclosure_core.document_unit "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.outbox_event "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.durable_publish_base "
                        " WHERE document_id=:document_id),"
                        "(SELECT count(*) FROM disclosure_ops.atomic_publication_winner_v4 "
                        " WHERE document_id=:document_id)"
                    ),
                    {"document_id": self.fixture.document_id},
                ).one()
            )
        self.assertEqual(counts, (1, 2, 1, 1))


if __name__ == "__main__":
    unittest.main()
