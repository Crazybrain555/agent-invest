from pathlib import Path
import tempfile
import unittest

from disclosure_anchor.adapters.storage.immutable_artifact_store import ImmutableArtifactStore
from disclosure_anchor.adapters.storage.v4_execution_spec_catalog import ImmutableV4ExecutionSpecCatalog
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactConflict, AtomicPublicationArtifactReadinessError,
)
from disclosure_anchor.application.ports.v4_execution_spec_catalog import V4ExecutionSpecCatalogReference
from tests.unit.test_v4_prepared_execution_spec import _spec


class _DataPaths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def data_path(self, relpath: Path) -> Path:
        return self.root / relpath


class _CatalogPaths:
    def __init__(self, fixed: Path | None = None) -> None:
        self.fixed = fixed

    def v4_execution_spec_relpath(self, *, spec_sha256: str) -> Path:
        return self.fixed or Path("derived/v4_execution_specs", f"sha256_{spec_sha256[7:]}.json")


class LegacyExecutionSpecCatalogTests(unittest.TestCase):
    def test_exact_read_only_import_and_identity_failures(self) -> None:
        spec = _spec()
        with tempfile.TemporaryDirectory() as root:
            paths = _CatalogPaths()
            store = ImmutableArtifactStore(_DataPaths(Path(root)))
            # Model a pre-cutover artifact; production catalog has no writer.
            store.create_or_verify(
                relpath=paths.v4_execution_spec_relpath(spec_sha256=spec.sha256),
                payload=spec.exact_bytes,
            )
            catalog = ImmutableV4ExecutionSpecCatalog(paths=paths, immutable_store=store)
            reference = V4ExecutionSpecCatalogReference(spec.sha256, spec.byte_count)
            self.assertEqual(catalog.load(reference=reference), spec)
            self.assertFalse(hasattr(catalog, "store_or_replay"))
            with self.assertRaises(AtomicPublicationArtifactConflict):
                catalog.load(reference=V4ExecutionSpecCatalogReference(spec.sha256, spec.byte_count-1))
            with self.assertRaises(AtomicPublicationArtifactReadinessError):
                catalog.load(reference=V4ExecutionSpecCatalogReference("sha256:"+"f"*64, spec.byte_count))
            self.assertEqual(len(tuple(Path(root).rglob("*.json"))), 1)

    def test_invalid_path_is_rejected_before_io(self) -> None:
        spec = _spec()
        for path in (Path("/tmp/spec.json"), Path("derived/../spec.json")):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as root:
                catalog = ImmutableV4ExecutionSpecCatalog(
                    paths=_CatalogPaths(path),
                    immutable_store=ImmutableArtifactStore(_DataPaths(Path(root))),
                )
                with self.assertRaisesRegex(ValueError, "path is not closed"):
                    catalog.load(reference=V4ExecutionSpecCatalogReference(spec.sha256, spec.byte_count))
                self.assertEqual(tuple(Path(root).rglob("*")), ())
