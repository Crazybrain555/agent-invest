"""Separate API and observer epochs; old evidence is never upgraded by parsing."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import (
    run_synchronized_telemetry_observer, verify_synchronized_telemetry_observer,
    validate_synchronized_telemetry_v2,
    SynchronizedObserverResult, ObserverState,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    FrozenApiProcessProfile, TelemetryObserverIdentity,
    SynchronizedTelemetryReceiptV2, SynchronizedTelemetryReceiptV3,
    operational_telemetry_schema_documents,
    SynchronizedTelemetrySealV3,
)
from disclosure_anchor.adapters.runtime.full_host_hour_kpi import verified_coverage_from_observer_artifacts
from tests.unit.test_resident_session_evidence import _mapping_fixture
from tests.unit.test_synchronized_telemetry_observer import (
    HASH_B, HASH_D, _collector_spec, _profile,
)


OBSERVER_EPOCH = "sha256:" + "f" * 64


def _api_profile() -> FrozenApiProcessProfile:
    return FrozenApiProcessProfile.model_validate(_profile().model_dump(exclude={"started_at_utc", "started_monotonic_ns", "clock_domain_identity_sha256"}))


class SynchronizedTelemetryObserverV3Tests(unittest.TestCase):
    def test_coverage_routes_exact_v3_and_never_uses_api_as_observer(self) -> None:
        # Projection unit fixture: actual anchor/replay is covered below. The
        # fake verifier must be called with the explicit requested version.
        _gpu, gpu_frames, _receipt = _mapping_fixture()
        _host, host_frames, receipt = _mapping_fixture(True)
        seal = SynchronizedTelemetrySealV3(
            run_id=receipt.run_id, receipt_sha256=HASH_B, frames_jsonl_sha256=HASH_B,
            preseal_observer_process_cpu_started_ns=0, preseal_observer_process_cpu_finished_ns=0,
            preseal_observer_cpu_ns=0, sampling_elapsed_ns_denominator=2_000_000_000,
            receipt_status="complete", status="complete",
        )
        result = SynchronizedObserverResult(ObserverState.SEALED, Path("/synthetic/only"), receipt, seal, (*gpu_frames, *host_frames))
        with patch("disclosure_anchor.adapters.runtime.full_host_hour_kpi.verify_synchronized_telemetry_observer", return_value=result) as verify:
            coverage = verified_coverage_from_observer_artifacts(artifact_root=Path("/synthetic/only"), run_id=receipt.run_id, receipt_version=3)
        verify.assert_called_once_with(artifact_root=Path("/synthetic/only"), run_id=receipt.run_id, receipt_version=3)
        self.assertEqual(coverage.observer_process_epoch_sha256, receipt.observer_identity.process_epoch_sha256)
        self.assertNotEqual(coverage.observer_process_epoch_sha256, receipt.process_profile.process_epoch_sha256)
        self.assertFalse(coverage.exporter_overhead_safe)
        self.assertIsNone(coverage.exporter_overhead_attestation_sha256)

    def test_v3_seals_actual_separate_epochs_with_unchanged_frame_v2(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            result = run_synchronized_telemetry_observer(
                artifact_root=root, process_profile=_api_profile(),
                observer_identity=TelemetryObserverIdentity(process_epoch_sha256=OBSERVER_EPOCH, clock_domain_identity_sha256=HASH_D),
                gpu_collector=_collector_spec(lane="gpu"), host_collector=_collector_spec(lane="host"),
                duration_seconds=0.3, process_cpu_ns=lambda: 0,
            )
            self.assertIsInstance(result.receipt, SynchronizedTelemetryReceiptV3)
            self.assertEqual(result.receipt.process_profile.process_epoch_sha256, HASH_B)
            self.assertEqual(result.receipt.observer_identity.process_epoch_sha256, OBSERVER_EPOCH)
            self.assertEqual({frame.clock.clock_domain_identity_sha256 for frame in result.frames}, {HASH_D})
            self.assertEqual({path.name for path in result.run_directory.iterdir()}, {"frames.v2.jsonl", "receipt.v3.json", "seal.v3.json"})
            replay = verify_synchronized_telemetry_observer(artifact_root=root, run_id=result.receipt.run_id, receipt_version=3)
            self.assertEqual(replay, result)
            with self.assertRaises(ValueError):
                verify_synchronized_telemetry_observer(artifact_root=root, run_id=result.receipt.run_id)
            with self.assertRaises(ValueError):
                SynchronizedTelemetryReceiptV2.model_validate(result.receipt.model_dump())
            changed = result.receipt.model_copy(update={"process_profile": _api_profile().model_copy(update={"process_epoch_sha256": OBSERVER_EPOCH})})
            with self.assertRaisesRegex(ValueError, "API process epoch"):
                validate_synchronized_telemetry_v2(result.frames, receipt=changed)
            payload = result.receipt.model_dump(mode="json")
            payload["observer_identity"]["clock_domain_identity_sha256"] = HASH_B
            with self.assertRaisesRegex(ValueError, "observer clock domain"):
                SynchronizedTelemetryReceiptV3.model_validate(payload)

    def test_mixed_legacy_profile_and_new_observer_never_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = dict(artifact_root=Path(temporary) / "not-created", gpu_collector=_collector_spec(lane="gpu"), host_collector=_collector_spec(lane="host"), duration_seconds=0.1)
            with self.assertRaisesRegex(ValueError, "separate observer"):
                run_synchronized_telemetry_observer(process_profile=_api_profile(), **args)
            with self.assertRaisesRegex(ValueError, "clock-free API profile"):
                run_synchronized_telemetry_observer(process_profile=_profile(), observer_identity=TelemetryObserverIdentity(process_epoch_sha256=OBSERVER_EPOCH, clock_domain_identity_sha256=HASH_D), **args)
            self.assertFalse((Path(temporary) / "not-created").exists())

    def test_preexisting_schema_objects_remain_exact(self) -> None:
        root = Path(__file__).parents[2] / "contracts/operational"
        for name, schema in operational_telemetry_schema_documents().items():
            if ".v3." not in name:
                with self.subTest(name=name):
                    self.assertEqual(schema, json.loads((root / name).read_bytes()))
        with self.assertRaises(ValueError):
            FrozenApiProcessProfile.model_validate(_profile().model_dump())
