"""Local config/wire consumers; explicit stop boundaries prevent real work or PASS receipts."""

from contextlib import ExitStack, redirect_stderr
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_deployment_gate as gate
from disclosure_anchor.adapters.runtime.mineru_identity import MinerUClientIdentity
from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    parse_mineru_orchestrator_health_payload,
)
from scripts import freeze_mineru_campaign_epoch as epoch
from scripts import mineru_smoke as smoke
from tests._mineru_capacity_config_fixture import canonical_payload
from tests._mineru_capacity_consumers_fixture import (
    API_URL,
    CLIENT_PACKAGES,
    CLIENT_SHA,
    INFERENCE_URL,
    OBSERVABILITY_URL,
    WRITER_SHA,
    config_payload,
    configuration,
    digest,
    legacy_health,
    overlap_health,
    runtime_wrapper,
)


class BoundaryReached(Exception):
    """An expected test-only control boundary; never an actual runtime failure."""


def write_json(path, value):
    path.write_bytes(canonical_payload(value))
    path.chmod(0o600)


class CapacityCliConsumerTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(
            prefix="capacity-consumer-", dir="/private/tmp"
        )
        self.addCleanup(self.owned.cleanup)
        self.root = Path(self.owned.name)
        self.wrapper = runtime_wrapper()
        self.manifest = self.root / "runtime.json"
        write_json(self.manifest, self.wrapper)
        self.capacity = self.root / "capacity.json"
        write_json(self.capacity, config_payload())
        self.capacity_args = [
            "--capacity-config",
            str(self.capacity),
            "--capacity-config-sha256",
            digest(config_payload()),
        ]
        self.epoch_args = [
            "--runtime-manifest",
            str(self.manifest),
            "--receipt-out",
            str(self.root / "epoch.json"),
            "--ssh-host",
            "example.invalid",
            "--ssh-user",
            "unused-test-user",
            "--ssh-identity",
            str(self.root / "unused-identity"),
            "--ssh-known-hosts",
            str(self.root / "unused-known-hosts"),
        ]
        self.input_path = self.root / "unparsed-fixture.pdf"
        self.input_path.write_bytes(b"synthetic CLI boundary input; never parsed")
        self.input_sha = hashlib.sha256(self.input_path.read_bytes()).hexdigest()
        self.bin = self.root / "unused-mineru"
        self.bin.write_bytes(b"not executable; never launched")
        self.epoch_args += [
            "--mineru-bin",
            str(self.bin),
            "--runtime-bundle-identity",
            self.wrapper["identity_sha256"],
        ]
        self.smoke_args = [
            "--input",
            str(self.input_path),
            "--expected-input-sha256",
            self.input_sha,
            "--mineru-bin",
            str(self.bin),
            "--api-url",
            API_URL,
            "--observability-url",
            OBSERVABILITY_URL,
            "--inference-upstream-url",
            INFERENCE_URL,
            "--runtime-manifest",
            str(self.manifest),
            "--runtime-bundle-identity",
            self.wrapper["identity_sha256"],
            "--receipt-out",
            str(self.root / "smoke.json"),
            "--canary-cache-out",
            str(self.root / "canary.json"),
            "--work-root",
            str(self.root),
        ]
        self.client = MinerUClientIdentity(CLIENT_SHA, "3.13.13", CLIENT_PACKAGES)

    def evidence(self, **overrides):
        values = dict(
            api_url=API_URL,
            observability_url=OBSERVABILITY_URL,
            inference_upstream_url=INFERENCE_URL,
            runtime_identity_sha256=self.wrapper["identity_sha256"],
            served_model_id="example-model",
            canary_passed_at_utc=datetime(2026, 9, 14, tzinfo=UTC),
            canary_max_age_seconds=600,
            task_retention_seconds=600,
            task_cleanup_interval_seconds=30,
            task_slots=2,
            expected_capacity=configuration(),
        )
        values.update(overrides)
        return gate.VerifiedMinerUDeployment(**values)

    def test_deployment_probe_forwards_external_capacity_and_idle_counts_finalizers(
        self,
    ):
        calls = []

        def fetch(url, **kwargs):
            self.assertEqual(url, API_URL)
            calls.append(kwargs)
            return parse_mineru_orchestrator_health_payload(overlap_health(), **kwargs)

        evidence = self.evidence()
        with patch.object(gate, "fetch_mineru_orchestrator_health", side_effect=fetch):
            evidence.probe_orchestrator(require_idle=False)
            with self.assertRaises(gate.MinerUDeploymentUnavailableError):
                evidence.probe_orchestrator(require_idle=True)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call["expected_capacity"], configuration())
            self.assertEqual(call["expected_task_retention_seconds"], 600)
            self.assertEqual(call["expected_cleanup_interval_seconds"], 30)

    def test_smoke_gate_checks_all_17_fields_and_distinct_N_P_without_legacy_fallback(
        self,
    ):
        before = overlap_health(idle=True)
        after = deepcopy(before)
        after["completed_tasks"] += 1
        bundle = {
            "task_registry_semantics": "retained-terminal-gauges.v1",
            "before": before,
            "after": after,
            "terminal_active_tasks": 0,
            "stop_semantics": "drain-not-cancel.v1",
        }
        kwargs = dict(
            task_slots=2,
            task_retention_seconds=600,
            cleanup_interval_seconds=30,
            expected_capacity=configuration(),
        )
        gate._verify_smoke_orchestrator(bundle, **kwargs)
        for field in ("capacity_observation", "task_admission"):
            bad = deepcopy(bundle)
            del bad["after"][field]
            with (
                self.subTest(field=field),
                self.assertRaises(gate.MinerUDeploymentGateError),
            ):
                gate._verify_smoke_orchestrator(bad, **kwargs)
        with self.assertRaises(gate.MinerUDeploymentGateError):
            gate._verify_smoke_orchestrator(
                bundle,
                **dict(kwargs, expected_capacity=configuration(omp_num_threads=4)),
            )
        with self.assertRaises(gate.MinerUDeploymentGateError):
            gate._verify_smoke_orchestrator(
                bundle,
                task_slots=2,
                task_retention_seconds=600,
                cleanup_interval_seconds=30,
            )

    def test_epoch_pair_loads_real_bytes_and_rejects_drift_before_host_boundary(self):
        def invoke(extra):
            with (
                patch.object(epoch, "client_bundle_identity", return_value=self.client),
                patch.object(epoch, "writer_code_digest", return_value=WRITER_SHA),
            ):
                return epoch.main(self.epoch_args + extra)

        with patch.object(
            epoch, "build_host_observer_ssh_command", side_effect=BoundaryReached
        ) as build:
            with self.assertRaises(BoundaryReached):
                invoke(self.capacity_args)
        build.assert_called_once()
        self.assertFalse((self.root / "epoch.json").exists())
        wrong_hash = [
            "--capacity-config",
            str(self.capacity),
            "--capacity-config-sha256",
            "sha256:" + "0" * 64,
        ]
        for extra in ([], self.capacity_args[:2], self.capacity_args[2:], wrong_hash):
            with (
                self.subTest(extra=extra),
                redirect_stderr(io.StringIO()),
                patch.object(
                    epoch,
                    "build_host_observer_ssh_command",
                    side_effect=AssertionError("host IO"),
                ) as build,
                self.assertRaises(SystemExit),
            ):
                invoke(extra)
            build.assert_not_called()
        changed = config_payload(omp_num_threads=4)
        write_json(self.capacity, changed)
        with patch.object(
            epoch,
            "build_host_observer_ssh_command",
            side_effect=AssertionError("host IO"),
        ) as build:
            with self.assertRaises(SystemExit):
                invoke(
                    [
                        "--capacity-config",
                        str(self.capacity),
                        "--capacity-config-sha256",
                        digest(changed),
                    ]
                )
        build.assert_not_called()

    def test_smoke_pair_flows_through_real_manifest_and_both_full_health_consumers(
        self,
    ):
        health_calls = []
        config = configuration(processing_window_size=32)
        payload = config_payload(processing_window_size=32)
        write_json(self.capacity, payload)
        self.capacity_args[-1] = digest(payload)
        orchestrator = self.wrapper["manifest"]["orchestrator"]
        orchestrator.update(
            processing_window_size=32,
            capacity_config=payload,
            capacity_config_sha256=digest(payload),
        )
        old_identity = self.wrapper["identity_sha256"]
        self.wrapper["identity_sha256"] = digest(self.wrapper["manifest"])
        write_json(self.manifest, self.wrapper)
        self.smoke_args[self.smoke_args.index(old_identity)] = self.wrapper[
            "identity_sha256"
        ]
        health = overlap_health(idle=True)
        health["processing_window_size"] = 32
        health["task_protocol_runtime"]["capacity_config_sha256"] = digest(payload)
        health["capacity_observation"]["capacity_config_sha256"] = digest(payload)
        real_evidence = smoke._smoke_orchestrator_evidence

        def fetch(url, **kwargs):
            self.assertEqual(url, API_URL)
            self.assertEqual(kwargs["expected_capacity"], config)
            health_calls.append(kwargs)
            return parse_mineru_orchestrator_health_payload(deepcopy(health), **kwargs)

        def stop_before_receipt(before, after):
            value = real_evidence(before, after)
            self.assertEqual(len(value["before"]), 17)
            self.assertEqual(len(value["after"]), 17)
            raise BoundaryReached

        with ExitStack() as stack:
            snapshot = stack.enter_context(tempfile.TemporaryDirectory(dir=self.root))
            # Opaque placeholders only cross the CLI forwarding seam. No source/provider/ACK authority is produced.
            for name, value in (
                ("process_snapshot", {}),
                ("mineru_api_temp_dirs", set()),
                ("client_bundle_identity", self.client),
                ("writer_code_digest", WRITER_SHA),
                ("run_mineru_multimodal_canary", object()),
                (
                    "run_diagnostic_pdf",
                    ({"page_count": 2}, {"fixture": "not a disposal proof"}),
                ),
            ):
                stack.enter_context(patch.object(smoke, name, return_value=value))
            # Use a separately owned snapshot handle so main's cleanup cannot erase its own manifest/config fixtures.
            cleanup_handle = tempfile.TemporaryDirectory(dir=snapshot)
            stack.callback(cleanup_handle.cleanup)
            stack.enter_context(
                patch.object(
                    smoke,
                    "_snapshot_pdf",
                    return_value=(
                        cleanup_handle,
                        self.input_path,
                        self.input_sha,
                        self.input_path.stat().st_size,
                        2,
                    ),
                )
            )
            stack.enter_context(
                patch.dict(os.environ, {"MINERU_PROCESSING_WINDOW_SIZE": "32"})
            )
            stack.enter_context(
                patch.object(
                    smoke, "fetch_mineru_orchestrator_health", side_effect=fetch
                )
            )
            stack.enter_context(
                patch.object(
                    smoke,
                    "_smoke_orchestrator_evidence",
                    side_effect=stop_before_receipt,
                )
            )
            with self.assertRaises(BoundaryReached):
                smoke.main(self.smoke_args + self.capacity_args)
        self.assertEqual(len(health_calls), 2)
        self.assertFalse((self.root / "smoke.json").exists())
        self.assertFalse((self.root / "canary.json").exists())

    def test_smoke_missing_pair_fails_before_snapshot_and_legacy_helper_keeps_family_boundary(
        self,
    ):
        for extra in (self.capacity_args[:2], self.capacity_args[2:]):
            with (
                self.subTest(extra=extra),
                redirect_stderr(io.StringIO()),
                patch.object(
                    smoke, "_snapshot_pdf", side_effect=AssertionError("PDF IO")
                ) as snapshot,
                self.assertRaises(SystemExit),
            ):
                smoke.main(self.smoke_args + extra)
            snapshot.assert_not_called()
        with self.assertRaises(ValueError):
            smoke._runtime_manifest(
                self.manifest,
                configured_identity=self.wrapper["identity_sha256"],
                local_client_identity=self.client,
                local_processing_window_size=16,
                local_writer_code_digest=WRITER_SHA,
            )
        old = legacy_health()
        old["processing_tasks"] = 0
        normalized = {
            key: value
            for key, value in old.items()
            if key not in {"task_protocol_schema", "task_protocol_runtime"}
        }
        bundle = {
            "task_registry_semantics": "retained-terminal-gauges.v1",
            "before": normalized,
            "after": deepcopy(normalized),
            "terminal_active_tasks": 0,
            "stop_semantics": "drain-not-cancel.v1",
        }
        gate._verify_smoke_orchestrator(
            bundle,
            task_slots=1,
            task_retention_seconds=600,
            cleanup_interval_seconds=30,
        )
