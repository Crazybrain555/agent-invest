"""Independent oracles for the shared admission STOP, end to end and without a process.

A measurement failure must stop *new admission* and let already accepted work finish
inside its original close bound; it must never interrupt the campaign. That makes one
file the whole control surface, so the cases below cross it rather than assert it from
one side: the driver publishes the real control file, the real ``m6_campaign`` parser and
constructor accept that path, the real ``_supervise_children`` hands the same path to the
runner, the real ``staged_campaign`` parser binds it, and the real reader accepts the
bytes the driver actually wrote.

No SSH, DB, GPU, remote or owner session runs here. Three bounded local subprocesses do:
a probe of the control reader, which is the only way to tell "refused" from "hung", and two
real children for the drain itself, because "never interrupted" and "really reaped" are
properties of signals and exit status that a stand-in can only assert about itself.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path, PureWindowsPath
import signal
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from disclosure_anchor.adapters.runtime import m6_campaign_assembly
from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
from disclosure_anchor.adapters.runtime.m6_campaign_assembly import (
    CampaignInputError, M6CampaignAssembly,
)
from disclosure_anchor.adapters.runtime.resident_owner_control import OwnerCommandResult
from disclosure_anchor.application.contracts.m6_owner import M6OwnerControl
from disclosure_anchor.cli import m6_campaign, staged_campaign
from tests import m6_owner_support as owner
from tests import m6_support as m6
from tests.integration import m6_measured_campaign_independent as driver
from tests.m6_cli_support import parse_cli_args
from tests.m6_delivery_support import campaign_intent_for_spec, evaluation_plan
from tests.unit.test_m6_measured_driver_independent import FakeChild


STOP_BYTES = b"stop\n"
# One bounded probe of the control reader, run out of process. Long enough that a slow
# machine cannot fake a hang, short enough that a real hang is reported, never waited out.
CONTROL_READ_BOUND_SECONDS = 5.0
_PROBE = """
import json, pathlib, sys, time
from types import SimpleNamespace
from disclosure_anchor.adapters.runtime.m6_campaign_assembly import M6CampaignAssembly
stand_in = SimpleNamespace(_admission_stop_file=pathlib.Path(sys.argv[1]))
started = time.monotonic()
try:
    outcome = {"returned": M6CampaignAssembly._external_stop_requested(stand_in), "error": None}
except BaseException as exc:
    outcome = {"returned": None, "error": type(exc).__name__, "message": str(exc)[:200]}
