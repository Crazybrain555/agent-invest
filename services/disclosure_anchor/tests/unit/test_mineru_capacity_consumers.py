"""Actual full-wire consumer projection; deterministic transport seam only."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import capacity_sources as sources
from tests._mineru_capacity_config_fixture import canonical_payload
from tests._mineru_capacity_consumers_fixture import (
    configuration,
    legacy_health,
    overlap_health,
)


class MineruCapacityConsumerTests(unittest.TestCase):
    def test_overlap_and_full_responsibility_project_parse_owners_not_finalizers(self):
        for full in (False, True):
            with self.subTest(full=full):
                wire = overlap_health(full=full)
                self.assertEqual(wire["processing_tasks"], 3)
                sample = sources._api_values(
                    canonical_payload(wire),
                    expected_capacity=configuration(),
                )
                self.assertEqual(sample.processing_tasks, 2)
                self.assertEqual(sample.task_slots, 2)
                self.assertEqual(sample.queued_tasks, int(full))
                self.assertEqual(sample.max_pending_tasks_requested, 4)
                self.assertEqual(sample.max_pending_tasks_effective, 4)
                self.assertEqual(sample.completed_tasks_gauge, 1)
                self.assertEqual(sample.failed_tasks_gauge, 2)
        tail = overlap_health()
        tail["processing_tasks"] = 1
        tail["task_admission"].update(
            accepted_processing_tasks=0,
            durable_nonterminal_tasks=1,
            scheduled_tasks=1,
            active_processors=1,
        )
        tail["capacity_observation"]["stage_counters"]["parse_active"] = 0
        tail["capacity_observation"]["http_counters"] = {
            "active_requests": 0,
            "pending_requests": 0,
        }
        self.assertEqual(
            sources._api_values(
                canonical_payload(tail),
                expected_capacity=configuration(),
            ).processing_tasks,
            0,
        )

    def test_full_wire_and_external_configuration_are_checked_before_projection(self):
        original = overlap_health()
        cases = []
        for missing in (
            "capacity_observation",
            "task_admission",
            "task_protocol_runtime",
        ):
            value = deepcopy(original)
            del value[missing]
            cases.append(value)
        invalid_stage = deepcopy(original)
        invalid_stage["capacity_observation"]["stage_counters"]["parse_active"] = True
        cases.append(invalid_stage)
        invalid_owner = deepcopy(original)
        invalid_owner["capacity_observation"]["owner"]["process_id"] = 0
        cases.append(invalid_owner)
        invalid_extra = deepcopy(original)
        invalid_extra["task_protocol_runtime"]["unreviewed"] = 1
        cases.append(invalid_extra)
        cases.append(legacy_health())
        for index, value in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValueError):
                sources._api_values(
                    canonical_payload(value), expected_capacity=configuration()
                )
        with self.assertRaises(ValueError):
            sources._api_values(
                canonical_payload(original),
                expected_capacity=configuration(omp_num_threads=4),
            )

    def test_sampler_fetches_once_and_conflicting_authority_fails_before_fetch(self):
        wire = canonical_payload(overlap_health())
        with patch.object(sources, "_fetch_payload", return_value=wire) as fetch:
            sample = sources.MineruApiCapacitySampler(
                url="http://example.invalid:30002/",
                timeout_seconds=0.75,
                expected_capacity=configuration(),
            ).sample()
        self.assertEqual(sample.processing_tasks, 2)
        fetch.assert_called_once_with(
            "http://example.invalid:30002/health",
            timeout_seconds=0.75,
            accepted_content_types=frozenset({"application/json"}),
            maximum_bytes=65536,
        )
        with patch.object(
            sources, "_fetch_payload", side_effect=AssertionError("network")
        ) as fetch:
            with self.assertRaises(ValueError):
                sources.MineruApiCapacitySampler(
                    url="http://example.invalid",
                    timeout_seconds=1,
                    task_slots=1,
                    expected_capacity=configuration(),
                ).sample()
        fetch.assert_not_called()

    def test_explicit_unknown_is_not_fake_resolution_and_foreign_drain_is_refused(self):
        idle = overlap_health(idle=True)
        obs = idle["capacity_observation"]
        obs["http_limiter_state"] = "not_initialized"
        obs["resolved_limits"]["final_http_limit_per_loop"] = None
        obs["framework_limits"]["torch_intraop_threads"] = {
            "state": "unavailable",
            "value": None,
            "reason": "serving_getter_not_loaded",
        }
        obs["framework_limits"]["pdf_render_pool_max_workers"] = {
            "state": "unavailable",
            "value": None,
            "reason": "serving_pool_not_initialized",
        }
        before = deepcopy(idle)
        sample = sources._api_values(
            canonical_payload(idle), expected_capacity=configuration()
        )
        self.assertEqual(sample.processing_tasks, 0)
        self.assertEqual(idle, before)
        for applied in (False, True):
            value = deepcopy(idle)
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
            with self.subTest(applied=applied), self.assertRaises(ValueError):
                sources._api_values(
                    canonical_payload(value), expected_capacity=configuration()
                )
        idle["status"] = "unknown"
        with self.assertRaises(ValueError):
            sources._api_values(
                canonical_payload(idle), expected_capacity=configuration()
            )

    def test_legacy_default_and_explicit_one_remain_strict(self):
        wire = canonical_payload(legacy_health())
        default = sources._api_values(wire)
        explicit = sources._api_values(wire, expected_task_slots=1)
        self.assertEqual(default, explicit)
        self.assertEqual(default.processing_tasks, 1)
        self.assertEqual(default.task_slots, 1)
        with patch.object(sources, "_fetch_payload", return_value=wire):
            self.assertEqual(
                sources.MineruApiCapacitySampler(
                    url="http://example.invalid",
                    timeout_seconds=1,
                ).sample(),
                default,
            )
        with self.assertRaises(ValueError):
            sources._api_values(canonical_payload(overlap_health()))
        legacy = legacy_health()
        legacy["processing_tasks"] = 2
        with self.assertRaises(ValueError):
            sources._api_values(canonical_payload(legacy))
