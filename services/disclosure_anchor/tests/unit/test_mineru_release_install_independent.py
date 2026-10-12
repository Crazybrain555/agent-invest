"""Independent pre-install refusals; all external reads are controlled fixtures."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_release_install as installation
from disclosure_anchor.application.contracts.mineru_capacity_config import MineruCapacityConfig
from tests._mineru_capacity_config_fixture import capacity_payload
from tests._mineru_capacity_v11_fixture import idle_health


class MineruReleaseInstallIndependentTests(unittest.TestCase):
    def idle_read(self, health):
        raw = json.dumps(health).encode()
        with patch.object(installation, "fetch_json", return_value=(health, raw)):
            return installation.read_idle_health(
                "http://127.0.0.1:30003", expected_capacity=MineruCapacityConfig(**capacity_payload()),
                task_retention_seconds=600, cleanup_interval_seconds=30,
            )

    def test_unknown_launchctl_failure_does_not_prove_a_writer_is_unloaded(self):
        for code, error in ((1, b"Operation not permitted"), (113, b"transport failed"),
                            (64, b"usage: launchctl ...")):
            with self.subTest(code=code, error=error), patch.object(
                installation.subprocess, "run",
                return_value=subprocess.CompletedProcess([], code, b"", error),
            ), self.assertRaises(ValueError):
                installation.assert_launchd_jobs_unloaded(("com.agentinvest.disclosure-worker",))

    def test_loaded_writer_and_timed_out_status_both_refuse_installation(self):
        with patch.object(installation.subprocess, "run", return_value=(
            subprocess.CompletedProcess([], 0, b"service = loaded", b"")
        )), self.assertRaises(ValueError):
            installation.assert_launchd_jobs_unloaded(("com.agentinvest.disclosure-worker",))
        with patch.object(installation.subprocess, "run", side_effect=(
            subprocess.TimeoutExpired("launchctl", 30)
        )), self.assertRaises(ValueError):
            installation.assert_launchd_jobs_unloaded(("com.agentinvest.disclosure-worker",))

    def test_empty_requested_label_list_cannot_omit_the_two_real_producers(self):
        missing = subprocess.CompletedProcess([], 113, b"", b'Could not find service "fixture" in domain')
        with patch.object(installation.subprocess, "run", return_value=missing) as run:
            states = installation.assert_launchd_jobs_unloaded(())
        self.assertEqual(states, {"com.agentinvest.disclosure-worker": "not_loaded",
                                  "com.agentinvest.disclosure-gc": "not_loaded"})
        self.assertEqual(len(run.call_args_list), 2)
        self.assertEqual({call.args[0][-1].rsplit("/", 1)[-1] for call in run.call_args_list}, set(states))

    def test_finalizing_or_accepted_work_cannot_pass_two_legacy_idle_counters(self):
        for field in ("accepted_finalizing_tasks", "durable_nonterminal_tasks", "scheduled_tasks"):
            health = idle_health(capacity_payload())
            health["task_admission"][field] = 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.idle_read(health)
        health = idle_health(capacity_payload())
        health["capacity_observation"]["stage_counters"]["finalizer_active"] = 1
        with self.assertRaises(ValueError):
            self.idle_read(health)

    def test_actual_empty_capacity_fixture_is_accepted_without_changing_it(self):
        health = idle_health(capacity_payload())
        actual, raw, proof = self.idle_read(health)
        self.assertEqual(raw, json.dumps(health).encode())
        self.assertEqual(proof, "explicit_capacity_closed")
        self.assertEqual(actual, health)


class MineruDiagnosticCollectorProbeIndependentTests(unittest.TestCase):
    def test_bounded_owner_proof_survives_log_rotation_and_refuses_stale_or_dead_process(self):
        # Execute the actual embedded stdlib probe with only its three IO roots
        # redirected to temporary fixtures. No Docker, CUDA, torch or vLLM import.
        collector = Path(__file__).resolve().parents[2] / "scripts/windows/collect_mineru_runtime.ps1"
        script = collector.read_text()
        probe = script.split("$diagnosticProbe = @'\n", 1)[1].split("\n'@", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "installed"
            package = installed / "mineru_inference_memory"
            package.mkdir(parents=True)
            for name in ("__init__.py", "core.py", "worker.py"):
                (package / name).write_bytes(("fixture " + name).encode())
            for name in ("worker_base.py", "gpu_worker.py", "gpu_model_runner.py", "gpu/mm/encoder_runner.py"):
                source = installed / "vllm/v1/worker" / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(("fixture " + name).encode())
            proc = root / "proc"
            for pid, ticks in ((1, 50), (42, 100)):
                (proc / str(pid)).mkdir(parents=True)
                (proc / str(pid) / "stat").write_text(f"{pid} (worker process) S " + "0 " * 18 + str(ticks))
            identity = root / "owner.json"
            owner = {"contract_version": "mineru.owner-memory-identity.v1",
                     "worker_class": "mineru_inference_memory.worker.OwnerMemoryWorker",
                     "module_sha256": "sha256:" + hashlib.sha256((package / "worker.py").read_bytes()).hexdigest(),
                     "pid": 42, "proc_start_ticks": 100, "device": "cuda:0", "instance_id": "fixture"}
            identity.write_text(json.dumps(owner))
            identity.chmod(0o600)
            controlled = probe.replace('root = Path(importlib.metadata.distribution("vllm").locate_file(""))',
                                       f"root = Path({str(installed)!r})")
            controlled = controlled.replace('"/tmp/mineru-owner-memory/owner.json"', repr(str(identity)))
            controlled = controlled.replace('Path("/proc")', f"Path({str(proc)!r})")
            def run():
                return subprocess.run([sys.executable, "-I", "-c", controlled], text=True,
                                      capture_output=True, timeout=10)
            result = run()
            self.assertEqual(result.returncode, 0, result.stderr)
            proof = json.loads(result.stdout)
            self.assertEqual(proof["loaded_workers"][0]["observed_proc_start_ticks"], 100)
            self.assertEqual(proof["loaded_workers"][0]["namespace_init_start_ticks"], 50)
            self.assertEqual(len(proof["actual_diagnostic_source_sha256"]), 3)
            # No log file exists at all; proof is independent of startup/tail age.
            for state, ticks in (("S", 101), ("Z", 100), ("X", 100)):
                with self.subTest(state=state, ticks=ticks):
                    (proc / "42/stat").write_text("42 (worker process) " + state + " " + "0 " * 18 + str(ticks))
                    self.assertNotEqual(run().returncode, 0)
            (proc / "42/stat").write_text("42 (worker process) S " + "0 " * 18 + "100")
            identity.write_text(json.dumps(owner)[:-1] + ',"pid":42}')
            self.assertNotEqual(run().returncode, 0)
            identity.write_text(" " * 16385)
            self.assertNotEqual(run().returncode, 0)