outcome["seconds"] = round(time.monotonic() - started, 3)
print(json.dumps(outcome), flush=True)
"""


class SupervisedChild:
    """A business child stand-in for the entry's supervision loop; it owns no process.

    ``exits_after`` polls model work that is still draining: the loop must keep
    polling it rather than abort it, and ``aborted`` records the difference.
    """

    def __init__(self, *, exits_after=1, exit_code=0):
        self._remaining = exits_after
        self._exit_code = exit_code
        self.captured_output = (b"", b"")
        self.aborted = False
        self.polls = 0

    def poll(self, timeout=0):
        self.polls += 1
        if self._remaining > 0:
            self._remaining -= 1
            return None
        return OwnerCommandResult(self._exit_code, b"", b"")

    def retention_report(self):
        return {"truncated": False}

    def abort(self):
        self.aborted = True
        self._remaining = 0


class DrainingChild(FakeChild):
    """Still running when the drain starts, then exits on its own before the deadline."""

    def __init__(self, *, natural_exit=0, **keywords):
        super().__init__(**keywords)
        self._natural_exit = natural_exit
        self.last_wait = None

    def wait(self, timeout):
        self.last_wait = timeout
        return self._natural_exit


class AdmissionStopHarness(unittest.TestCase):
    """The frozen fixture and declared port doubles the entry needs, and nothing else."""

    def setUp(self):
        self.spec = m6.make_fixture("e2e_publication", {"a": (7, "replay")}).spec
        self.anchor = owner.anchor_for(self.spec)

    def inputs_for(self, root):
        windows = SimpleNamespace(workspace_root=PureWindowsPath(r"C:\test-root"), hostname="TEST-HOST",
                                  owner_executable_path=PureWindowsPath(r"C:\test-bin\owner.exe"),
                                  owner_executable_sha256=m6.digest("binary"), launcher_sha256=m6.digest("launcher"))
        binding = SimpleNamespace(
            runtime_root=root, windows=windows, service_root=root,
            python_executable=Path(sys.executable), env_dir=root,
            mac_exclusive_lock_path=root / "campaign.lock",
            env_files=lambda: (root / "worker.env", root / "cninfo.env"))
        plan = evaluation_plan()
        intent = campaign_intent_for_spec(
            self.spec, plan, runner_stop_reserve_seconds=5, verifier_deadline_seconds=80, bootstrap_bind_seconds=60,
            verifier_identity="test-verifier", close_grace_seconds=30,
            binding_sha256=m6.digest("release-binding"), release_manifest_sha256=m6.digest("release-manifest"),
        )
        return SimpleNamespace(intent=intent, binding=binding, intent_sha256=intent.canonical_sha256(),
                               intent_raw=intent.canonical_bytes(),
                               manifest_path=root / "manifest.json", scope_path=root / "scope.json",
                               quality_plan_path=root / "quality.json", worker_env_sha256=m6.digest("worker-env"),
                               evaluation_plan=plan, evaluation_plan_raw=plan.canonical_bytes())

    def assembly(self, root, *, launch=None, admission_stop_file=None):
        assembly = M6CampaignAssembly(self.inputs_for(root), output=root / "output", mode="run", attempt_id="attempt-1",
                                      launch=launch or Mock(), continuous_ns=lambda: 1_000_000_000,
                                      admission_stop_file=admission_stop_file)
        assembly._output.mkdir()
        return assembly

    def attach_owner(self, assembly, root):
        assembly._run = SimpleNamespace(anchor=self.anchor, path=root / "run")
        # By supervision the run directory exists: bind and open loaded it. `attach_owner`
        # only names it, so it is created here, with nothing else of the real bind sequence.
        assembly._run.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        assembly._controller = Mock()
        assembly._launcher = Mock()
        # A launcher that is still carrying the transport: no launcher failure of its own.
        assembly._launcher.poll = Mock(return_value=None)
        return owner.ScriptedOwner(self.spec, self.anchor, owner.ManualClock()).status(observed=self.spec.t0_ticks)

    def supervise(self, assembly, status):
        return assembly._supervise_children(self.spec, status, 1_000_000_000)

    @staticmethod
    def stop_requests(assembly):
        return [call for call in assembly._controller.request.call_args_list
                if call.args and call.args[0] == M6OwnerControl(kind="stop")]

    @staticmethod
    def argv_value(argv, name):
        return argv[argv.index(name) + 1]

    @staticmethod
    def spawning(children):
        """A launch factory that hands out the given children and records every argv."""
        captured = []

        def spawn(argv, **keywords):
            captured.append({"argv": list(argv), "timeout_seconds": keywords.get("timeout_seconds")})
            return children[len(captured) - 1]

        return Mock(side_effect=spawn), captured

    @staticmethod
    def build_control_after_spawn(launch, captured, build, *, spawns=2):
        """Run ``build`` once the given number of children are already owned.

        A control file that exists before the first spawn is a different case: the entry
        takes its zero-admission branch. Everything about draining accepted work needs the
        control to appear while the children are running, which is what this arranges.
        """
        spawn = launch.side_effect
        note = {}

        def spawn_then_build(argv, **keywords):
            child = spawn(argv, **keywords)
            if len(captured) == spawns:
                note["note"] = build()
            return child

        launch.side_effect = spawn_then_build
        return note

    @staticmethod
    def publish_stop(control):
        """Write the control file exactly as the product's own writer would."""
        write_new_exact(control, STOP_BYTES)


