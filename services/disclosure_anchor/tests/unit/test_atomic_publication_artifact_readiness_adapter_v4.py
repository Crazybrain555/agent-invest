from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import tempfile
import unittest

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
)
from disclosure_anchor.application.ports.file_store import ArtifactWriteResult
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    validate_atomic_publication_artifacts_ready_v4,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
    V4ClaimWitness,
)
from tests.unit.test_atomic_document_publication_v4 import (
    _previous_active_unit,
    _publication_materialized_evidence,
    _request,
)


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


class _RecordingStore:
    def __init__(self, delegate: ImmutableArtifactStore) -> None:
        self.delegate = delegate
        self.created: list[str] = []

    def create_or_verify(
        self,
        *,
        relpath: Path,
        payload: bytes,
    ) -> ArtifactWriteResult:
        self.created.append(relpath.name)
        return self.delegate.create_or_verify(relpath=relpath, payload=payload)

    def read_exact(self, **kwargs: object) -> bytes:
        return self.delegate.read_exact(**kwargs)  # type: ignore[arg-type]


class _FailAfterCreateStore(_RecordingStore):
    def __init__(
        self,
        delegate: ImmutableArtifactStore,
        *,
        fail_name: str,
    ) -> None:
        super().__init__(delegate)
        self.fail_name = fail_name
        self.failed = False

    def create_or_verify(
        self,
        *,
        relpath: Path,
        payload: bytes,
    ) -> ArtifactWriteResult:
        result = super().create_or_verify(relpath=relpath, payload=payload)
        if relpath.name == self.fail_name and not self.failed:
            self.failed = True
            raise RuntimeError(f"after {self.fail_name}")
        return result


class _Promotion:
    def __init__(
        self,
        events: list[str],
        *,
        fail_after_promote: bool = False,
    ) -> None:
        self.events = events
        self.published: tuple[str, str, int, int] | None = None
        self.fail_after_promote = fail_after_promote

    def promote_or_replay(self, **kwargs: object) -> None:
        materialized = kwargs["materialized"]
        published_relpath = kwargs["published_relpath"]
        self.events.append("parser-output")
        self.published = (
            str(published_relpath),
            materialized.receipt.output_files_sha256,  # type: ignore[union-attr]
            materialized.receipt.output_file_count,  # type: ignore[union-attr]
            materialized.receipt.output_byte_count,  # type: ignore[union-attr]
        )
        if self.fail_after_promote:
            self.fail_after_promote = False
            raise RuntimeError("after parser-output")

    def verify_published(self, **kwargs: object) -> None:
        self.events.append("verify-parser-output")
        expected = (
            str(kwargs["published_relpath"]),
            str(kwargs["expected_inventory_sha256"]),
            int(kwargs["expected_file_count"]),
            int(kwargs["expected_byte_count"]),
        )
        if self.published != expected:
            raise AssertionError("parser output verification drifted")


class _Guard:
    def assert_current_under_resource_lock(self, **kwargs: object) -> None:
        return None


