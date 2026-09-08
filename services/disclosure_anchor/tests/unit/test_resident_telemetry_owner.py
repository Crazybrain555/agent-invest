"""Composition uses real pure replayers with synthetic external boundaries.

No live kernel/SSH/GPU or shared runtime. A fake observer result is not proof
of its own anchored seal; that real boundary has separate native opt-in tests.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import resident_telemetry_owner as owner
from disclosure_anchor.adapters.runtime.bounded_http import BoundedHTTPTransportError
from disclosure_anchor.adapters.runtime.resident_owner_control import OwnerCommandResult
from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig
from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import ObserverState, SynchronizedObserverResult
from disclosure_anchor.application.contracts.resident_session_evidence import artifact_sha256, canonical_bytes, check_mac_observer_identity
from disclosure_anchor.application.contracts.synchronized_telemetry import ProcessProfileParameters, SynchronizedTelemetryFrameV2, SynchronizedTelemetryReceiptV3, SynchronizedTelemetrySealV3
from tests.unit.test_dedicated_mac_observer import _identity_bytes
from tests.unit.test_mineru_process_profile import _profile
from tests.unit.test_resident_external_observation import _external_fixture, NODE
from tests.unit.test_resident_session_evidence import _mapping_fixture, _rebind_ready
from tests.unit.test_synchronized_telemetry_contract import START


def _fixture(root):
    profile = replace(_profile(), host_runtime_identity_sha256=NODE)
    fixtures, external, frames = [], {}, []
    mac = check_mac_observer_identity(_identity_bytes())
    for host in (False, True):
        f, original_frames, receipt = _mapping_fixture(host)
        config = f["config"]
        if not host:
            session = "234567891234423482341234567890ab"
            config.update(session=session, run_directory="C:\\fixture\\run-" + session)
            f["ready"]["session"] = session
            f["ready"]["process"].update(session=session, pid=400)
            f["job"].update(session=session)
            f["job"]["supervisor_process"].update(session=session, pid=399)
            f["job"]["job"].update(child_pid=400, supervisor_pid=399, job_instance="34567890-1234-4234-8234-1234567890ab")
            f["closed"]["session"] = session
        config.update(lease_ms=30000, lifetime_ms=120000)
        if host:
            config["backend"]["linux_config"].update(lease_ms=30000, lifetime_ms=120000)
            count = f["closed"]["sampling"]["sample_count"]
            f["linux"]["close"]["sequence"] = count + 1
            f["linux"]["closed"]["sequence"] = count + 1
        host_assignment = artifact_sha256(canonical_bytes({"windows_node_identity_sha256": NODE, "gpu_uuid": "GPU-12345678-1234-4234-8234-1234567890ab"}))
        config["owner_identity"].update(runtime_bundle_identity_sha256=profile.runtime_bundle_identity_sha256, process_profile_sha256=profile.sha256, host_assignment_identity_sha256=host_assignment)
        f["ready"]["identity"].update(config["owner_identity"])
        _rebind_ready(f)
        ready, first, last = _external_fixture(host, f)
        first["observed_at_utc"] = (START - timedelta(seconds=1)).isoformat()
        last["observed_at_utc"] = (START + timedelta(seconds=3)).isoformat()
        external[ready.lane] = (first, last)
        fixtures.append(f)
        for original in original_frames:
            value = original.model_dump(mode="json")
            value.update(runtime_bundle_identity_sha256=profile.runtime_bundle_identity_sha256, process_profile_sha256=profile.sha256)
            value["resident_exporter_provenance"].update({name: getattr(ready.identity, name) for name in ("exporter_source_sha256", "host_assignment_identity_sha256", "boot_identity_sha256", "exporter_process_epoch_sha256")})
            value["clock"]["clock_domain_identity_sha256"] = mac.clock_domain_identity_sha256
            frames.append(SynchronizedTelemetryFrameV2.model_validate(value))
    payload = receipt.model_dump(mode="json")
    payload.update(runtime_bundle_identity_sha256=profile.runtime_bundle_identity_sha256, observer_identity=mac.model_dump(mode="json"), clock_domain_identity_sha256=mac.clock_domain_identity_sha256)
    payload["process_profile"].update(runtime_bundle_identity_sha256=profile.runtime_bundle_identity_sha256, process_profile_sha256=profile.sha256, parameters={key: getattr(profile, key) for key in ProcessProfileParameters.model_fields})
    receipt = SynchronizedTelemetryReceiptV3.model_validate(payload)
    seal = SynchronizedTelemetrySealV3(run_id=receipt.run_id, receipt_sha256=artifact_sha256(canonical_bytes(receipt.model_dump(mode="json"))), frames_jsonl_sha256=receipt.artifacts.frames_jsonl_sha256, preseal_observer_process_cpu_started_ns=0, preseal_observer_process_cpu_finished_ns=100, preseal_observer_cpu_ns=100, sampling_elapsed_ns_denominator=2_000_000_000, receipt_status="complete", status="complete")
    result = SynchronizedObserverResult(ObserverState.SEALED, root / "observer" / receipt.run_id, receipt, seal, tuple(frames))
    plans = [owner.ResidentLaneLaunch(canonical_bytes(f["config"]), canonical_bytes(f["manifest"]), "C:\\fixture\\" + f["config"]["lane"] + ".json") for f in fixtures]
    request = owner.ResidentTelemetryOwnerRequest(root / "owner", root / "observer", receipt.run_id, plans[0], plans[1], {**fixtures[0]["hashes"], "observe_mineru_resident_session.ps1": artifact_sha256(b"control")}, profile.exact_bytes, NODE, ResidentSSHConfig("192.0.2.1", 22, "fixture", "/fixture/key", "/fixture/known-hosts"), Path("/fixture/ssh"), artifact_sha256(b"ssh"), 2)
    return request, external, result


class ResidentTelemetryOwnerTests(unittest.TestCase):
    def _run(self, request, external, result, *, close_error=None, early_exit=False, observer_error=False):
        events, commands = [], []
        self.last_events, self.last_commands = events, commands
        finished = False

        class Command:
            def __init__(self, phase, lane):
                self.phase, self.lane, self.aborted = phase, lane, False

            def poll(self, *, timeout=0):
                if self.phase == "start" and not finished and not early_exit:
                    return None
                value = external[self.lane][0 if self.phase == "ready" else 1]
                stdout = value["job_raw"].encode() + b"\r\n" if self.phase == "start" else canonical_bytes(value)
                return OwnerCommandResult(0, stdout, b"synthetic warning\r\n")

            def abort(self):
                self.aborted = True

            @property
            def captured_output(self):
                return b"partial", b"warning"

        def launch(request, plan, *, phase, journal, container_id=None):
            lane = json.loads(plan.config_bytes)["lane"]
            events.append((phase, lane))
            command = Command(phase, lane)
            commands.append(command)
            return command

        class Observer:
            identity_bytes = _identity_bytes()

            def __init__(self, request):
                events.append(("observer-created", request.run_id))
                self.request = request

            def start(self):
                events.append(("GO", "once"))

            def poll(self, *, timeout):
                nonlocal finished
                if observer_error:
                    raise RuntimeError("synthetic observer failure")
                finished = True
                events.append(("observer-reaped", "once"))
                return result

            def close(self):
                events.append(("observer-close", "once"))

        def close(ssh, ready):
            events.append(("close", ready.lane))
            if close_error is not None:
                raise close_error
            return external[ready.lane][1]["closed_raw"].encode()

        with patch.object(owner, "_launch_command", side_effect=launch), patch.object(owner, "DedicatedMacObserver", Observer), patch.object(owner, "MacObserverIdentityReader", return_value=SimpleNamespace(observe=_identity_bytes)), patch.object(owner, "_close_lane", side_effect=close):
            return owner.run_resident_telemetry_session(request), events, commands

    def test_real_replayers_compose_after_observer_reap_and_exact_both_closures(self):
        with tempfile.TemporaryDirectory() as directory:
            request, external, result = _fixture(Path(directory).resolve())
            actual, events, commands = self._run(request, external, result)
            self.assertEqual(events[:4], [("start", "gpu_fast"), ("start", "host_slow"), ("ready", "gpu_fast"), ("ready", "host_slow")])
            self.assertLess(events.index(("observer-reaped", "once")), events.index(("close", "gpu_fast")))
            self.assertEqual(len(commands), 6)
            self.assertEqual(actual.cpu.total_cpu_ns, 147)
            saved = json.loads((request.evidence_directory / "owner-result.json").read_bytes())
            self.assertFalse(saved["activation_authorized"])
            self.assertEqual(saved["observer_status"], "complete")
            for name, sha in saved["evidence_sha256"].items():
                path = request.evidence_directory / name
                self.assertEqual(artifact_sha256(path.read_bytes()), sha)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                self._run(request, external, result)

    def test_lost_close_transport_reply_reconciles_original_artifact_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            request, external, result = _fixture(Path(directory).resolve())
            _, events, _ = self._run(request, external, result, close_error=BoundedHTTPTransportError("synthetic response lost"))
            self.assertEqual(sum(event[0] == "close" for event in events), 2)
            self.assertTrue((request.evidence_directory / "host_slow-close-response-error.json").is_file())

    def test_nontransport_close_and_early_owner_exit_remain_visible_failures(self):
        for options in ({"close_error": ValueError("wrong reply")}, {"early_exit": True}, {"observer_error": True}):
            with tempfile.TemporaryDirectory() as directory:
                request, external, result = _fixture(Path(directory).resolve())
                with self.subTest(options=options), self.assertRaises(BaseExceptionGroup):
                    self._run(request, external, result, **options)
                self.assertTrue((request.evidence_directory / "owner-failure.json").is_file())
                self.assertFalse((request.evidence_directory / "owner-result.json").exists())
                self.assertEqual(sum(event[0] == "start" for event in self.last_events), 2)
                self.assertLessEqual(sum(event[0] == "close" for event in self.last_events), 2)
                self.assertTrue(all(command.aborted for command in self.last_commands))

    def test_crosspaired_job_and_malformed_closure_cannot_produce_owner_result(self):
        for field in ("job_raw", "closed_raw"):
            with tempfile.TemporaryDirectory() as directory:
                request, external, result = _fixture(Path(directory).resolve())
                external["host_slow"][1][field] = "notJSON"
                with self.subTest(field=field), self.assertRaises(BaseExceptionGroup):
                    self._run(request, external, result)
                self.assertFalse((request.evidence_directory / "owner-result.json").exists())

    def test_launch_inputs_fail_before_directory_or_process_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            request, external, result = _fixture(Path(directory).resolve())
            invalid = [replace(request, source_hashes={**request.source_hashes, "start_mineru_resident_telemetry.ps1": artifact_sha256(b"wrong")})]
            for field, value in (("lane", "gpu_fast"), ("port", 30316), ("lifetime_ms", 7180001), ("lifetime_ms", 7190000)):
                config = json.loads(request.host.config_bytes)
                config[field] = value
                if field == "lifetime_ms":
                    config["backend"]["linux_config"][field] = value
                invalid.append(replace(request, host=replace(request.host, config_bytes=canonical_bytes(config))))
            for changed in invalid:
                with self.assertRaises(ValueError):
                    self._run(changed, external, result)
                self.assertFalse(request.evidence_directory.exists())
            config = json.loads(request.host.config_bytes)
            config["lifetime_ms"] = 7180000
            config["backend"]["linux_config"]["lifetime_ms"] = 7180000
            owner._validate_request(replace(request, host=replace(request.host, config_bytes=canonical_bytes(config))))

    def test_journal_pins_private_new_only_directory_and_detects_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            journal = owner._Journal(root / "evidence")
            try:
                journal.put("raw.stdout", b"original")
                with self.assertRaises(FileExistsError):
                    journal.put("raw.stdout", b"overwrite")
                (root / "evidence").rename(root / "preserved")
                (root / "evidence").mkdir(mode=0o700)
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    journal.put("after.stdout", b"not accepted")
            finally:
                journal.close()

    def test_real_control_composition_records_intent_before_process_constructor(self):
        with tempfile.TemporaryDirectory() as directory:
            request, external, _ = _fixture(Path(directory).resolve())
            executable = Path(sys.executable)
            request = replace(request, ssh_executable=executable, ssh_executable_sha256=artifact_sha256(executable.read_bytes()))
            journal = owner._Journal(request.evidence_directory)
            try:
                def create(command, *, timeout_seconds):
                    intent = json.loads((request.evidence_directory / "gpu_fast-start-intent.json").read_bytes())
                    self.assertEqual(intent["command"], command)
                    self.assertEqual(timeout_seconds, 140)
                    self.assertIn("IdentityFile=none", command)
                    return SimpleNamespace()
                with patch.object(owner, "BoundedOwnerCommand", side_effect=create) as constructor:
                    owner._launch_command(request, request.gpu, phase="start", journal=journal)
                    with self.assertRaises(FileExistsError):
                        owner._launch_command(request, request.gpu, phase="start", journal=journal)
                    self.assertEqual(constructor.call_count, 1)
            finally:
                journal.close()