class AdmissionStopControlChainTests(AdmissionStopHarness):
    """D1: one file, published by the driver and honoured by the entry and its runner."""

    def test_the_stop_the_driver_publishes_is_the_file_the_entry_and_its_runner_read(self):
        """The whole control surface crossed once, with no re-declared argument names.

        The driver writes the real control file; the campaign CLI's own parser and the real
        constructor accept that path; the real supervision hands the same absolute path to
        the runner, whose own parser binds it; and the real reader accepts exactly those
        bytes. A shared STOP that any one of those four disagrees about is a stop that
        never arrives.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "campaign-admission.STOP"

            # 1. The campaign CLI's own parser and the real constructor, over the argv the
            #    campaign child receives. Only `execute` is replaced: nothing may run.
            constructed = []

            def record(self):
                constructed.append(self)
                return {"status": "failed", "first_error": {"stage": "declared", "message": "not executed"}}

            argv = ["run", "--intent", str(root / "intent.json"), "--intent-sha256", m6.digest("intent"),
                    "--private-binding", str(root / "private.json"), "--binding", str(root / "binding.json"),
                    "--release-manifest", str(root / "release.json"), "--manifest", str(root / "manifest.json"),
                    "--scope", str(root / "scope.json"), "--quality-plan", str(root / "quality.json"),
                    "--evaluation-plan", str(root / "plan.json"), "--output", str(root / "output"),
                    "--admission-stop-file", str(control)]
            with patch.object(m6_campaign, "load_campaign_inputs", return_value=self.inputs_for(root)), \
                    patch.object(M6CampaignAssembly, "execute", record), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m6_campaign.main(argv), 1)
            self.assertEqual(len(constructed), 1)
            self.assertEqual(constructed[0]._admission_stop_file, control)

            # 2. The real supervision hands that exact path to the runner.
            launch, captured = self.spawning([SupervisedChild(exits_after=1), SupervisedChild(exits_after=1)])
            assembly = self.assembly(root, launch=launch, admission_stop_file=control)
            status = self.attach_owner(assembly, root)
            self.supervise(assembly, status)
            runner_argv = captured[0]["argv"]
            self.assertEqual(Path(self.argv_value(runner_argv, "--stop-file")), control)

            # 3. The runner's own parser binds it, over the argv its process receives.
            module = "disclosure_anchor.cli.staged_campaign"
            self.assertEqual(parse_cli_args(staged_campaign, runner_argv[runner_argv.index(module) + 1:]).stop_file,
                             control)

            # 4. The driver's own drain publishes the control file at that same path.
            evidence = {}
            driver.drain_campaign(DrainingChild(), admission_stop_file=control, deadline_monotonic=10.0,
                                  evidence=evidence, now=lambda: 0.0)
            self.assertTrue(evidence["admission_stop_requested"])
            self.assertEqual(control.read_bytes(), STOP_BYTES)
            info = control.lstat()
            self.assertTrue(stat.S_ISREG(info.st_mode))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)

            # 5. And the entry's real reader accepts exactly the bytes the driver wrote.
            self.assertTrue(assembly._external_stop_requested())

    def test_a_stop_while_the_children_run_drains_them_instead_of_interrupting_them(self):
        """Accepted work finishes: the entry asks the owner to stop admitting and keeps polling."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "campaign-admission.STOP"
            runner, verifier = SupervisedChild(exits_after=3), SupervisedChild(exits_after=2)
            launch, captured = self.spawning([runner, verifier])
            assembly = self.assembly(root, launch=launch, admission_stop_file=control)
            status = self.attach_owner(assembly, root)
            # The control appears only once both children are already working.
            self.build_control_after_spawn(launch, captured, lambda: self.publish_stop(control))

            record = self.supervise(assembly, status)

            self.assertEqual(len(captured), 2, "a stop does not cancel an already scheduled child")
            self.assertFalse(runner.aborted or verifier.aborted, "the drain must not abort accepted work")
            self.assertEqual(record["runner"]["exit_code"], 0)
            self.assertEqual(record["verifier"]["exit_code"], 0)
            self.assertEqual(record["failures"],
                             {"external_admission_stop": "external admission stop; original close deadline unchanged"})
            self.assertEqual(len(self.stop_requests(assembly)), 1, "one owner stop, not one per poll")
            self.assertTrue(assembly._children_reaped)
            # The measurement failure the stop came from is still a failure.
            self.assertEqual(assembly._summary.first_error["stage"], "supervision")

    def test_a_stop_before_the_first_spawn_admits_nothing_and_invents_no_receipt(self):
        """Zero admission goes through the existing zero-admission closure, not a made-up one."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "campaign-admission.STOP"
            self.publish_stop(control)
            launch, captured = self.spawning([])
            assembly = self.assembly(root, launch=launch, admission_stop_file=control)
            status = self.attach_owner(assembly, root)
            # The zero-admission path has its own coverage; what matters here is that it,
            # and not a business reconciliation, is the branch a pre-spawn stop takes.
            bootstrap = Mock(return_value={"steps": [], "admitted_count": 0})
            assembly._bootstrap_closure = bootstrap

            record = self.supervise(assembly, status)

            self.assertEqual(captured, [], "no business child may be spawned after a pre-spawn stop")
            bootstrap.assert_called_once_with(self.spec)
            self.assertEqual(record["admitted_count"], 0)
            self.assertEqual(record["failures"], {"external_admission_stop": "before business spawn"})
            self.assertNotIn("runner", record, "no runner receipt exists to reconcile")
            self.assertTrue(assembly._children_reaped)
            self.assertEqual(assembly._summary.first_error["message"], "external admission stop before business spawn")

    def test_a_stop_between_the_two_spawns_still_reconciles_the_child_that_started(self):
        """The race the entry cannot prevent: once anything is spawned, business facts decide."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "campaign-admission.STOP"
            runner, verifier = SupervisedChild(exits_after=2), SupervisedChild(exits_after=1)
            launch, captured = self.spawning([runner, verifier])
            assembly = self.assembly(root, launch=launch, admission_stop_file=control)
            status = self.attach_owner(assembly, root)
            bootstrap = Mock()
            assembly._bootstrap_closure = bootstrap
            # After the runner, before the verifier.
            self.build_control_after_spawn(launch, captured, lambda: self.publish_stop(control), spawns=1)

            record = self.supervise(assembly, status)

            bootstrap.assert_not_called()
            self.assertEqual(len(captured), 2)
            self.assertFalse(runner.aborted or verifier.aborted)
            self.assertEqual(record["runner"]["exit_code"], 0)
            self.assertEqual(len(self.stop_requests(assembly)), 1)
            self.assertIn("external_admission_stop", record["failures"])

    def test_a_repeated_stop_and_a_second_failure_never_extend_a_child(self):
        """No renewal: one owner stop for the whole run, and no lifetime issued after the spawns."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "campaign-admission.STOP"
            # A verifier that fails later, so the entry's own failure branch runs too while the
            # external control file is already present.
            runner, verifier = SupervisedChild(exits_after=4), SupervisedChild(exits_after=1, exit_code=3)
            launch, captured = self.spawning([runner, verifier])
            assembly = self.assembly(root, launch=launch, admission_stop_file=control)
            status = self.attach_owner(assembly, root)
            assembly._bootstrap_closure = Mock(side_effect=AssertionError("business children were spawned"))
            self.build_control_after_spawn(launch, captured, lambda: self.publish_stop(control))

            record = self.supervise(assembly, status)

            self.assertEqual(len(self.stop_requests(assembly)), 1,
                             "the external stop and the entry's own failure are one request, not two")
            self.assertEqual(control.read_bytes(), STOP_BYTES, "the control file is written once, not rewritten")
            self.assertEqual(record["verifier"]["exit_code"], 3)
            lifetimes = [spawn["timeout_seconds"] for spawn in captured]
            self.assertEqual(len(lifetimes), 2)
            self.assertTrue(all(0 < value <= assembly._summary.values["budgets"]["child_lifetime_seconds"]
                                for value in lifetimes),
                            "a child lifetime is bounded by the original close bound, never renewed")


class AdmissionStopControlIntegrityTests(AdmissionStopHarness):
    """D2: a control path that is not exactly the agreed file never resumes admission."""

    CONTROLS = {
        "loose_mode": ("a control file any other process could rewrite", 0o644, STOP_BYTES),
        "trailing_bytes": ("extra bytes after the agreed control word", 0o600, STOP_BYTES + b"resume"),
        "other_word": ("a different control word", 0o600, b"go\n"),
        "empty": ("an empty control file", 0o600, b""),
    }

    @staticmethod
    def plant(control, mode, payload):
        control.write_bytes(payload)
        control.chmod(mode)

    @staticmethod
    def plant_directory(control):
        control.mkdir(mode=0o700)

    @staticmethod
    def plant_swapped_symlink(control):
        real = control.with_name("real-stop")
        real.write_bytes(STOP_BYTES)
        real.chmod(0o600)
        control.symlink_to(real)

    def test_a_forged_control_before_any_admission_refuses_the_whole_run(self):
        """Nothing has been admitted yet, so a control nobody can vouch for ends the run."""
        for name, (note, mode, payload) in self.CONTROLS.items():
            with self.subTest(control=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                control = root / "campaign-admission.STOP"
                self.plant(control, mode, payload)
                launch, captured = self.spawning([])
                assembly = self.assembly(root, launch=launch, admission_stop_file=control)
                status = self.attach_owner(assembly, root)
                assembly._bootstrap_closure = Mock(side_effect=AssertionError("a forged control is not a stop"))
                with self.assertRaises(CampaignInputError) as caught:
                    self.supervise(assembly, status)
                self.assertEqual(str(caught.exception), "admission stop control identity or bytes changed", note)
                self.assertEqual(captured, [], note)
                self.assertEqual(self.stop_requests(assembly), [], note)

    def test_a_forged_control_while_work_is_in_flight_stops_admission_without_destroying_it(self):
        """Accepted work is reconciled from real facts even when the control itself is broken.

        A control that fails its identity check mid-run is a control-integrity failure, so
        admission must close - but the runner and verifier were admitted under the original
        max_close and their outcome is still a business fact. Tearing them down over an
        unreadable file would destroy exactly the evidence this round exists to preserve,
        and would be indistinguishable from the interrupt A removed. The refusal must also
        stay separable from an operator's valid stop, or a forged control reads as one.
        """
        cases = dict(self.CONTROLS)
        cases["directory"] = ("a directory where the control file belongs", None, None)
        cases["swapped_symlink"] = ("the control path swapped for a symlink after it was accepted", None, None)
        for name, (note, mode, payload) in cases.items():
            with self.subTest(control=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                control = root / "campaign-admission.STOP"
                runner, verifier = SupervisedChild(exits_after=3), SupervisedChild(exits_after=3)
                launch, captured = self.spawning([runner, verifier])
                assembly = self.assembly(root, launch=launch, admission_stop_file=control)
                status = self.attach_owner(assembly, root)
                if name == "directory":
                    build = lambda: self.plant_directory(control)          # noqa: E731 - one expression
                elif name == "swapped_symlink":
                    build = lambda: self.plant_swapped_symlink(control)    # noqa: E731 - one expression
                else:
                    build = lambda mode=mode, payload=payload: self.plant(control, mode, payload)  # noqa: E731
                self.build_control_after_spawn(launch, captured, build)

                record = self.supervise(assembly, status)

                self.assertFalse(runner.aborted or verifier.aborted, note)
                self.assertEqual(record["runner"]["exit_code"], 0, note)
                self.assertEqual(record["verifier"]["exit_code"], 0, note)
                self.assertEqual(len(self.stop_requests(assembly)), 1, note)
                reported = " | ".join(f"{key}: {value}" for key, value in record["failures"].items())
                self.assertTrue(record["failures"], note)
                self.assertNotEqual(set(record["failures"]), {"external_admission_stop"},
                                    "a forged control must not be recorded as an operator's valid stop")
                self.assertRegex(reported, "(?i)control|stop", note)

    def test_a_control_path_that_cannot_answer_is_refused_rather_than_waited_on(self):
        """A FIFO at the control path must not park the supervisor in ``open``.

        The reader runs in its own process with a hard bound, because the difference this
        case exists for - refused versus hung - cannot be observed in-process without
        risking the suite. A child still alive at the bound is killed and reaped here.
        """
        # The probe must read exactly the product this test imported, wherever it came from.
        source_root = Path(m6_campaign_assembly.__file__).resolve().parents[3]
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(source_root))
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory) / "campaign-admission.STOP"
            os.mkfifo(control, 0o600)
            try:
                probe = subprocess.run([sys.executable, "-B", "-c", _PROBE, str(control)],
                                       capture_output=True, env=environment,
                                       timeout=CONTROL_READ_BOUND_SECONDS)
            except subprocess.TimeoutExpired:
                self.fail(f"the admission stop reader did not answer within {CONTROL_READ_BOUND_SECONDS}s on a FIFO: "
                          "a control path that blocks in open() suspends supervision for as long as no writer "
                          "appears, with no deadline, no owner stop and no summary")
            outcome = json.loads(probe.stdout.decode())
            self.assertIsNone(outcome["returned"],
                              "a FIFO is not the agreed control file and must never read as a stop or as no stop")
            self.assertIn(outcome["error"], ("CampaignInputError", "OSError"), probe.stderr.decode()[-300:])

    @unittest.skipUnless(Path("/dev/fd").is_dir(), "descriptor accounting needs /dev/fd")
    def test_a_refused_control_read_keeps_no_descriptor_of_its_own(self):
        """The reader runs once per supervision poll, so it must close what it opens.

        A reader that keeps a descriptor per refused read exhausts the process instead of
        reporting the problem, which matters exactly when the run is told to keep going.
        """
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory) / "campaign-admission.STOP"
            control.mkdir(mode=0o700)
            reader = SimpleNamespace(_admission_stop_file=control)
            before = len(os.listdir("/dev/fd"))
            for _ in range(20):
                with self.assertRaises((CampaignInputError, OSError)):
                    M6CampaignAssembly._external_stop_requested(reader)
            self.assertEqual(len(os.listdir("/dev/fd")), before,
                             "a refused control read leaked one descriptor per attempt")

    def test_a_control_path_outside_the_private_parent_is_refused_when_the_entry_is_built(self):
        """The declared path itself is checked once, before any admission can exist."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "campaign-admission.STOP"
            real = root / "real-stop"
            real.write_bytes(STOP_BYTES)
            control.symlink_to(real)
            for name, path in (("symlink", control), ("relative", Path("campaign-admission.STOP")),
                               ("another_parent", root / "elsewhere" / "campaign-admission.STOP")):
                with self.subTest(control=name), self.assertRaises(CampaignInputError) as caught:
                    self.assembly(root, admission_stop_file=path)
                self.assertEqual(str(caught.exception),
                                 "admission stop file must be a direct child of the private campaign parent")


