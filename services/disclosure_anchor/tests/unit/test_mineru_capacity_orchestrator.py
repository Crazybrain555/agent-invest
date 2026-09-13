"""Explicit capacity orchestrator branch through real decoders and bounded HTTP doubles."""

from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from disclosure_anchor.adapters.runtime import mineru_orchestrator as orchestrator
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    MineruCapacityConfig,
)
from tests._mineru_capacity_config_fixture import canonical_payload, capacity_payload
from tests._mineru_capacity_health_fixture import capacity_health_payload


URL = "http://127.0.0.1:9"
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


def legacy_wire():
    value = {
        key: item for key, item in capacity_health_payload().items() if key in BASE_KEYS
    }
    value.update(
        queued_tasks=0,
        processing_tasks=0,
        max_concurrent_requests=1,
        max_pending_tasks_requested=1,
        max_pending_tasks_effective=1,
    )
    value["task_protocol_schema"] = "mineru-task-protocol.v2"
    value["task_protocol_runtime"] = {
        "schema": "mineru-task-runtime.v1",
        "enabled": True,
        "task_registry_max_records": 128,
        "task_result_reservation_bytes": 268435456,
        "max_unacked_result_bytes": 2147483648,
    }
    return value


def idle_wire():
    value = capacity_health_payload()
    value.update(queued_tasks=0, processing_tasks=0)
    admission = value["task_admission"]
    for key in tuple(admission):
        if type(admission[key]) is int and key != "nonterminal_limit":
            admission[key] = 0
    admission.update(admission_open=True, blocked_reason=None)
    observation = value["capacity_observation"]
    observation["stage_counters"] = dict.fromkeys(observation["stage_counters"], 0)
    observation["http_counters"] = dict.fromkeys(observation["http_counters"], 0)
    return value


class MineruCapacityOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.config = MineruCapacityConfig(**capacity_payload())

    def transport(self, payload):
        result = Mock(spec_set=["get_bytes", "close"])
        result.get_bytes.return_value = (200, payload)
        return result

    def test_selected_fetch_and_decoded_entry_retain_all_seventeen_fields(self):
        wire = capacity_health_payload()
        transport = self.transport(canonical_payload(wire))
        with patch.object(
            orchestrator, "ThreadOwnedPersistentHTTPClient", return_value=transport
        ):
            result = orchestrator.fetch_mineru_orchestrator_health(
                URL, expected_task_slots=None, expected_capacity=self.config
            )
        self.assertIs(type(result), orchestrator.MinerUCapacityOrchestratorHealth)
        self.assertEqual(result.active_tasks, 3)
        self.assertEqual(result.as_dict(), wire)
        self.assertEqual(len(result.as_dict()), 17)
        self.assertEqual(
            result.capacity_observation["stage_counters"]["finalizer_waiting"], 1
        )
        transport.get_bytes.assert_called_once()
        transport.close.assert_called_once_with()
        decoded = orchestrator.parse_mineru_orchestrator_health_payload(
            wire, expected_task_slots=None, expected_capacity=self.config
        )
        self.assertIs(type(decoded), orchestrator.MinerUCapacityOrchestratorHealth)
        saved = deepcopy(wire)
        wire["capacity_observation"]["framework_limits"]["torch_intraop_threads"][
            "value"
        ] = 99
        self.assertEqual(decoded.as_dict(), saved)
        self.assertEqual(result.as_dict(), saved)

    def test_invalid_wire_legacy_under_new_authority_and_unselected_v3_all_refuse_without_fallback(
        self,
    ):
        bad = capacity_health_payload()
        bad["capacity_observation"]["owner_control"]["soft_drain_applied"] = True
        cases = (
            (b"not JSON", self.config),
            (canonical_payload(bad), self.config),
            (canonical_payload(legacy_wire()), self.config),
            (canonical_payload(capacity_health_payload()), None),
        )
        for raw, config in cases:
            transport = self.transport(raw)
            with (
                self.subTest(selected=config is not None, payload_size=len(raw)),
                patch.object(
                    orchestrator,
                    "ThreadOwnedPersistentHTTPClient",
                    return_value=transport,
                ),
                self.assertRaises(orchestrator.MinerUOrchestratorError) as caught,
            ):
                orchestrator.fetch_mineru_orchestrator_health(
                    URL, expected_task_slots=None, expected_capacity=config
                )
            self.assertIsInstance(caught.exception.__cause__, ValueError)
            transport.get_bytes.assert_called_once()
            transport.close.assert_called_once_with()
        for wire, config in (
            (bad, self.config),
            (legacy_wire(), self.config),
            (capacity_health_payload(), None),
        ):
            with (
                self.subTest(decoded=True, selected=config is not None),
                self.assertRaises(orchestrator.MinerUOrchestratorError),
            ):
                orchestrator.parse_mineru_orchestrator_health_payload(
                    wire, expected_task_slots=None, expected_capacity=config
                )

    def test_dual_authority_is_rejected_before_any_http_exchange_in_all_fetch_entrypoints(
        self,
    ):
        for kind in ("client", "fetch", "idle"):
            for slots in (1, 2):
                transport = self.transport(b"must not be decoded")
                transport.get_bytes.side_effect = AssertionError(
                    "authority conflict performed HTTP"
                )
                with (
                    self.subTest(kind=kind, slots=slots),
                    patch.object(
                        orchestrator,
                        "ThreadOwnedPersistentHTTPClient",
                        return_value=transport,
                    ),
                ):
                    with self.assertRaises(ValueError):
                        if kind == "client":
                            client = orchestrator.MinerUOrchestratorHealthClient(URL)
                            try:
                                client.fetch(
                                    expected_task_slots=slots,
                                    expected_capacity=self.config,
                                )
                            finally:
                                client.close()
                        elif kind == "fetch":
                            orchestrator.fetch_mineru_orchestrator_health(
                                URL,
                                expected_task_slots=slots,
                                expected_capacity=self.config,
                            )
                        else:
                            orchestrator.wait_for_mineru_orchestrator_idle(
                                URL,
                                timeout_seconds=1,
                                expected_task_slots=slots,
                                expected_capacity=self.config,
                            )
                    transport.get_bytes.assert_not_called()
                    transport.close.assert_called_once_with()
        with self.assertRaises(ValueError):
            orchestrator.parse_mineru_orchestrator_health_payload(
                {}, expected_task_slots=2, expected_capacity=self.config
            )

    def test_explicit_retention_and_cleanup_reach_fetch_decoded_and_idle_contracts(
        self,
    ):
        for kind in ("fetch", "decoded", "idle"):
            wire = idle_wire() if kind == "idle" else capacity_health_payload()
            wire.update(task_retention_seconds=901, task_cleanup_interval_seconds=47)
            transport = self.transport(canonical_payload(wire))
            kwargs = {
                "expected_task_slots": None,
                "expected_capacity": self.config,
                "expected_task_retention_seconds": 901,
                "expected_cleanup_interval_seconds": 47,
            }
            with (
                self.subTest(kind=kind),
                patch.object(
                    orchestrator,
                    "ThreadOwnedPersistentHTTPClient",
                    return_value=transport,
                ),
            ):
                if kind == "decoded":
                    result = orchestrator.parse_mineru_orchestrator_health_payload(
                        wire, **kwargs
                    )
                    transport.get_bytes.assert_not_called()
                elif kind == "idle":
                    result, duration = orchestrator.wait_for_mineru_orchestrator_idle(
                        URL, timeout_seconds=1, **kwargs
                    )
                    self.assertEqual(result.active_tasks, 0)
                    self.assertGreaterEqual(duration, 0)
                else:
                    result = orchestrator.fetch_mineru_orchestrator_health(
                        URL, **kwargs
                    )
                self.assertEqual(result.as_dict(), wire)
                if kind != "decoded":
                    transport.get_bytes.assert_called_once()
                    transport.close.assert_called_once_with()
        for field in (
            "expected_task_retention_seconds",
            "expected_cleanup_interval_seconds",
        ):
            for invalid in (None, True, 999):
                wire = capacity_health_payload()
                options = {
                    "expected_task_slots": None,
                    "expected_capacity": self.config,
                    field: invalid,
                }
                transport = self.transport(canonical_payload(wire))
                with (
                    self.subTest(field=field, invalid=invalid),
                    patch.object(
                        orchestrator,
                        "ThreadOwnedPersistentHTTPClient",
                        return_value=transport,
                    ),
                ):
                    with self.assertRaises(
                        orchestrator.MinerUOrchestratorError
                    ) as caught:
                        orchestrator.fetch_mineru_orchestrator_health(URL, **options)
                    self.assertIsInstance(caught.exception.__cause__, ValueError)
                    transport.close.assert_called_once_with()
                    with self.assertRaises(orchestrator.MinerUOrchestratorError):
                        orchestrator.parse_mineru_orchestrator_health_payload(
                            wire, **options
                        )

    def test_unselected_legacy_keeps_original_result_and_wrapper_keyword_call_shape(
        self,
    ):
        wire = legacy_wire()
        transport = self.transport(canonical_payload(wire))
        with patch.object(
            orchestrator, "ThreadOwnedPersistentHTTPClient", return_value=transport
        ):
            result = orchestrator.fetch_mineru_orchestrator_health(URL)
        self.assertIs(type(result), orchestrator.MinerUOrchestratorHealth)
        self.assertEqual(result.as_dict(), {key: wire[key] for key in BASE_KEYS})
        self.assertEqual(len(result.as_dict()), 13)
        transport.close.assert_called_once_with()
        seen = []

        def old_signature_fetch(
            *,
            timeout_seconds,
            expected_task_slots,
            expected_task_retention_seconds,
            expected_cleanup_interval_seconds,
        ):
            seen.append(
                (
                    timeout_seconds,
                    expected_task_slots,
                    expected_task_retention_seconds,
                    expected_cleanup_interval_seconds,
                )
            )
            return result

        client = Mock(spec_set=["fetch", "close"])
        client.fetch.side_effect = old_signature_fetch
        with patch.object(
            orchestrator, "MinerUOrchestratorHealthClient", return_value=client
        ):
            self.assertIs(orchestrator.fetch_mineru_orchestrator_health(URL), result)
            idle, _duration = orchestrator.wait_for_mineru_orchestrator_idle(
                URL, timeout_seconds=1
            )
        self.assertIs(idle, result)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], (15.0, 1, 600, 30))
        self.assertEqual(seen[1][1:], (1, 600, 30))
        self.assertEqual(client.close.call_count, 2)
