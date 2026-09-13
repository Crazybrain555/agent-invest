"""Independent complete wire and external capacity authority tests; no live health."""

from copy import deepcopy
import hashlib
import json
import unittest

from disclosure_anchor.application.contracts.mineru_api_health import (
    parse_mineru_api_health,
    validate_mineru_api_health,
    validate_mineru_api_wire_health,
)
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    MineruCapacityConfig,
)
from disclosure_anchor.application.contracts.mineru_capacity_health import (
    parse_mineru_capacity_wire_health,
    validate_mineru_capacity_wire_health,
)
from tests._mineru_capacity_config_fixture import canonical_payload, capacity_payload
from tests._mineru_capacity_health_fixture import capacity_health_payload


OBS = "capacity_observation"
BASE_KEYS = {
    "status",
    "version",
    "protocol_version",
    "queued_tasks",
    "processing_tasks",
    "completed_tasks",
    "failed_tasks",
    "max_concurrent_requests",
    "max_pending_tasks_requested",
    "max_pending_tasks_effective",
    "processing_window_size",
    "task_retention_seconds",
    "task_cleanup_interval_seconds",
}


def at(value, path):
    for key in path:
        value = value[key]
    return value


class MineruCapacityHealthTests(unittest.TestCase):
    def setUp(self):
        self.config = MineruCapacityConfig(**capacity_payload())

    def validate(self, wire):
        return validate_mineru_capacity_wire_health(wire, expected_capacity=self.config)

    def test_complete_wire_and_nested_evidence_survive_both_entries_as_independent_copies(
        self,
    ):
        wire = capacity_health_payload()
        before = deepcopy(wire)
        validated = self.validate(wire)
        parsed = parse_mineru_capacity_wire_health(
            canonical_payload(wire), expected_capacity=self.config
        )
        self.assertEqual(
            set(validated),
            BASE_KEYS
            | {"task_protocol_schema", "task_protocol_runtime", "task_admission", OBS},
        )
        self.assertEqual(validated, before)
        self.assertEqual(parsed, before)
        self.assertIsNot(validated, wire)
        self.assertIsNot(validated[OBS], wire[OBS])
        validated[OBS]["framework_limits"]["torch_intraop_threads"]["value"] = 99
        parsed[OBS]["stage_counters"]["parse_active"] = 99
        wire["task_admission"]["blocked_reason"] = "changed input"
        self.assertEqual(validated["task_admission"], before["task_admission"])
        self.assertEqual(wire[OBS], before[OBS])
        self.assertEqual(
            parsed[OBS]["framework_limits"], before[OBS]["framework_limits"]
        )

    def test_multiple_external_capacity_values_keep_same_wire_schema_and_do_not_reapply_serial_window_limits(
        self,
    ):
        variants = (
            dict(
                parse_active_limit=1,
                total_nonterminal_limit=3,
                finalizer_active_limit=2,
                final_http_limit_per_loop=1,
                processing_window_size=1,
                omp_num_threads=2,
                mkl_num_threads=3,
                openblas_num_threads=4,
                pdf_render_processes_requested=8,
                hybrid_batch_ratio_requested=1,
                result_reservation_bytes=5,
                max_unacked_result_bytes=5,
            ),
            dict(
                parse_active_limit=3,
                total_nonterminal_limit=7,
                finalizer_active_limit=4,
                final_http_limit_per_loop=128,
                processing_window_size=1024,
                omp_num_threads=8,
                mkl_num_threads=6,
                openblas_num_threads=2,
                pdf_render_processes_requested=4,
                hybrid_batch_ratio_requested=4,
                result_reservation_bytes=10,
                max_unacked_result_bytes=17,
            ),
        )
        for changes in variants:
            with self.subTest(changes=changes):
                payload = capacity_payload(**changes)
                expected = MineruCapacityConfig(**payload)
                digest = (
                    "sha256:" + hashlib.sha256(canonical_payload(payload)).hexdigest()
                )
                wire = capacity_health_payload()
                wire.update(
                    max_concurrent_requests=changes["parse_active_limit"],
                    max_pending_tasks_requested=changes["total_nonterminal_limit"],
                    max_pending_tasks_effective=changes["total_nonterminal_limit"],
                    processing_window_size=changes["processing_window_size"],
                )
                runtime = wire["task_protocol_runtime"]
                runtime.update(
                    capacity_config_sha256=digest,
                    task_result_reservation_bytes=changes["result_reservation_bytes"],
                    max_unacked_result_bytes=changes["max_unacked_result_bytes"],
                )
                admission = wire["task_admission"]
                admission["nonterminal_limit"] = changes["total_nonterminal_limit"]
                admission["admission_open"] = changes["total_nonterminal_limit"] > 3
                admission["blocked_reason"] = (
                    None if admission["admission_open"] else "capacity_full"
                )
                observation = wire[OBS]
                observation["capacity_config_sha256"] = digest
                for field in observation["resolved_limits"]:
                    observation["resolved_limits"][field] = changes[field]
                observation["http_counters"] = {
                    "active_requests": 1,
                    "pending_requests": 99,
                }
                self.assertEqual(
                    validate_mineru_capacity_wire_health(
                        wire, expected_capacity=expected
                    ),
                    wire,
                )
                self.assertGreater(
                    changes["total_nonterminal_limit"]
                    * changes["result_reservation_bytes"],
                    changes["max_unacked_result_bytes"],
                )
                self.assertEqual(
                    wire[OBS]["framework_limits"]["torch_intraop_threads"]["value"], 6
                )

    def test_self_consistent_claims_cannot_replace_external_exact_capacity_authority(
        self,
    ):
        wire = capacity_health_payload()
        other = MineruCapacityConfig(**capacity_payload(omp_num_threads=8))
        with self.assertRaises(ValueError):
            validate_mineru_capacity_wire_health(wire, expected_capacity=other)
        wire["task_protocol_runtime"]["capacity_config_sha256"] = other.sha256
        wire[OBS]["capacity_config_sha256"] = other.sha256
        with self.assertRaises(ValueError):
            self.validate(wire)
        self.assertEqual(
            validate_mineru_capacity_wire_health(wire, expected_capacity=other), wire
        )

        class Derived(MineruCapacityConfig):
            pass

        for authority in (None, capacity_payload(), Derived(**capacity_payload())):
            with (
                self.subTest(type=type(authority).__name__),
                self.assertRaises(ValueError),
            ):
                validate_mineru_capacity_wire_health(
                    capacity_health_payload(), expected_capacity=authority
                )
        for branch in ("task_protocol_runtime", OBS):
            wire = capacity_health_payload()
            wire[branch]["capacity_config_sha256"] = "sha256:" + "0" * 64
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                self.validate(wire)

    def test_every_nested_contract_mapping_is_closed_and_missing_fields_are_not_defaulted(
        self,
    ):
        paths = (
            (),
            ("task_protocol_runtime",),
            ("task_admission",),
            (OBS,),
            (OBS, "owner"),
            (OBS, "resolved_limits"),
            (OBS, "stage_counters"),
            (OBS, "http_counters"),
            (OBS, "owner_control"),
            (OBS, "observed_at"),
            (OBS, "framework_limits"),
            (OBS, "framework_limits", "torch_intraop_threads"),
        )
        for path in paths:
            for mode in ("extra", "missing"):
                wire = capacity_health_payload()
                target = at(wire, path)
                if mode == "extra":
                    target["invented_observed_default"] = 1
                else:
                    del target[next(iter(target))]
                with self.subTest(path=path, mode=mode), self.assertRaises(ValueError):
                    self.validate(wire)

    def test_exact_integer_boolean_owner_clock_and_identity_boundaries_are_enforced(
        self,
    ):
        invalid = (
            (("queued_tasks",), True),
            (("protocol_version",), 2.0),
            (("processing_tasks",), 4),
            (("task_protocol_runtime", "enabled"), 1),
            (("task_protocol_runtime", "schema"), "mineru-task-runtime.v2"),
            (("task_admission", "admission_open"), 0),
            (("task_admission", "active_processors"), True),
            ((OBS, "owner", "process_id"), 0),
            ((OBS, "owner", "process_start_ticks"), True),
            ((OBS, "owner", "boot_id"), "11111111222243338444555555555555"),
            ((OBS, "owner", "loop_epoch"), "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"),
            ((OBS, "observed_at", "clock"), "wall_clock"),
            ((OBS, "observed_at", "implementation"), ""),
            ((OBS, "observed_at", "started_ns"), -1),
            ((OBS, "observed_at", "completed_ns"), 99),
            ((OBS, "resolved_limits", "parse_active_limit"), True),
            ((OBS, "resolved_limits", "result_reservation_bytes"), 30),
            ((OBS, "http_counters", "active_requests"), 8),
            ((OBS, "http_counters", "pending_requests"), -1),
            ((OBS, "stage_counters", "parse_active"), True),
        )
        for path, value in invalid:
            wire = capacity_health_payload()
            at(wire, path[:-1])[path[-1]] = value
            with self.subTest(path=path, value=value), self.assertRaises(ValueError):
                self.validate(wire)
        wire = capacity_health_payload()
        wire[OBS]["observed_at"]["completed_ns"] = 100
        self.assertEqual(self.validate(wire), wire)

    def test_disjoint_stage_ownership_is_bounded_by_durable_categories_without_forcing_equality(
        self,
    ):
        invalid = (
            {
                "result_capacity_waiting": 1,
                "parse_active": 1,
                "finalizer_waiting": 1,
                "finalizer_active": 1,
            },
            {"parse_waiting": 1},
            {"parse_active": 2},
            {"finalizer_waiting": 3},
            {"parse_active": 3},
            {"finalizer_active": 2},
            {"result_capacity_waiting": -1},
        )
        for counts in invalid:
            wire = capacity_health_payload()
            wire[OBS]["stage_counters"] = {
                key: counts.get(key, 0) for key in wire[OBS]["stage_counters"]
            }
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                self.validate(wire)
        wire = capacity_health_payload()
        wire[OBS]["stage_counters"] = dict.fromkeys(wire[OBS]["stage_counters"], 0)
        self.assertEqual(self.validate(wire), wire)
        wire = capacity_health_payload()
        wire.update(queued_tasks=1, processing_tasks=2)
        wire["task_admission"].update(
            accepted_pending_tasks=1, accepted_finalizing_tasks=1
        )
        wire[OBS]["stage_counters"].update(
            result_capacity_waiting=1, finalizer_waiting=0
        )
        self.assertEqual(self.validate(wire), wire)

    def test_uninitialized_http_and_unavailable_frameworks_remain_distinct_from_resolved_values(
        self,
    ):
        wire = capacity_health_payload()
        observation = wire[OBS]
        observation["http_limiter_state"] = "not_initialized"
        observation["resolved_limits"]["final_http_limit_per_loop"] = None
        observation["http_counters"] = {"active_requests": 0, "pending_requests": 0}
        observation["framework_limits"]["torch_intraop_threads"] = {
            "state": "unavailable",
            "value": None,
            "reason": "serving_getter_not_loaded",
        }
        observation["framework_limits"]["pdf_render_pool_max_workers"] = {
            "state": "unavailable",
            "value": None,
            "reason": "serving_pool_not_initialized",
        }
        self.assertEqual(self.validate(wire), wire)
        for reason in ("serving_pool_not_initialized", "serving_pool_lock_busy"):
            observation["framework_limits"]["pdf_render_pool_max_workers"]["reason"] = (
                reason
            )
            self.assertEqual(self.validate(wire), wire)
        negatives = (
            (("resolved_limits", "final_http_limit_per_loop"), 7),
            (("http_counters", "pending_requests"), 1),
            (("framework_limits", "torch_intraop_threads", "value"), 4),
            (
                ("framework_limits", "pdf_render_pool_max_workers", "reason"),
                ["serving_pool_lock_busy"],
            ),
            (
                ("framework_limits", "openblas_threads"),
                {"state": "available", "value": 1, "reason": None},
            ),
            (("framework_limits", "mkl_threads", "reason"), "configured_default"),
        )
        for path, value in negatives:
            bad = deepcopy(wire)
            at(bad[OBS], path[:-1])[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate(bad)
        initialized = capacity_health_payload()
        initialized[OBS]["resolved_limits"]["final_http_limit_per_loop"] = None
        with self.assertRaises(ValueError):
            self.validate(initialized)

    def test_foreign_loop_requested_drain_is_not_falsely_reported_applied(self):
        pending = capacity_health_payload()
        pending[OBS]["owner_control"].update(
            foreign_loop_observed=True,
            soft_drain_requested=True,
            trigger="foreign_event_loop",
        )
        self.assertEqual(self.validate(pending), pending)
        applied = deepcopy(pending)
        applied[OBS]["owner_control"]["soft_drain_applied"] = True
        with self.assertRaises(ValueError):
            self.validate(applied)
        applied["task_admission"]["blocked_reason"] = "shutting_down"
        self.assertEqual(self.validate(applied), applied)
        for key, value in (
            ("foreign_loop_observed", False),
            ("soft_drain_requested", False),
            ("soft_drain_applied", 1),
            ("trigger", None),
        ):
            bad = deepcopy(applied)
            bad[OBS]["owner_control"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.validate(bad)

    def test_raw_parser_has_finite_strict_json_boundary_without_requiring_canonical_wire(
        self,
    ):
        wire = capacity_health_payload()
        raw = canonical_payload(wire)
        self.assertEqual(
            parse_mineru_capacity_wire_health(
                json.dumps(wire, indent=2).encode(), expected_capacity=self.config
            ),
            wire,
        )
        exact = raw + b" " * (65536 - len(raw))
        self.assertEqual(
            parse_mineru_capacity_wire_health(exact, expected_capacity=self.config),
            wire,
        )
        duplicate = raw.replace(
            b'"process_id":123', b'"process_id":123,"process_id":123'
        )
        nonfinite = raw.replace(b'"pending_requests":5', b'"pending_requests":NaN')
        self.assertNotEqual(duplicate, raw)
        self.assertNotEqual(nonfinite, raw)
        for bad in (
            b"",
            b"\xff",
            bytearray(raw),
            raw.decode(),
            exact + b" ",
            duplicate,
            nonfinite,
        ):
            with (
                self.subTest(type=type(bad).__name__, size=len(bad)),
                self.assertRaises(ValueError),
            ):
                parse_mineru_capacity_wire_health(bad, expected_capacity=self.config)

    def test_legacy_runtime_v1_v2_remain_strict_serial_and_do_not_accept_new_evidence_family(
        self,
    ):
        for version in ("mineru-task-runtime.v1", "mineru-task-runtime.v2"):
            wire = capacity_health_payload()
            del wire[OBS]
            wire.update(
                queued_tasks=0,
                processing_tasks=0,
                max_concurrent_requests=1,
                max_pending_tasks_requested=1,
                max_pending_tasks_effective=1,
                processing_window_size=16,
            )
            wire["task_protocol_runtime"] = {
                "schema": version,
                "enabled": True,
                "task_registry_max_records": 128,
                "task_result_reservation_bytes": 268435456,
                "max_unacked_result_bytes": 2147483648,
            }
            if version.endswith("v1"):
                del wire["task_admission"]
            else:
                wire["task_protocol_runtime"].update(
                    registry_schema="mineru-task-registry.v3",
                    admission_scope="post_form_owned_upload",
                )
                admission = wire["task_admission"]
                for key in tuple(admission):
                    if type(admission[key]) is int:
                        admission[key] = 0
                admission.update(
                    nonterminal_limit=1, admission_open=True, blocked_reason=None
                )
            expected = {key: wire[key] for key in BASE_KEYS}
            self.assertEqual(
                validate_mineru_api_wire_health(wire, expected_task_slots=1), expected
            )
            self.assertEqual(
                parse_mineru_api_health(canonical_payload(wire), expected_task_slots=1),
                expected,
            )
            with self.assertRaises(ValueError):
                self.validate(wire)
            for changes in (
                {"processing_window_size": 1},
                {
                    "max_concurrent_requests": 2,
                    "max_pending_tasks_requested": 3,
                    "max_pending_tasks_effective": 3,
                },
                {"capacity_observation": capacity_health_payload()[OBS]},
            ):
                with (
                    self.subTest(version=version, changes=tuple(changes)),
                    self.assertRaises(ValueError),
                ):
                    validate_mineru_api_wire_health(
                        {**wire, **changes}, expected_task_slots=1
                    )
        with self.assertRaises(ValueError):
            validate_mineru_api_wire_health(
                capacity_health_payload(), expected_task_slots=None
            )
        with self.assertRaises(ValueError):
            validate_mineru_api_health(
                capacity_health_payload(), expected_task_slots=None
            )