class AdmissionStopDrainBoundTests(unittest.TestCase):
    """D2 on the driver's side: one request, the original deadline, and no second grace."""

    def drain(self, campaign, *, control, deadline=10.0, at=0.0):
        evidence = {}
        code = driver.drain_campaign(campaign, admission_stop_file=control, deadline_monotonic=deadline,
                                     evidence=evidence, now=lambda: at)
        return code, evidence

    def test_the_drain_asks_once_and_waits_only_to_the_original_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory) / "campaign-admission.STOP"
            campaign = DrainingChild(natural_exit=0)
            code, evidence = self.drain(campaign, control=control, deadline=10.0, at=2.5)
            self.assertEqual(code, 0)
            self.assertFalse(campaign.interrupted, "a measurement failure never interrupts the campaign")
            self.assertFalse(campaign.killed)
            self.assertEqual(campaign.last_wait, 7.5, "the wait is the remainder of the original deadline")
            self.assertEqual(evidence["campaign_drain_deadline_monotonic"], 10.0)
            self.assertNotIn("campaign_forced", evidence)
            self.assertIn("unverified", evidence["owner_outcome"])

            # The cleanup path may drain again; the request is one-way and the deadline is the same.
            second = DrainingChild(natural_exit=0)
            code, again = self.drain(second, control=control, deadline=10.0, at=6.0)
            self.assertEqual(code, 0)
            self.assertFalse(second.interrupted or second.killed)
            self.assertEqual(second.last_wait, 4.0, "a repeated drain shortens; it never renews")
            self.assertEqual(again["campaign_drain_deadline_monotonic"], 10.0)
            self.assertEqual(control.read_bytes(), STOP_BYTES)
            self.assertEqual(sorted(path.name for path in control.parent.iterdir()),
                             ["campaign-admission.STOP"], "no partial sibling is left behind")

    def test_an_exhausted_deadline_or_a_lost_control_forces_a_bounded_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # The original deadline is already spent: the drain does not wait past it.
            stubborn = FakeChild()
            code, evidence = self.drain(stubborn, control=root / "spent.STOP", deadline=10.0, at=11.0)
            self.assertEqual(stubborn.last_wait, 0.0, "a spent deadline is clamped, never negative")
            self.assertTrue(stubborn.killed and evidence["campaign_forced"])
            self.assertTrue(evidence["campaign_drain_deadline_exhausted"])
            self.assertEqual((code, evidence["campaign_exit_code"]), (-9, -9))

            # The control file cannot be published: capability is lost, so this fails closed
            # and does not claim a stop that no one can read.
            lost = FakeChild()
            code, evidence = self.drain(lost, control=root / "missing-parent" / "control.STOP", deadline=10.0)
            self.assertTrue(lost.killed and evidence["campaign_forced"])
            self.assertFalse(lost.interrupted)
            self.assertIn("admission_stop_write_failure", evidence)
            self.assertNotIn("admission_stop_requested", evidence)

    def test_a_drain_without_the_original_deadline_refuses_before_it_writes_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory) / "campaign-admission.STOP"
            for missing in (None, float("inf"), float("nan")):
                with self.subTest(deadline=missing):
                    campaign = FakeChild()
                    with self.assertRaises(AssertionError):
                        self.drain(campaign, control=control, deadline=missing)
                    self.assertFalse(control.exists(), "no control byte is published without a bound to honour")
                    self.assertFalse(campaign.killed or campaign.interrupted)



