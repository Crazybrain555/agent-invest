"""Independent closed health wire, conservation, and actual collector probes."""

import unittest
from copy import deepcopy

from disclosure_anchor.application.contracts.mineru_api_health import (
    parse_mineru_api_health,
    validate_mineru_api_health,
    validate_mineru_api_wire_health,
    validate_mineru_task_runtime,
)

from tests._mineru_admission_health_fixture import (
    DELETE,
    CollectorIO,
    canonical,
    changed,
    responsibility,
    wire_health,
)

NORMALIZED = {
    "status", "version", "protocol_version", "queued_tasks", "processing_tasks",
    "completed_tasks", "failed_tasks", "max_concurrent_requests",
    "max_pending_tasks_requested", "max_pending_tasks_effective",
    "processing_window_size", "task_retention_seconds", "task_cleanup_interval_seconds",
}


class AdmissionHealthContractTests(unittest.TestCase):
    def validate(self, value):
        return validate_mineru_api_wire_health(value, expected_task_slots=1)

    def reject(self, value):
        with self.assertRaises(ValueError):
            self.validate(value)

    def test_legacy_and_v2_keep_exact_normalized_thirteen_fields(self):
        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                raw = wire_health(legacy=legacy)
                expected = {key: raw[key] for key in NORMALIZED}
                before = deepcopy(raw)
                self.assertEqual(self.validate(raw), expected)
                self.assertEqual(parse_mineru_api_health(canonical(raw),
                                                       expected_task_slots=1), expected)
                self.assertEqual(raw, before)
                self.assertEqual(validate_mineru_api_health(expected,
                                                           expected_task_slots=1), expected)
                self.reject(expected)

    def test_upload_and_each_accepted_phase_conserve_nonterminal_count(self):
        for phase in ("ingress", "pending", "processing", "finalizing", "cleanup",
                      "unowned", "routeless"):
            with self.subTest(phase=phase):
                normalized = self.validate(responsibility(phase))
                self.assertEqual(normalized["queued_tasks"] + normalized["processing_tasks"], 1)
                self.assertEqual(normalized["queued_tasks"],
                                 0 if phase in {"processing", "finalizing"} else 1)

    def test_v2_does_not_require_pending_to_equal_physical_queue(self):
        value = responsibility("pending")
        value["task_admission"]["queue_depth"] = 0
        # Actual processor task may have just completed before its done callback.
        self.assertEqual(self.validate(value)["queued_tasks"], 1)
        idle = wire_health()
        idle["completed_tasks"] = 7
        idle["failed_tasks"] = 2
        idle["task_admission"]["scheduled_tasks"] = 1
        self.assertEqual(self.validate(idle)["completed_tasks"], 7)

    def test_runtime_versions_cannot_smuggle_or_omit_version_fields(self):
        for legacy in (False, True):
            runtime = wire_health(legacy=legacy)["task_protocol_runtime"]
            validate_mineru_task_runtime(runtime)
            for field in tuple(runtime):
                with self.subTest(legacy=legacy, missing=field):
                    bad = dict(runtime)
                    del bad[field]
                    with self.assertRaises(ValueError):
                        validate_mineru_task_runtime(bad)
            for field, value in (("schema", "mineru-task-runtime.v99"),
                                 ("enabled", 1), ("extra", 1)):
                with self.subTest(legacy=legacy, field=field):
                    with self.assertRaises(ValueError):
                        validate_mineru_task_runtime({**runtime, field: value})
        for field, value in (("registry_schema", "mineru-task-registry.v2"),
                             ("admission_scope", "pre_body")):
            with self.subTest(field=field):
                self.reject(changed(wire_health(), "task_protocol_runtime", field, value))
        legacy = wire_health(legacy=True)
        legacy["task_admission"] = wire_health()["task_admission"]
        self.reject(legacy)
        self.reject(changed(wire_health(), None, "task_admission", DELETE))

    def test_admission_exact_shape_and_scalars_do_not_coerce(self):
        raw = wire_health()
        for field in tuple(raw["task_admission"]):
            with self.subTest(missing=field):
                self.reject(changed(raw, "task_admission", field, DELETE))
        self.reject(changed(raw, "task_admission", "extra", 0))
        for field in ("schema", "registry_schema"):
            self.reject(changed(raw, "task_admission", field, "unknown"))
        counts = set(raw["task_admission"]) - {
            "schema", "registry_schema", "admission_open", "blocked_reason",
            "recovery_overcommitted",
        }
        for field in counts:
            for invalid in (True, False, -1, 0.0, "0", None):
                with self.subTest(field=field, invalid=invalid):
                    self.reject(changed(raw, "task_admission", field, invalid))
        for field in ("admission_open", "recovery_overcommitted"):
            for invalid in (0, 1, "false", None):
                with self.subTest(field=field, invalid=invalid):
                    self.reject(changed(raw, "task_admission", field, invalid))

    def test_counts_crossbind_normalized_totals_and_subsets(self):
        for section, field, value in (
            (None, "queued_tasks", 0),
            (None, "processing_tasks", 1),
            ("task_admission", "durable_nonterminal_tasks", 0),
            ("task_admission", "accepted_pending_tasks", 1),
            ("task_admission", "ingress_cleanup_tasks", 2),
            ("task_admission", "unowned_ingress_tasks", 2),
            ("task_admission", "routeless_accepted_tasks", 1),
            ("task_admission", "nonterminal_limit", 2),
        ):
            with self.subTest(section=section, field=field):
                self.reject(changed(responsibility("ingress"), section, field, value))
        for field, value in (("scheduled_tasks", 2), ("queue_depth", 2),
                             ("active_processors", 1)):
            with self.subTest(field=field):
                self.reject(changed(responsibility("pending"), "task_admission", field, value))

    def test_blocked_reason_order_open_flag_and_recovery_flag_are_bound(self):
        for reason in ("shutting_down", "worker_unavailable"):
            raw = wire_health()
            raw["task_admission"].update(admission_open=False, blocked_reason=reason)
            self.validate(raw)
            raw = responsibility("cleanup")
            raw["task_admission"]["blocked_reason"] = reason
            self.validate(raw)
        for raw in (wire_health(), responsibility("ingress"), responsibility("cleanup"),
                    responsibility("unowned"), responsibility("routeless")):
            self.reject(changed(raw, "task_admission", "admission_open",
                                not raw["task_admission"]["admission_open"]))
            self.reject(changed(raw, "task_admission", "recovery_overcommitted", True))
            for reason in ("typo", "", 0, False):
                with self.subTest(reason=reason, raw=raw["task_admission"]["blocked_reason"]):
                    self.reject(changed(raw, "task_admission", "blocked_reason", reason))
        self.reject(changed(responsibility("cleanup"), "task_admission",
                            "blocked_reason", "capacity_full"))
        self.reject(changed(responsibility("routeless"), "task_admission",
                            "blocked_reason", "capacity_full"))
        self.reject(changed(wire_health(), "task_admission", "blocked_reason", "capacity_full"))

    def test_overcommitted_diagnostic_is_never_qualified_as_healthy_idle(self):
        raw = responsibility("pending")
        raw["status"] = "recovering"
        raw["queued_tasks"] = 2
        raw["task_admission"].update(
            accepted_pending_tasks=2, durable_nonterminal_tasks=2,
            recovery_overcommitted=True, blocked_reason="recovery_overcommitted",
        )
        self.reject(raw)
        raw["status"] = "healthy"
        self.reject(raw)

    def test_new_fields_are_strict_json_and_closed_before_projection(self):
        raw = canonical(wire_health())
        for bad in (
            raw.replace(b'"ingress_tasks":0', b'"ingress_tasks":0,"ingress_tasks":1'),
            raw.replace(b'"admission_open":true', b'"admission_open":true,"admission_open":false'),
            raw.replace(b'"blocked_reason":null', b'"blocked_reason":NaN'),
            raw + b'\xff',
        ):
            with self.subTest(raw=bad[:80]):
                with self.assertRaises(ValueError):
                    parse_mineru_api_health(bad, expected_task_slots=1)


