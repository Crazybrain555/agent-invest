"""Independent pre-install refusals; all external reads are controlled fixtures."""

import json
import subprocess
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