# Two real subprocesses, each with its own bound. R23 section 8.1 asks for the failure-close
# chain over an actual process rather than a stand-in, because "never interrupted" and
# "really reaped" are properties of signals and exit status, not of a recorded flag.
DRAIN_OBSERVES_STOP = """
import os, signal, sys, time
marker, control = sys.argv[1], sys.argv[2]


def note(_signum, _frame):
    with open(marker, "a") as record:
        record.write("interrupted\\n")


signal.signal(signal.SIGINT, note)
signal.signal(signal.SIGTERM, note)
print("working", flush=True)
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    if os.path.exists(control):
        print("admission stop observed", flush=True)
        raise SystemExit(0)
    time.sleep(0.02)
raise SystemExit(3)
"""

DRAIN_IGNORES_STOP = """
import signal, subprocess, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
print(grandchild.pid, flush=True)
time.sleep(600)
"""


class AdmissionStopRealProcessTests(unittest.TestCase):
    """The drain over an actual child process: real signals, real exit status, real group."""

    def child(self, root, program, *arguments):
        child = driver.Child([sys.executable, "-B", "-c", program, *arguments],
                             output=root, label="campaign")
        self.addCleanup(child.close_files)

        def reap():
            if child.poll() is None:
                child.kill_group()

        self.addCleanup(reap)
        return child

    def test_a_real_child_drains_on_the_shared_stop_and_is_never_signalled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control, marker = root / "campaign-admission.STOP", root / "signals.txt"
            child = self.child(root, DRAIN_OBSERVES_STOP, str(marker), str(control))
            # It must already be running, so the drain is a stop and not a race with startup.
            for _ in range(500):
                if child.stdout_path.read_bytes().startswith(b"working"):
                    break
                time.sleep(0.01)
            self.assertIsNone(child.poll(), "the child must still be running when the drain starts")

            evidence = {}
            code = driver.drain_campaign(child, admission_stop_file=control,
                                         deadline_monotonic=time.monotonic() + 20.0, evidence=evidence)

            self.assertEqual(code, 0, "the child ended on its own after seeing the control file")
            self.assertEqual(evidence["campaign_exit_code"], 0)
            self.assertTrue(evidence["admission_stop_requested"])
            self.assertNotIn("campaign_forced", evidence)
            self.assertFalse(child.interrupted or child.killed)
            self.assertFalse(marker.exists(), "no signal reached the child; the stop is the file")
            self.assertIn("admission stop observed", child.output_head()["stdout"])
            self.assertEqual(control.read_bytes(), STOP_BYTES)

    def test_a_real_child_that_ignores_the_stop_is_reaped_only_when_its_deadline_is_spent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "campaign-admission.STOP"
            child = self.child(root, DRAIN_IGNORES_STOP)
            for _ in range(500):
                if child.stdout_path.read_bytes().count(b"\n"):
                    break
                time.sleep(0.01)
            grandchild = int(child.stdout_path.read_bytes().split()[0])

            evidence = {}
            started = time.monotonic()
            code = driver.drain_campaign(child, admission_stop_file=control,
                                         deadline_monotonic=started + 0.5, evidence=evidence)
            waited = time.monotonic() - started

            self.assertTrue(evidence["admission_stop_requested"], "the stop is requested before any force")
            self.assertTrue(evidence["campaign_drain_deadline_exhausted"])
            self.assertTrue(evidence["campaign_forced"])
            self.assertFalse(child.interrupted, "a drain never interrupts; only the spent deadline forces")
            self.assertEqual(code, -signal.SIGKILL, "the exit status is the kernel's, not a claim")
            self.assertGreaterEqual(waited, 0.5, "the original deadline was waited out, not cut short")
            self.assertLess(waited, 20.0)
            # The whole process group went, not only the leader this test happens to hold.
            for _ in range(200):
                try:
                    os.kill(grandchild, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            with self.assertRaises(ProcessLookupError):
                os.kill(grandchild, 0)
            self.assertEqual(evidence["campaign_exit_code"], -signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
