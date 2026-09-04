from __future__ import annotations

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
)
from disclosure_anchor.application.services.atomic_publication_request_factory_v4 import (
    RecoverableAtomicPublicationRequestFactoryV4,
)
from tests.unit.test_atomic_publication_artifact_readiness_adapter_v4 import (
    _Guard,
    _Paths,
    _Promotion,
)
from tests.unit.test_atomic_document_publication_v4 import (
    _publication_materialized_evidence,
    _request,
)


class _StageGuard:
    def __init__(self) -> None:
        self.calls = 0

    def checkpoint(self) -> None:
        self.calls += 1


class _Builder:
    def __init__(self, request: object) -> None:
        self.request = request
        self.calls = 0

    def build(self, **_: object) -> object:
        self.calls += 1
        return self.request


class AtomicPublicationRequestFactoryV4Tests(unittest.TestCase):
    def test_fresh_factory_reopens_prepared_request_without_rebuilding(self) -> None:
        request = _request()
        _, checkpoint, intent, receipt, manifest, envelope = (
            _publication_materialized_evidence()
        )
        from disclosure_anchor.application.ports.staged_provider_parser import (
            MaterializedProviderDocumentV4,
            V4ClaimWitness,
        )

        materialized = MaterializedProviderDocumentV4(
            receipt=receipt,
            intent=intent,
            provider_envelope=envelope,
            manifest=manifest,
        )
        claim = V4ClaimWitness(
            attempt_id=checkpoint.attempt_id,
            fence_identity=checkpoint.fence_identity,
            state=checkpoint.state,
            lifecycle_version=checkpoint.lifecycle_version,
            checkpoint_sha256=checkpoint.sha256,
            claim_owner_identity="worker-1",
            claim_generation=1,
        )
        with tempfile.TemporaryDirectory() as raw_root:
            paths = _Paths(Path(raw_root))
            store = ImmutableArtifactStore(paths)  # type: ignore[arg-type]
            promotion = _Promotion([])
            first_readiness = FilesystemAtomicPublicationArtifactReadinessV4(
                paths=paths,  # type: ignore[arg-type]
                immutable_store=store,
                output_promotion=promotion,  # type: ignore[arg-type]
            )
            initial_builder = _Builder(request)
            guard = _StageGuard()
            first = RecoverableAtomicPublicationRequestFactoryV4(
                readiness=first_readiness,
                new_request_builder=initial_builder,  # type: ignore[arg-type]
            ).build_or_reopen(
                checkpoint=checkpoint,
                materialized=materialized,
                stage_guard=guard,
            )
            self.assertEqual(first, request)
            self.assertEqual(initial_builder.calls, 1)

            first_readiness.prepare_or_replay(
                request=first,
                checkpoint=checkpoint,
                materialized=materialized,
                claim=claim,
                claim_guard=_Guard(),
            )

            forbidden_builder = _Builder(object())
            reopened = RecoverableAtomicPublicationRequestFactoryV4(
                readiness=FilesystemAtomicPublicationArtifactReadinessV4(
                    paths=paths,  # type: ignore[arg-type]
                    immutable_store=store,
                    output_promotion=promotion,  # type: ignore[arg-type]
                ),
                new_request_builder=forbidden_builder,  # type: ignore[arg-type]
            ).build_or_reopen(
                checkpoint=checkpoint,
                materialized=materialized,
                stage_guard=_StageGuard(),
            )
            self.assertEqual(reopened, request)
            self.assertEqual(forbidden_builder.calls, 0)
            self.assertTrue(
                any(
                    path.name == ATOMIC_PUBLICATION_PREPARATION_FILENAME
                    for path in Path(raw_root).rglob("*.json")
                )
            )
            self.assertTrue(
                any(
                    path.name == ATOMIC_PUBLICATION_READINESS_FILENAME
                    for path in Path(raw_root).rglob("*.json")
                )
            )


if __name__ == "__main__":
    unittest.main()
