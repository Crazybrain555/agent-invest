"""Stopped idle health remains observable but cannot authorize start/acceptance."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_deployment_gate as gate
from disclosure_anchor.adapters.runtime.mineru_identity import MinerUClientIdentity
from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorError,
    parse_mineru_orchestrator_health_payload,
)
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
    overlap_health,
    runtime_wrapper,
)


STOPS = ("foreign_pending", "foreign_applied", "shutting_down", "worker_unavailable")


def stopped_health(reason):
    value = overlap_health(idle=True)
    if reason.startswith("foreign_"):
        applied = reason == "foreign_applied"
        value["capacity_observation"]["owner_control"].update(
            foreign_loop_observed=True,
            soft_drain_requested=True,
            soft_drain_applied=applied,
            trigger="foreign_event_loop",
        )
        if applied:
            value["task_admission"].update(
                admission_open=False, blocked_reason="shutting_down"
            )
    else:
        value["task_admission"].update(admission_open=False, blocked_reason=reason)
    return value


def parsed(value):
    return parse_mineru_orchestrator_health_payload(
        value, expected_task_slots=None, expected_capacity=configuration()
    )


def deployment():
    return gate.VerifiedMinerUDeployment(
        api_url=API_URL,
        observability_url=OBSERVABILITY_URL,
        inference_upstream_url=INFERENCE_URL,
        runtime_identity_sha256=runtime_wrapper()["identity_sha256"],
        served_model_id="example-model",
        canary_passed_at_utc=datetime(2026, 9, 14, tzinfo=UTC),
        canary_max_age_seconds=600,
        task_retention_seconds=600,
        task_cleanup_interval_seconds=30,
        task_slots=2,
        expected_capacity=configuration(),
    )


class DiagnosticReached(Exception):
    """A test-only sentinel: the old code attempted new work; no parser was run."""


class MineruCapacityReadinessTests(unittest.TestCase):
    def test_generic_parser_retains_legal_stops_and_normal_capacity_full_remains_usable(
        self,
    ):
        for reason in STOPS:
            with self.subTest(reason=reason):
                value = stopped_health(reason)
                health = parsed(value)
                self.assertEqual(health.active_tasks, 0)
                self.assertEqual(health.as_dict(), value)
                self.assertEqual(len(health.as_dict()), 17)
        with patch.object(
            gate,
            "fetch_mineru_orchestrator_health",
            return_value=parsed(overlap_health(idle=True)),
        ):
            deployment().probe_orchestrator(require_idle=True)
        with patch.object(
            gate,
            "fetch_mineru_orchestrator_health",
            return_value=parsed(overlap_health(full=True)),
        ):
            deployment().probe_orchestrator(require_idle=False)

    def test_deployment_probe_rejects_stopped_idle_owner_for_both_probe_modes(self):
        for reason in STOPS:
            for idle in (False, True):
                with (
                    self.subTest(reason=reason, require_idle=idle),
                    patch.object(
                        gate,
                        "fetch_mineru_orchestrator_health",
                        return_value=parsed(stopped_health(reason)),
                    ),
                    self.assertRaises(gate.MinerUDeploymentGateError),
                ):
                    deployment().probe_orchestrator(require_idle=idle)

    def test_smoke_acceptance_rejects_stopped_before_or_after_without_hiding_observation(
        self,
    ):
        for reason in STOPS:
            for position in ("before", "after"):
                before = overlap_health(idle=True)
                after = deepcopy(before)
                if position == "before":
                    before = stopped_health(reason)
                else:
                    after = stopped_health(reason)
                bundle = {
                    "task_registry_semantics": "retained-terminal-gauges.v1",
                    "before": before,
                    "after": after,
                    "terminal_active_tasks": 0,
                    "stop_semantics": "drain-not-cancel.v1",
                }
                with (
                    self.subTest(reason=reason, position=position, consumer="receipt"),
                    self.assertRaises(MinerUOrchestratorError),
                ):
                    smoke._smoke_orchestrator_evidence(parsed(before), parsed(after))
                with (
                    self.subTest(reason=reason, position=position, consumer="gate"),
                    self.assertRaises(gate.MinerUDeploymentGateError),
                ):
                    gate._verify_smoke_orchestrator(
                        bundle,
                        task_slots=2,
                        task_retention_seconds=600,
                        cleanup_interval_seconds=30,
                        expected_capacity=configuration(),
                    )

    def test_actual_smoke_entry_rejects_stopped_idle_before_diagnostic_submission(self):
        for reason in STOPS:
            with self.subTest(reason=reason), ExitStack() as stack:
                root = Path(
                    stack.enter_context(
                        tempfile.TemporaryDirectory(
                            prefix="capacity-readiness-", dir="/private/tmp"
                        )
                    )
                )
                source = root / "never-parsed.pdf"
                source.write_bytes(b"local control fixture, not PDF semantics")
                source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
                binary = root / "never-executed"
                binary.write_bytes(b"unused")
                config_path = root / "capacity.json"
                config_path.write_bytes(canonical_payload(config_payload()))
                config_path.chmod(0o600)
                manifest = root / "manifest.json"
                wrapper = runtime_wrapper()
                manifest.write_bytes(canonical_payload(wrapper))
                manifest.chmod(0o600)
                snapshot = tempfile.TemporaryDirectory(dir=root)
                stack.callback(snapshot.cleanup)
                for name, value in (
                    (
                        "_snapshot_pdf",
                        (snapshot, source, source_sha, source.stat().st_size, 2),
                    ),
                    ("process_snapshot", {}),
                    ("mineru_api_temp_dirs", set()),
                    (
                        "client_bundle_identity",
                        MinerUClientIdentity(CLIENT_SHA, "3.13.13", CLIENT_PACKAGES),
                    ),
                    ("writer_code_digest", WRITER_SHA),
                    ("run_mineru_multimodal_canary", object()),
                    (
                        "fetch_mineru_orchestrator_health",
                        parsed(stopped_health(reason)),
                    ),
                ):
                    stack.enter_context(patch.object(smoke, name, return_value=value))
                stack.enter_context(
                    patch.dict(os.environ, {"MINERU_PROCESSING_WINDOW_SIZE": "16"})
                )
                diagnostic = stack.enter_context(
                    patch.object(
                        smoke, "run_diagnostic_pdf", side_effect=DiagnosticReached
                    )
                )
                caught = None
                try:
                    smoke.main(
                        [
                            "--input",
                            str(source),
                            "--expected-input-sha256",
                            source_sha,
                            "--mineru-bin",
                            str(binary),
                            "--api-url",
                            API_URL,
                            "--observability-url",
                            OBSERVABILITY_URL,
                            "--inference-upstream-url",
                            INFERENCE_URL,
                            "--runtime-manifest",
                            str(manifest),
                            "--runtime-bundle-identity",
                            wrapper["identity_sha256"],
                            "--capacity-config",
                            str(config_path),
                            "--capacity-config-sha256",
                            digest(config_payload()),
                            "--receipt-out",
                            str(root / "receipt.json"),
                            "--canary-cache-out",
                            str(root / "canary.json"),
                            "--work-root",
                            str(root),
                        ]
                    )
                except (MinerUOrchestratorError, DiagnosticReached) as exc:
                    caught = exc
                self.assertIsInstance(caught, MinerUOrchestratorError)
                diagnostic.assert_not_called()
                self.assertFalse((root / "receipt.json").exists())
                self.assertFalse((root / "canary.json").exists())
