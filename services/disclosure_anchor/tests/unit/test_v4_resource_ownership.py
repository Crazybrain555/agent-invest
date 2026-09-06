"""No detached quarantine, no false cleanup/ACK, exact recovery still separate."""

import os
from pathlib import Path
import tempfile
import unittest

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import MinerUHttpStagedV4
from disclosure_anchor.application.ports.staged_provider_parser import V4ResourceOwnershipError
from disclosure_anchor.application.ports.remote_parse_v4_repository import V4HistoricalLocalResources
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    LocalCleanupResourceResultV4, build_local_cleanup_receipt_v4,
)
from disclosure_anchor.domain.errors import ParserOutputContractError
from tests.unit.test_mineru_http_staged_v4 import (
    _Guard, _Transport, _materialize_fixture, _official_zip, _published_test_root,
    _quarantine_path, _no_receipt_local_failure_cleanup_arguments,
    _ack_no_receipt_local_failure,
)


def _backend(root, transport, fault=lambda _phase: None):
    return MinerUHttpStagedV4(
        scratch_root=root, published_root=_published_test_root(root),
        transport=transport, clock=lambda: 1.0, fault_hook=fault,
    )


def _payloads(tree, fixture):
    excluded = {
        fixture.intent.provider_envelope_relpath, fixture.intent.output_manifest_relpath,
        Path(fixture.intent.staging_marker_relpath).name,
    }
    return sorted(path for path in tree.rglob("*") if path.is_file() and path.relative_to(tree).as_posix() not in excluded)


