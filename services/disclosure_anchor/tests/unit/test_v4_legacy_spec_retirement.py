from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from disclosure_anchor.adapters.storage.immutable_artifact_store import ImmutableArtifactStore
from disclosure_anchor.adapters.storage.v4_legacy_spec_retirement import LegacyV4ExecutionSpecRetirer
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import AtomicPublicationArtifactConflict
from disclosure_anchor.application.ports.v4_execution_spec_catalog import V4ExecutionSpecCatalogReference
from disclosure_anchor.cli.v4_spec_maintenance import main
from tests.unit.test_v4_execution_spec_catalog import _CatalogPaths
from tests.unit.test_v4_prepared_execution_spec import _spec


class _RetirementPaths(_CatalogPaths):
    def __init__(self, root):
        super().__init__()
        self.root = root

    def data_path(self, relative):
        return self.root/relative


class LegacySpecRetirementTests(unittest.TestCase):
    def _fixture(self, root):
        paths = _RetirementPaths(Path(root))
        spec = _spec()
        relative = paths.v4_execution_spec_relpath(spec_sha256=spec.sha256)
        ImmutableArtifactStore(paths).create_or_verify(relpath=relative, payload=spec.exact_bytes)
        return paths, spec, paths.data_path(relative), V4ExecutionSpecCatalogReference(spec.sha256, spec.byte_count)

    def test_exact_selected_file_and_replayed_absence(self):
        with tempfile.TemporaryDirectory() as root:
            paths, spec, path, reference = self._fixture(root)
            authorize = Mock()
            retire = LegacyV4ExecutionSpecRetirer(paths)
            self.assertTrue(retire.retire_exact(reference=reference, authorize=authorize))
            authorize.assert_called_once_with(spec)
            self.assertFalse(path.exists())
            self.assertFalse(retire.retire_exact(reference=reference, authorize=authorize))
            authorize.assert_called_once_with(spec)

    def test_denied_or_cancelled_authority_never_deletes(self):
        with tempfile.TemporaryDirectory() as root:
            paths, spec, path, reference = self._fixture(root)
            with self.assertRaisesRegex(RuntimeError, "not copied"):
                LegacyV4ExecutionSpecRetirer(paths).retire_exact(
                    reference=reference, authorize=Mock(side_effect=RuntimeError("not copied or revoked")),
                )
            self.assertEqual(path.read_bytes(), spec.exact_bytes)

    def test_hash_size_link_and_identity_races_preserve_evidence(self):
        for mode in ("hash", "size", "hardlink", "symlink", "file-replace", "root-replace"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                paths, spec, path, reference = self._fixture(root)
                saved = path.with_name("retained-original")
                authorize = Mock()
                if mode == "hash":
                    path.write_bytes(spec.exact_bytes[:-1]+b"!")
                elif mode == "size":
                    path.write_bytes(b"oversize-or-truncated")
                elif mode == "hardlink":
                    os.link(path, saved)
                elif mode == "symlink":
                    path.rename(saved)
                    path.symlink_to(saved)
                elif mode == "file-replace":
                    def replace_file(_spec):
                        path.rename(saved)
                        path.write_bytes(spec.exact_bytes)
                        path.chmod(0o600)
                    authorize.side_effect = replace_file
                else:
                    def replace_root(_spec):
                        parent = path.parent
                        parent.rename(parent.with_name("retained-parent"))
                        parent.mkdir(mode=0o700)
                    authorize.side_effect = replace_root
                with self.assertRaises((AtomicPublicationArtifactConflict, OSError)):
                    LegacyV4ExecutionSpecRetirer(paths).retire_exact(reference=reference, authorize=authorize)
                if mode not in {"file-replace", "root-replace"}:
                    authorize.assert_not_called()
                self.assertTrue(path.exists() if mode != "root-replace" else path.parent.with_name("retained-parent").is_dir())

    def test_cli_missing_drain_or_invalid_selection_fails_before_settings(self):
        for arguments in (
            ["--limit", "1"], ["--limit", "101", "--old-writers-drained"],
            ["--retire-spec", "wrong", "--old-writers-drained"],
            ["--retire-spec", "sha256:"+"a"*64+"=2", "--after-attempt-id", "a", "--old-writers-drained"],
        ):
            with self.subTest(arguments=arguments), patch("disclosure_anchor.cli.v4_spec_maintenance.load_settings") as settings:
                with self.assertRaises(SystemExit):
                    main(arguments)
                settings.assert_not_called()
