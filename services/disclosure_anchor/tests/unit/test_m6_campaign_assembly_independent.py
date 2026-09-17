"""Independent bootstrap/ownership fault oracles, with no SSH, DB or GPU access."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from disclosure_anchor.adapters.runtime.m6_campaign_assembly import (
    CampaignOutcomeUnknown, M6CampaignAssembly, launcher_lines, parse_ready_line, sourced_child_argv,
)
from disclosure_anchor.adapters.runtime.resident_owner_control import BoundedOwnerCommand, OwnerCommandResult
from disclosure_anchor.application.services.m6_launch_budget import finish_wait_seconds
from tests import m6_owner_support as owner
from tests import m6_support as m6
from tests.m6_delivery_support import campaign_intent_for_spec, evaluation_plan


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class CampaignAssemblyIndependentTests(unittest.TestCase):
    def setUp(self):
        self.spec = m6.make_fixture("e2e_publication", {"a": (7, "replay")}).spec
        self.anchor = owner.anchor_for(self.spec)

    def ready(self):
        return {"status": "ready_unbound", "anchor": json.loads(self.anchor.canonical_bytes()),
                "anchor_sha256": self.anchor.canonical_sha256(),
                "owner_epoch_sha256": self.anchor.owner_process_epoch_sha256, "spec_sha256": None,
                "journal_prefix_bytes": 0, "journal_prefix_sha256": "sha256:" + hashlib.sha256(b"").hexdigest()}

    def assembly(self, root, launch=None, *, mode="run", create_output=True):
        # Contract loading has separate coverage; these declared port doubles isolate
        # process ownership/record verification without any production credentials.
        windows = SimpleNamespace(workspace_root=PureWindowsPath(r"C:\test-root"), hostname="TEST-HOST",
                                  owner_executable_path=PureWindowsPath(r"C:\test-bin\owner.exe"),
                                  owner_executable_sha256=m6.digest("binary"), launcher_sha256=m6.digest("launcher"))
        binding = SimpleNamespace(runtime_root=root, windows=windows, service_root=root,
                                  python_executable=Path(sys.executable), env_dir=root,
                                  mac_exclusive_lock_path=root / "campaign.lock",
                                  env_files=lambda: (root / "worker.env", root / "cninfo.env"))
        plan = evaluation_plan()
        intent = campaign_intent_for_spec(
            self.spec, plan, runner_stop_reserve_seconds=5, verifier_deadline_seconds=80, bootstrap_bind_seconds=60,
            verifier_identity="test-verifier", close_grace_seconds=30,
            binding_sha256=m6.digest("release-binding"), release_manifest_sha256=m6.digest("release-manifest"),
        )
        inputs = SimpleNamespace(intent=intent, binding=binding, intent_sha256=intent.canonical_sha256(),
                                 intent_raw=intent.canonical_bytes(),
                                 manifest_path=root / "manifest.json", scope_path=root / "scope.json",
                                 quality_plan_path=root / "quality.json", worker_env_sha256=m6.digest("worker-env"),
                                 evaluation_plan=plan, evaluation_plan_raw=plan.canonical_bytes())
        assembly = M6CampaignAssembly(inputs, output=root / "output", mode=mode, attempt_id="attempt-1",
                                      launch=launch or Mock(), continuous_ns=lambda: 1_000_000_000)
        if create_output:
            assembly._output.mkdir()
        return assembly

    def attach_owner(self, assembly, directory):
        assembly._run = SimpleNamespace(anchor=self.anchor, path=Path(directory) / "run")
        assembly._controller = Mock()
        assembly._launcher = Mock()
        return owner.ScriptedOwner(self.spec, self.anchor, owner.ManualClock()).status(observed=self.spec.t0_ticks)

    def test_fresh_ready_requires_exact_identity_types_and_fields(self):
        value = self.ready()
        actual = parse_ready_line(canonical(value), expected_run_id=self.spec.run_id)
        self.assertEqual(actual.anchor, self.anchor)
        variants = [dict(value, extra=True), dict(value, journal_prefix_bytes=False),
                    dict(value, anchor_sha256=m6.digest("wrong-anchor")), dict(value, owner_epoch_sha256=m6.OWNER_EPOCH_2),
                    dict(value, spec_sha256=self.spec.canonical_sha256())]
        for bad in variants:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_ready_line(canonical(bad), expected_run_id=self.spec.run_id)
        with self.assertRaises(ValueError):
            parse_ready_line(canonical(value)[:-1] + ',"status":"ready_unbound"}', expected_run_id=self.spec.run_id)

    def test_recovered_ready_keeps_original_anchor_and_reports_new_incarnation(self):
        value = dict(self.ready(), status="ready_recovered", owner_epoch_sha256=m6.OWNER_EPOCH_2,
                     spec_sha256=self.spec.canonical_sha256(), journal_prefix_bytes=123,
                     journal_prefix_sha256=m6.digest("original-prefix"))
        actual = parse_ready_line(canonical(value), expected_run_id=self.spec.run_id, expect_recovered=True)
        self.assertEqual(actual.anchor, self.anchor)
        self.assertEqual(actual.owner_epoch_sha256, m6.OWNER_EPOCH_2)
        self.assertEqual(actual.spec_sha256, self.spec.canonical_sha256())
        for overrides in ({"spec_sha256": None}, {"owner_epoch_sha256": "wrong"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                parse_ready_line(canonical({**value, **overrides}), expected_run_id=self.spec.run_id, expect_recovered=True)

    def test_launcher_framing_does_not_parse_partial_line_or_replace_invalid_utf8(self):
        first = b'M6-READY {"first":1}\n'
        partial = b'M6-READY {"second":'
        self.assertEqual(launcher_lines(first + partial, "M6-READY"), ['{"first":1}'])
        self.assertEqual(launcher_lines(first + partial + b'2}\r\n', "M6-READY"), ['{"first":1}', '{"second":2}'])
        with self.assertRaises((UnicodeError, ValueError)):
            launcher_lines(b'M6-READY {"value":"\xff"}\n', "M6-READY")

    def test_launcher_transport_covers_delayed_t0_and_the_full_business_window(self):
        for planned, grace, ready_wait in ((60, 30, 30), (4800, 2400, 30)):
            with self.subTest(planned=planned), tempfile.TemporaryDirectory() as directory:
                launch = Mock()
                assembly = self.assembly(Path(directory), launch=launch)
                assembly._intent = assembly._intent.model_copy(update={
                    "run": assembly._intent.run.model_copy(update={"planned_seconds": planned}),
                    "close_grace_seconds": grace, "ready_wait_seconds": ready_wait,
                })
                with (patch.object(assembly, "_script", return_value="declared-test-launch"),
                      patch.object(assembly, "_ssh_argv", return_value=["declared-test-transport"])):
                    assembly._start_launcher(staged_name="test.json", deployment_sha256=m6.digest("config"))
                options = launch.call_args.kwargs
                # Transport starts before native T0. A legal delayed READY must not
                # shorten the 4800+2400 business deadline or force an unknown close.
                self.assertGreaterEqual(options["timeout_seconds"], planned + grace + ready_wait)
                self.assertLessEqual(options["timeout_seconds"], options.get("lifetime_ceiling_seconds", 7200))
                self.assertLessEqual(options.get("lifetime_ceiling_seconds", 7200), 8400)
                projection = json.loads((assembly._output / "launcher-command.json").read_text())
                self.assertEqual(projection["timeout_seconds"], options["timeout_seconds"])
                self.assertEqual(projection["expected_start"]["planned_seconds"], planned)
                self.assertEqual(projection["expected_start"]["close_grace_seconds"], grace)

    def test_insufficient_transport_tail_stops_before_either_business_child(self):
        with tempfile.TemporaryDirectory() as directory:
            launch = Mock()
            assembly = self.assembly(Path(directory), launch=launch)
            status = self.attach_owner(assembly, directory)
            remaining = (self.anchor.max_close_ticks - self.spec.t0_ticks) / self.anchor.clock.qpc_frequency_hz
            # The transport can reach max_close, but cannot leave its promised
            # exit/readback tail. No work may start under this truncated budget.
            assembly._launcher_deadline_ns = 1_000_000_000 + int(remaining * 1e9)
            with self.assertRaisesRegex(CampaignOutcomeUnknown, "transport deadline"):
                assembly._supervise_children(self.spec, status, 1_000_000_000)
            launch.assert_not_called()
            self.assertFalse((assembly._output / "runner").exists())
            self.assertFalse((assembly._output / "verifier").exists())

    def test_finish_wait_consumes_one_absolute_deadline_without_renewal(self):
        deadline = 101_000_000_000
        waits = [finish_wait_seconds(launcher_deadline_ns=deadline, now_ns=now)
                 for now in (1_000_000_000, 41_000_000_000, deadline, deadline + 1_000_000_000)]
        self.assertEqual(waits[0] - waits[1], 40)
        self.assertEqual(waits[1] - waits[2], 60)
        self.assertLessEqual(waits[2], 5)
        self.assertEqual(waits[2], waits[3], "expiry must not restart a business/grace window")

    def test_env_source_error_prevents_second_source_and_child_but_valid_shell_values_work(self):
        with tempfile.TemporaryDirectory(prefix="m6 env '") as directory:
            root = Path(directory)
            envs = (root / "worker.env", root / "cninfo.env")
            marker = root / "child-ran"
            envs[0].write_text("return 7\n")
            envs[1].write_text("M6_LITERAL='two words'\n")
            code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')"
            child = BoundedOwnerCommand(sourced_child_argv(envs, [sys.executable, "-c", code, str(marker)]), timeout_seconds=3)
            self.addCleanup(child.abort)
            result = child.finish()
            self.assertNotEqual(result.exit_code, 0)
            self.assertFalse(marker.exists(), "source failure must not launch a partly configured worker")
            envs[0].write_text("M6_NUMBER=7\n")
            code = "import json,os; print(json.dumps([os.environ['M6_NUMBER'],os.environ['M6_LITERAL']]))"
            child = BoundedOwnerCommand(sourced_child_argv(envs, [sys.executable, "-c", code]), timeout_seconds=3)
            self.addCleanup(child.abort)
            result = child.finish()
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), ["7", "two words"])
            self.assertNotIn("M6_NUMBER", os.environ)

    def test_prepare_result_can_be_decoded_by_a_new_assembly_before_staging_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            assembly = self.assembly(Path(directory))
            sid = "S-1-5-21-1-2-3-1001"
            private = str(assembly._private_root)
            paths = [private, str(assembly._private_root / "runs"), str(assembly._private_root / "staging"), str(assembly._private_root / "attempts")]
            receipt = dict(contract_version="m6.owner-workspace-prepare.v1", hostname="TEST-HOST", workspace_root=str(assembly._workspace),
                           private_root=paths[0], runs_root=paths[1], staging_root=paths[2], attempts_root=paths[3],
                           binary_sha256=m6.digest("binary"), launcher_sha256=m6.digest("launcher"), owner_sid=sid,
                           directories={path: {"protected": True, "sddl": f"O:{sid}D:P(A;;FA;;;{sid})(A;;FA;;;SY)"} for path in paths})
            raw = ("M6-PREPARE " + canonical(receipt) + '\nM6-RESULT ' + canonical({"contract_version": "m6.owner-launcher-result.v1", "exit_code": 0}) + '\n').encode()
            with patch.object(assembly, "_script", return_value="declared-script"), patch.object(assembly, "_ssh_argv", return_value=["declared-ssh"]), patch.object(assembly, "_owned", return_value=OwnerCommandResult(0, raw, b"")):
                actual = assembly._prepare()
            self.assertEqual(actual, receipt)
            self.assertTrue((assembly._output / "prepare-receipt.json").is_file())

    def test_second_child_spawn_failure_cannot_leave_first_child_unowned(self):
        runner = Mock(captured_output=(b"runner", b""))
        original_error = OSError("declared verifier spawn failure")
        with tempfile.TemporaryDirectory() as directory:
            assembly = self.assembly(Path(directory), launch=Mock(side_effect=[runner, original_error]))
            assembly._run = SimpleNamespace(anchor=self.anchor, path=Path(directory) / "run")
            assembly._controller = Mock()
            assembly._launcher = Mock()
            status = owner.ScriptedOwner(self.spec, self.anchor, owner.ManualClock()).status(observed=self.spec.t0_ticks)
            with self.assertRaisesRegex(CampaignOutcomeUnknown, "declared verifier spawn failure") as caught:
                assembly._supervise_children(self.spec, status, 1_000_000_000)
            self.assertIs(caught.exception.__cause__, original_error)
            runner.abort.assert_called_once()

    def test_an_exit_record_without_matching_start_record_is_not_remote_exit_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            assembly = self.assembly(Path(directory))
            record = {"contract_version": "m6.owner-external-exit.v2", "run_id": self.spec.run_id,
                      "attempt_id": "attempt-1", "binary_sha256": m6.digest("binary"), "launcher_sha256": m6.digest("launcher"),
                      "pid": 123, "creation_filetime_100ns": 456, "configuration_sha256": m6.digest("unbound-config"),
                      "process_handle_signaled": True, "exit_code": 0, "forced_termination": False,
                      "parent_failure": None, "cancel": None, "ready_received": True}
            def fetch(*args, **kwargs):
                # Deliberately no start/READY record, even though transport and exit JSON say zero.
                (assembly._output / "launcher-records" / "process-exit.json").write_text(canonical(record))
                return OwnerCommandResult(0, b"", b"")
            with patch.object(assembly, "_sftp_argv", return_value=["declared-sftp"]), patch.object(assembly, "_owned", side_effect=fetch):
                self.assertFalse(assembly._verify_external_exit())

    def test_spawn_failure_and_failed_abort_cannot_claim_child_reaped(self):
        runner = Mock(captured_output=(b"runner", b""), poll=Mock(return_value=None),
                      abort=Mock(side_effect=OSError("declared runner remains alive")))
        original = OSError("declared second spawn failure")
        with tempfile.TemporaryDirectory() as directory:
            assembly = self.assembly(Path(directory), launch=Mock(side_effect=[runner, original]))
            status = self.attach_owner(assembly, directory)
            with self.assertRaises(CampaignOutcomeUnknown) as caught:
                assembly._supervise_children(self.spec, status, 1_000_000_000)
            self.assertIs(caught.exception.__cause__, original)
            runner.abort.assert_called_once()
            self.assertFalse(assembly._children_reaped, "failed abort and no exit proof must remain unresolved")

    def test_failed_cleanup_still_handles_other_child_and_preserves_first_failure(self):
        runner = Mock(captured_output=(b"runner", b""), poll=Mock(return_value=None),
                      abort=Mock(side_effect=OSError("declared abort failure")))
        verifier = Mock(captured_output=(b"verifier", b""), poll=Mock(return_value=None))
        verifier.abort.side_effect = lambda: setattr(verifier.poll, "return_value", OwnerCommandResult(-15, b"verifier", b""))
        original = OSError("declared supervision failure")
        with tempfile.TemporaryDirectory() as directory:
            assembly = self.assembly(Path(directory), launch=Mock(side_effect=[runner, verifier]))
            status = self.attach_owner(assembly, directory)
            assembly._launcher.poll.side_effect = original
            error = None
            try:
                assembly._supervise_children(self.spec, status, 1_000_000_000)
            except (OSError, CampaignOutcomeUnknown) as exc:
                error = exc
            verifier.abort.assert_called_once()
            self.assertFalse(assembly._children_reaped)
            chain = []
            while error is not None:
                chain.append(error)
                error = error.__cause__
            preserved = original in chain or "declared supervision failure" in str(assembly._summary.first_error)
            self.assertTrue(preserved, "cleanup must not replace the original failure")

    def test_business_children_cannot_receive_a_new_deadline_past_original_max_close(self):
        for spawn_delay_seconds in (0, 12):
            with self.subTest(spawn_delay_seconds=spawn_delay_seconds), tempfile.TemporaryDirectory() as directory:
                children = [Mock(captured_output=(b"", b""), poll=Mock(return_value=None)) for _ in range(2)]
                for child in children:
                    child.abort.side_effect = lambda child=child: setattr(child.poll, "return_value", OwnerCommandResult(-15, b"", b""))
                now = [1_000_000_000]
                launched_at = []
                def spawn(argv, **kwargs):
                    launched_at.append(now[0])
                    child = children[len(launched_at) - 1]
                    now[0] += spawn_delay_seconds * 1_000_000_000
                    return child
                launch = Mock(side_effect=spawn)
                assembly = self.assembly(Path(directory), launch=launch)
                assembly._now_ns = lambda: now[0]
                status = self.attach_owner(assembly, directory)
                assembly._launcher.poll.side_effect = OSError("declared supervision stop")
                try:
                    assembly._supervise_children(self.spec, status, 1_000_000_000)
                except (OSError, CampaignOutcomeUnknown):
                    pass
                # A delayed first spawn consumes the same original budget; the second cannot restart it.
                remaining = (self.anchor.max_close_ticks - self.spec.t0_ticks) / self.anchor.clock.qpc_frequency_hz
                self.assertEqual(launch.call_count, 2)
                for call, started_at in zip(launch.call_args_list, launched_at, strict=True):
                    self.assertGreater(call.kwargs["timeout_seconds"], 0)
                    self.assertLessEqual(call.kwargs["timeout_seconds"], remaining - (started_at - 1_000_000_000) / 1e9)

    def test_execute_cleanup_failure_still_releases_lock_and_records_original_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            assembly = self.assembly(Path(directory), mode="bootstrap-check", create_output=False)
            lock = Mock()
            def start(**kwargs):
                assembly._launcher = Mock()
            prefix = "disclosure_anchor.adapters.runtime.m6_campaign_assembly."
            with (patch(prefix + "acquire_exclusivity", return_value=lock),
                  patch(prefix + "generate_roles", return_value={}),
                  patch(prefix + "write_run_directory"),
                  patch(prefix + "deployment_document", return_value={"declared": "private deployment"}),
                  patch.object(assembly, "_prepare"), patch.object(assembly, "_stage_deployment", return_value="staged.json"),
                  patch.object(assembly, "_start_launcher", side_effect=start),
                  patch.object(assembly, "_await_ready", side_effect=CampaignOutcomeUnknown("declared original READY failure")),
                  patch.object(assembly, "_cancel"),
                  patch.object(assembly, "_finish_launcher", side_effect=OSError("declared launcher reap failure")),
                  patch.object(assembly, "_verify_external_exit", return_value=False) as verify):
                summary = assembly.execute()
            lock.release.assert_called_once()
            verify.assert_called_once()
            self.assertNotEqual(summary["status"], "complete")
            self.assertIn("declared original READY failure", summary["first_error"]["message"])
            self.assertIn("declared launcher reap failure", str(summary["stages"]))
            self.assertEqual(json.loads((assembly._output / "campaign-summary.json").read_text()), summary)

    def test_bootstrap_execute_positive_and_cleanup_failures_have_distinct_outcomes(self):
        for fault in (None, "controller", "finish", "verify", "release"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                assembly = self.assembly(Path(directory), mode="bootstrap-check", create_output=False)
                lock, controller = Mock(), Mock()
                if fault == "release":
                    lock.release.side_effect = OSError("declared release failure")
                if fault == "controller":
                    controller.close.side_effect = OSError("declared controller failure")
                def start(**kwargs):
                    assembly._launcher = Mock()
                    assembly._controller = controller
                prefix = "disclosure_anchor.adapters.runtime.m6_campaign_assembly."
                def inspect_frozen_inputs():
                    self.assertEqual((assembly._output / "campaign-intent.json").read_bytes(),
                                     assembly._inputs.intent.canonical_bytes())
                    self.assertEqual((assembly._output / "evaluation-plan.json").read_bytes(),
                                     assembly._inputs.evaluation_plan_raw)
                    frozen = json.loads((assembly._output / "campaign-inputs.json").read_bytes())
                    self.assertEqual(frozen["intent_sha256"], assembly._inputs.intent.canonical_sha256())
                ready = SimpleNamespace(anchor_sha256=self.anchor.canonical_sha256(),
                                        owner_epoch_sha256=self.anchor.owner_process_epoch_sha256)
                status = owner.ScriptedOwner(self.spec, self.anchor, owner.ManualClock()).status(observed=self.spec.t0_ticks)
                with (patch(prefix + "acquire_exclusivity", return_value=lock),
                      patch(prefix + "generate_roles", return_value={}),
                      patch(prefix + "write_run_directory"),
                      patch(prefix + "deployment_document", return_value={"declared": "private deployment"}),
                      patch.object(assembly, "_prepare", side_effect=inspect_frozen_inputs), patch.object(assembly, "_stage_deployment", return_value="staged.json"),
                      patch.object(assembly, "_start_launcher", side_effect=start),
                      patch.object(assembly, "_await_ready", return_value=ready),
                      patch.object(assembly, "_bind_and_open", return_value=(self.spec, status, 1_000_000_000)),
                      patch.object(assembly, "_bootstrap_closure", return_value={"close": {"outcome": "ok"}, "admitted_count": 0}),
                      patch.object(assembly, "_cancel"),
                      patch.object(assembly, "_finish_launcher", side_effect=OSError("declared finish failure") if fault == "finish" else None),
                      patch.object(assembly, "_verify_external_exit", return_value=True,
                                   side_effect=OSError("declared verify failure") if fault == "verify" else None) as verify):
                    summary = assembly.execute()
                lock.release.assert_called_once()
                verify.assert_called_once()
                self.assertEqual(summary["admitted_count"], 0)
                self.assertEqual(summary["database_access"], "none")
                if fault is None:
                    self.assertEqual(summary["status"], "complete")
                    self.assertIsNone(summary["first_error"])
                    self.assertTrue(summary["owner_external_exit_verified"])
                else:
                    self.assertNotEqual(summary["status"], "complete")
                    self.assertIn("declared " + fault + " failure", str(summary["first_error"]))
                self.assertEqual(json.loads((assembly._output / "campaign-summary.json").read_text()), summary)