class V4ResourceOwnershipTests(unittest.TestCase):
    def _crash(self, root, transport, fixture, phase):
        def crash(observed):
            if observed == phase:
                raise RuntimeError("simulated process exit")
        with self.assertRaisesRegex(RuntimeError, "simulated process exit"):
            _backend(root, transport, crash).materialize_v4(**fixture.arguments(), claim_guard=_Guard())

    def test_ambiguous_staging_family_retains_one_namespace_across_restarts(self):
        self.assertFalse(issubclass(V4ResourceOwnershipError, ParserOutputContractError))
        modes = ("partial", "torn", "mutated", "non-suffix", "large", "over-limit", "hardlink", "symlink", "marker-drift", "markerless", "root-link")
        for mode in modes:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)/"scratch"
                root.mkdir(mode=0o700)
                archive = _official_zip()
                fixture = _materialize_fixture(archive, output_bytes=256*1024, uncompressed_byte_limit=256*1024, temp_disk_bytes=512*1024)
                transport = _Transport(archive)
                self._crash(root, transport, fixture, "after_staging_fsync")
                staging = root/fixture.intent.staging_relpath
                marker = staging/Path(fixture.intent.staging_marker_relpath).name
                payloads = _payloads(staging, fixture)
                witness = payloads[0]
                if mode == "partial":
                    (staging/fixture.intent.output_manifest_relpath).unlink()
                elif mode == "torn":
                    witness = staging/fixture.intent.output_manifest_relpath
                    witness.write_bytes(b"{")
                elif mode == "mutated":
                    witness.write_bytes(b"changed")
                elif mode == "non-suffix":
                    payloads[1].unlink()
                elif mode in {"large", "over-limit"}:
                    witness = staging/"residual.bin"
                    witness.write_bytes(b"x"*(160*1024 if mode == "large" else 1024*1024))
                    witness.chmod(0o600)
                elif mode == "hardlink":
                    os.link(witness, staging/"linked.bin")
                elif mode == "symlink":
                    (staging/"linked.bin").symlink_to(witness)
                elif mode == "marker-drift":
                    marker.write_bytes(b"foreign")
                elif mode == "markerless":
                    marker.unlink()
                elif mode == "root-link":
                    moved = Path(directory)/"retained"
                    relative = witness.relative_to(staging)
                    staging.rename(moved)
                    staging.symlink_to(moved, target_is_directory=True)
                    witness = moved/relative
                retained = witness.read_bytes()
                inode = witness.stat().st_ino
                for _ in range(3):
                    with self.assertRaises(V4ResourceOwnershipError):
                        _backend(root, transport).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                    self.assertEqual(witness.read_bytes(), retained)
                    self.assertEqual(witness.stat().st_ino, inode)
                    self.assertFalse(_quarantine_path(root, fixture).exists())
                    self.assertFalse((root/fixture.intent.output_relpath).exists())
                self.assertEqual(transport.downloads, 1)
                self.assertEqual(transport.acks, 0)
                if mode not in {"root-link", "marker-drift", "markerless", "hardlink", "symlink"}:
                    cleanup, _ = _no_receipt_local_failure_cleanup_arguments(fixture=fixture)
                    for _ in range(2):
                        with self.assertRaises(V4ResourceOwnershipError):
                            _backend(root, transport).cleanup_v4(**cleanup)
                    self.assertEqual(witness.read_bytes(), retained)
                    self.assertEqual(transport.acks, 0)

    def test_invalid_promoted_output_containment_crash_windows_marker_or_markerless(self):
        for markerless in (False, True):
            for phase in ("before_invalid_output_containment_rename", "after_invalid_output_containment_rename"):
                with self.subTest(markerless=markerless, phase=phase), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)/"scratch"
                    root.mkdir(mode=0o700)
                    archive = _official_zip()
                    fixture = _materialize_fixture(archive)
                    transport = _Transport(archive)
                    self._crash(root, transport, fixture, "after_promotion_rename")
                    output = root/fixture.intent.output_relpath
                    staging = root/fixture.intent.staging_relpath
                    victim = _payloads(output, fixture)[0]
                    relpath = victim.relative_to(output)
                    victim.write_bytes(b"retained-invalid-output")
                    if markerless:
                        (output/Path(fixture.intent.staging_marker_relpath).name).unlink()
                    def crash(observed):
                        if observed == phase:
                            raise RuntimeError("containment response lost")
                    with self.assertRaisesRegex(RuntimeError, "containment response lost"):
                        _backend(root, transport, crash).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                    for _ in range(2):
                        with self.assertRaises(V4ResourceOwnershipError):
                            _backend(root, transport).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                    self.assertFalse(output.exists())
                    self.assertEqual((staging/relpath).read_bytes(), b"retained-invalid-output")
                    self.assertFalse(_quarantine_path(root, fixture).exists())
                    self.assertEqual((transport.downloads, transport.acks), (1, 0))

    def test_containment_preserves_injection_and_refuses_marker_destination_or_claim_races(self):
        for mode in ("injection", "marker-swap", "destination-race", "claim-loss", "root-replace"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)/"scratch"
                root.mkdir(mode=0o700)
                archive = _official_zip()
                fixture = _materialize_fixture(archive)
                transport = _Transport(archive)
                self._crash(root, transport, fixture, "after_promotion_rename")
                output = root/fixture.intent.output_relpath
                staging = root/fixture.intent.staging_relpath
                victim = _payloads(output, fixture)[0]
                relative = victim.relative_to(output)
                victim.write_bytes(b"invalid")
                revoked = False
                class Guard:
                    def assert_current_under_resource_lock(self, **_args):
                        if revoked:
                            raise RuntimeError("claim lost")
                def interfere(phase):
                    nonlocal revoked
                    if phase != "before_invalid_output_containment_rename":
                        return
                    if mode == "injection":
                        (output/"foreign.bin").write_bytes(b"foreign")
                    elif mode == "marker-swap":
                        (output/Path(fixture.intent.staging_marker_relpath).name).write_bytes(b"foreign-marker")
                    elif mode == "destination-race":
                        staging.mkdir(mode=0o700)
                        (staging/"foreign.bin").write_bytes(b"foreign")
                    elif mode == "claim-loss":
                        revoked = True
                    elif mode == "root-replace":
                        output.rename(output.with_name("retained-original"))
                        output.mkdir(mode=0o700)
                expected = RuntimeError if mode == "claim-loss" else V4ResourceOwnershipError
                with self.assertRaises(expected):
                    _backend(root, transport, interfere).materialize_v4(**fixture.arguments(), claim_guard=Guard())
                if mode == "injection":
                    self.assertEqual((staging/"foreign.bin").read_bytes(), b"foreign")
                    self.assertEqual((staging/relative).read_bytes(), b"invalid")
                    self.assertFalse(output.exists())
                elif mode == "root-replace":
                    self.assertEqual((output.with_name("retained-original")/relative).read_bytes(), b"invalid")
                else:
                    self.assertTrue(output.is_dir())
                    self.assertEqual((output/relative).read_bytes(), b"invalid")
                self.assertFalse(_quarantine_path(root, fixture).exists())

    def test_cleanup_and_historical_gate_refuse_legacy_residual_or_omitted_output(self):
        for kind in ("legacy", "omitted-output", "claimed-absent"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)/"scratch"
                root.mkdir(mode=0o700)
                archive = _official_zip()
                fixture = _materialize_fixture(archive)
                transport = _Transport(archive)
                backend = _backend(root, transport)
                cleanup, failure = _no_receipt_local_failure_cleanup_arguments(fixture=fixture)
                receipt = build_local_cleanup_receipt_v4(
                    plan=cleanup["plan"], cleanup_pending_checkpoint=cleanup["checkpoint"],
                    results=tuple(LocalCleanupResourceResultV4(kind=r.kind, relpath=r.relpath, disposition="absent") for r in cleanup["plan"].resources),
                )
                if kind == "legacy":
                    residual = _quarantine_path(root, fixture)
                elif kind == "omitted-output":
                    residual = root/fixture.intent.output_relpath
                else:
                    residual = root/fixture.intent.staging_relpath
                backend._ensure_parent(residual)
                residual.mkdir(mode=0o700)
                witness = residual/"foreign.bin"
                witness.write_bytes(b"forensic")
                for _ in range(2):
                    with self.assertRaises(V4ResourceOwnershipError):
                        backend.verify_historical_local_resources(V4HistoricalLocalResources(fixture.intent, receipt))
                    if kind != "claimed-absent":
                        with self.assertRaises(V4ResourceOwnershipError):
                            backend.cleanup_v4(**cleanup)
                    with self.assertRaises(V4ResourceOwnershipError):
                        _ack_no_receipt_local_failure(
                            backend=backend, fixture=fixture, cleanup=cleanup,
                            failure=failure, cleanup_receipt=receipt,
                        )
                    self.assertEqual(witness.read_bytes(), b"forensic")
                self.assertEqual((transport.downloads, transport.acks), (0, 0))