class AdmissionCollectorTests(unittest.TestCase):
    def test_full_candidate_collector_probe_requires_admission_runtime(self):
        for value in (wire_health(), responsibility("ingress"),
                      responsibility("finalizing")):
            with self.subTest(runtime=value["task_protocol_runtime"]["schema"],
                              queued=value["queued_tasks"]):
                io = CollectorIO(value)
                result = io.run()
                self.assertIs(result["task_protocol_v2_enabled"], True)
                self.assertEqual(result["task_registry_max_records"], 128)
                self.assertEqual(result["task_result_reservation_bytes"], 268435456)
                self.assertEqual(result["max_unacked_result_bytes"], 2147483648)
                self.assertEqual(io.open_calls, [("http://127.0.0.1:8000/health", 10)])
                self.assertEqual(io.read_sizes, [65537])
                self.assertTrue(io.response_closed)
                self.assertCountEqual(io.file_reads, [
                    "/usr/local/lib/python3.12/dist-packages/" + relative
                    for relative in (
                        "mineru/cli/api_request.py", "mineru/cli/fast_api.py",
                        "mineru/backend/vlm/vlm_analyze.py",
                        "mineru/backend/hybrid/hybrid_analyze.py",
                        "mineru/utils/model_utils.py",
                        "mineru_vl_utils/post_process/__init__.py",
                        "mineru_vl_utils/post_process/cross_page_table.py",
                        "mineru_vl_utils/vlm_client/http_client.py",
                        "mineru/cli/agent_task_protocol_v2.py",
                    )
                ])
        # Candidate qualification uses its new collector. Old deployment reads
        # retain the old pinned collector; this is not an observer fallback.
        io = CollectorIO(wire_health(legacy=True))
        with self.assertRaises(RuntimeError):
            io.run()
        self.assertTrue(io.response_closed)

    def test_collector_does_not_discard_malformed_v2_or_count_contradiction(self):
        cases = [
            changed(wire_health(), None, "task_admission", DELETE),
            changed(wire_health(), "task_admission", "extra", 0),
            changed(wire_health(), "task_admission", "ingress_tasks", True),
            changed(responsibility("ingress"), None, "queued_tasks", 0),
            changed(wire_health(), "task_protocol_runtime", "registry_schema", "v2"),
            changed(wire_health(), "task_admission", "admission_open", False),
        ]
        for case in cases:
            with self.subTest(case=case):
                io = CollectorIO(case)
                with self.assertRaises((RuntimeError, ValueError)):
                    io.run()
                self.assertTrue(io.response_closed)

    def test_collector_keeps_bounded_http_read_and_rejects_original_bad_bytes(self):
        for raw in (b" " * 65537, b"\xff", b"{", b"[]"):
            with self.subTest(raw=raw[:10]):
                io = CollectorIO(raw)
                with self.assertRaises((RuntimeError, ValueError, TypeError)):
                    io.run()
                self.assertEqual(io.read_sizes, [65537])
                self.assertTrue(io.response_closed)


if __name__ == "__main__":
    unittest.main()
