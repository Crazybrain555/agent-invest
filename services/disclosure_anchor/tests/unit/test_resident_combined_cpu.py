"""Disjoint-role cost arithmetic after canonical mechanism replay."""

from __future__ import annotations

from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.resident_combined_cpu import (
    check_combined_resident_cpu, check_resident_supervisor_started,
)
from disclosure_anchor.application.contracts.resident_session_evidence import artifact_sha256, canonical_bytes
from disclosure_anchor.application.contracts.synchronized_telemetry import SynchronizedTelemetrySealV3
from tests.unit.test_resident_session_evidence import _fixture, _mapping_fixture, _check_ready, _check_closure, _rebind_ready


def _gpu_fixture(pid=400, parent=399, port=30316):
    fixture = _fixture()
    session = "234567891234423482341234567890ab"
    fixture["config"].update(session=session, run_directory="C:\\fixture\\run-" + session, port=port)
    fixture["ready"].update(session=session, port=port)
    fixture["ready"]["process"].update(session=session, pid=pid)
    _rebind_ready(fixture)
    fixture["job"].update(session=session, config_sha256=fixture["ready"]["config_sha256"])
    fixture["job"]["supervisor_process"].update(session=session, pid=parent, config_sha256=fixture["ready"]["config_sha256"])
    fixture["job"]["job"].update(child_pid=pid, supervisor_pid=parent, job_instance="34567890-1234-4234-8234-1234567890ab")
    fixture["closed"].update(session=session, config_sha256=fixture["ready"]["config_sha256"], identity=fixture["ready"]["identity"])
    return fixture


def _inputs():
    gpu, host = _gpu_fixture(), _fixture(True)
    _, _, receipt = _mapping_fixture(True)
    gpu_ready, gpu_closure = _check_ready(gpu), _check_closure(gpu)
    seal = SynchronizedTelemetrySealV3(
        run_id=receipt.run_id, receipt_sha256=artifact_sha256(canonical_bytes(receipt.model_dump(mode="json"))),
        frames_jsonl_sha256=receipt.artifacts.frames_jsonl_sha256,
        preseal_observer_process_cpu_started_ns=100,
        preseal_observer_process_cpu_finished_ns=200,
        preseal_observer_cpu_ns=100, sampling_elapsed_ns_denominator=2_000_000_000,
        receipt_status=receipt.status, status=receipt.status,
    )
    return dict(gpu_ready=gpu_ready, host_ready=_check_ready(host), gpu_closure=gpu_closure,
                host_closure=_check_closure(host), receipt=receipt, seal=seal)


class ResidentCombinedCpuTests(unittest.TestCase):
    def test_disjoint_seven_roles_and_exact_integer_threshold(self):
        args = _inputs()
        result = check_combined_resident_cpu(**args)
        self.assertEqual(result.total_cpu_ns, 100 + 7 + 7 + 11 + 11 + 4 + 7)
        self.assertTrue(result.within_two_percent)
        limit = 2 * result.sampling_elapsed_ns // 100
        boundary = replace(result, gpu_job_ns=result.gpu_job_ns + limit - result.total_cpu_ns)
        self.assertTrue(boundary.within_two_percent)
        self.assertFalse(replace(boundary, gpu_job_ns=boundary.gpu_job_ns + 1).within_two_percent)

    def test_duplicate_jobs_processes_linux_and_denominator_are_not_accepted(self):
        args = _inputs()
        changes = (
            {"gpu_ready": replace(args["gpu_ready"], pid=args["host_ready"].pid)},
            {"gpu_closure": replace(args["gpu_closure"], job_instance=args["host_closure"].job_instance)},
            {"gpu_closure": replace(args["gpu_closure"], linux_sampler_exit_cpu_ns=1)},
            {"host_closure": replace(args["host_closure"], linux_sampler_exit_cpu_ns=None)},
            {"seal": args["seal"].model_copy(update={"sampling_elapsed_ns_denominator": 3_000_000_000})},
            {"seal": args["seal"].model_copy(update={"receipt_sha256": "sha256:" + "0" * 64})},
        )
        for change in changes:
            with self.subTest(change=tuple(change)), self.assertRaises(ValueError):
                check_combined_resident_cpu(**{**args, **change})

    def test_pre_job_marker_requires_same_exact_parent_not_a_new_launch(self):
        fixture = _fixture()
        started = {key: value for key, value in fixture["job"].items() if key != "job"}
        started["contract_version"] = "mineru.windows-resident-supervisor-started.v1"
        args = dict(ready=_check_ready(fixture), job_bytes=canonical_bytes(fixture["job"]))
        check_resident_supervisor_started(started_bytes=canonical_bytes(started), **args)
        started["supervisor_process"] = {**started["supervisor_process"], "creation_filetime_100ns": 134332345274349836}
        with self.assertRaisesRegex(ValueError, "marker binding"):
            check_resident_supervisor_started(started_bytes=canonical_bytes(started), **args)

    def test_same_session_different_replayed_launch_cannot_supply_cpu_cost(self):
        args = _inputs()
        alternate = _gpu_fixture(pid=401, parent=398, port=30318)
        alternate["job"]["job"].update(job_user_ns_total=1, job_system_ns_total=0)
        other_ready, other_closed = _check_ready(alternate), _check_closure(alternate)
        self.assertEqual(other_ready.session, args["gpu_ready"].session)
        self.assertNotEqual(other_closed.ready_sha256, args["gpu_closure"].ready_sha256)
        self.assertEqual(check_combined_resident_cpu(**{**args, "gpu_ready": other_ready, "gpu_closure": other_closed}).gpu_job_ns, 1)
        with self.assertRaisesRegex(ValueError, "config/READY binding"):
            check_combined_resident_cpu(**{**args, "gpu_closure": other_closed})