class AtomicPublicationArtifactReadinessAdapterV4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = _request()
        _, checkpoint, intent, receipt, manifest, envelope = (
            _publication_materialized_evidence()
        )
        self.checkpoint = checkpoint
        self.materialized = MaterializedProviderDocumentV4(
            receipt=receipt,
            intent=intent,
            provider_envelope=envelope,
            manifest=manifest,
        )
        self.claim = V4ClaimWitness(
            attempt_id=checkpoint.attempt_id,
            fence_identity=checkpoint.fence_identity,
            state=checkpoint.state,
            lifecycle_version=checkpoint.lifecycle_version,
            checkpoint_sha256=checkpoint.sha256,
            claim_owner_identity="worker-1",
            claim_generation=1,
        )

    def test_prepares_all_exact_resources_and_writes_readiness_last(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            paths = _Paths(root)
            store = _RecordingStore(
                ImmutableArtifactStore(paths)  # type: ignore[arg-type]
            )
            events: list[str] = []
            adapter = FilesystemAtomicPublicationArtifactReadinessV4(
                paths=paths,  # type: ignore[arg-type]
                immutable_store=store,
                output_promotion=_Promotion(events),  # type: ignore[arg-type]
            )

            reference = adapter.prepare_or_replay(
                request=self.request,
                checkpoint=self.checkpoint,
                materialized=self.materialized,
                claim=self.claim,
                claim_guard=_Guard(),
            )
            witness = adapter.verify_ready(
                reference=reference,
                expected_request=self.request,
            )

            self.assertEqual(witness.reference, reference)
            with self.assertRaises(FrozenInstanceError):
                witness.reference = reference  # type: ignore[misc]
            self.assertEqual(
                store.created,
                [
                    ATOMIC_PUBLICATION_PREPARATION_FILENAME,
                    "provider_document.v1.json",
                    "document_units.v1.jsonl",
                    "semantic_route_receipts.v3.jsonl",
                    ATOMIC_PUBLICATION_READINESS_FILENAME,
                ],
            )
            self.assertEqual(events[0], "parser-output")
            semantic = root / witness.preparation.semantic_route_receipts_plan.relpath
            self.assertFalse(semantic.read_bytes().startswith(b"["))
            self.assertTrue(semantic.read_bytes().endswith(b"\n"))

            replay = adapter.prepare_or_replay(
                request=self.request,
                checkpoint=self.checkpoint,
                materialized=self.materialized,
                claim=self.claim,
                claim_guard=_Guard(),
            )
            self.assertEqual(replay, reference)
            replay_witness = adapter.verify_ready(
                reference=replay,
                expected_request=self.request,
            )
            self.assertEqual(
                replay_witness.preparation.unit_bindings,
                witness.preparation.unit_bindings,
            )
            object.__setattr__(
                replay_witness,
                "reference",
                replace(
                    replay_witness.reference,
                    manifest_relpath=(
                        "derived/document_unit_snapshots/forged/run/"
                        "atomic_publication_readiness.v1.json"
                    ),
                ),
            )
            with self.assertRaisesRegex(ValueError, "fixed siblings"):
                validate_atomic_publication_artifacts_ready_v4(
                    request=self.request,
                    artifacts_ready=replay_witness,
                )

    def test_resource_drift_and_different_request_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            paths = _Paths(root)
            promotion = _Promotion([])
            adapter = FilesystemAtomicPublicationArtifactReadinessV4(
                paths=paths,  # type: ignore[arg-type]
                immutable_store=ImmutableArtifactStore(paths),  # type: ignore[arg-type]
                output_promotion=promotion,  # type: ignore[arg-type]
            )
            reference = adapter.prepare_or_replay(
                request=self.request,
                checkpoint=self.checkpoint,
                materialized=self.materialized,
                claim=self.claim,
                claim_guard=_Guard(),
            )
            projection = json.loads(self.request.processing_run_projection_json)
            semantic = root / projection["semantic_route_receipts_relpath"]
            semantic.write_bytes(b"{}\n")
            with self.assertRaises(AtomicPublicationArtifactConflict):
                adapter.verify_ready(
                    reference=reference,
                    expected_request=self.request,
                )

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            paths = _Paths(root)
            promotion = _Promotion([])
            adapter = FilesystemAtomicPublicationArtifactReadinessV4(
                paths=paths,  # type: ignore[arg-type]
                immutable_store=ImmutableArtifactStore(paths),  # type: ignore[arg-type]
                output_promotion=promotion,  # type: ignore[arg-type]
            )
            adapter.prepare_or_replay(
                request=self.request,
                checkpoint=self.checkpoint,
                materialized=self.materialized,
                claim=self.claim,
                claim_guard=_Guard(),
            )
            different = _request(
                previous_active_run_id="run-old",
                previous_active_units=(_previous_active_unit(self.request),),
            )
            with self.assertRaises(AtomicPublicationArtifactConflict):
                adapter.prepare_or_replay(
                    request=different,
                    checkpoint=self.checkpoint,
                    materialized=self.materialized,
                    claim=self.claim,
                    claim_guard=_Guard(),
                )

    def test_every_outer_readiness_boundary_replays_with_the_same_unit_ids(self) -> None:
        boundaries = (
            ATOMIC_PUBLICATION_PREPARATION_FILENAME,
            "parser-output",
            "provider_document.v1.json",
            "document_units.v1.jsonl",
            "semantic_route_receipts.v3.jsonl",
            ATOMIC_PUBLICATION_READINESS_FILENAME,
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                paths = _Paths(root)
                delegate = ImmutableArtifactStore(paths)  # type: ignore[arg-type]
                store = _FailAfterCreateStore(
                    delegate,
                    fail_name=boundary,
                )
                promotion = _Promotion(
                    [],
                    fail_after_promote=boundary == "parser-output",
                )
                failing = FilesystemAtomicPublicationArtifactReadinessV4(
                    paths=paths,  # type: ignore[arg-type]
                    immutable_store=store,
                    output_promotion=promotion,  # type: ignore[arg-type]
                )
                with self.assertRaisesRegex(RuntimeError, "after"):
                    failing.prepare_or_replay(
                        request=self.request,
                        checkpoint=self.checkpoint,
                        materialized=self.materialized,
                        claim=self.claim,
                        claim_guard=_Guard(),
                    )
                preparation = failing.load_preparation(request=self.request)
                self.assertIsNotNone(preparation)
                assert preparation is not None
                self.assertEqual(
                    FilesystemAtomicPublicationArtifactReadinessV4(
                        paths=paths,  # type: ignore[arg-type]
                        immutable_store=delegate,
                        output_promotion=promotion,  # type: ignore[arg-type]
                    ).reopen_prepared_request(
                        checkpoint=self.checkpoint,
                        materialized=self.materialized,
                    ),
                    self.request,
                )
                assigned_ids = tuple(
                    item.asset_id for item in preparation.unit_bindings
                )

                replay = FilesystemAtomicPublicationArtifactReadinessV4(
                    paths=paths,  # type: ignore[arg-type]
                    immutable_store=delegate,
                    output_promotion=promotion,  # type: ignore[arg-type]
                )
                reference = replay.prepare_or_replay(
                    request=self.request,
                    checkpoint=self.checkpoint,
                    materialized=self.materialized,
                    claim=self.claim,
                    claim_guard=_Guard(),
                )
                witness = replay.verify_ready(
                    reference=reference,
                    expected_request=self.request,
                )
                self.assertEqual(
                    tuple(item.asset_id for item in witness.preparation.unit_bindings),
                    assigned_ids,
                )
                self.assertEqual(
                    len(tuple(root.rglob(ATOMIC_PUBLICATION_READINESS_FILENAME))),
                    1,
                )


if __name__ == "__main__":
    unittest.main()
