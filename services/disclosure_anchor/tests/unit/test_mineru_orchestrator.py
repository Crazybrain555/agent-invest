import json
import unittest
from unittest.mock import Mock, patch

from disclosure_anchor.adapters.runtime.bounded_http import (
    BoundedHTTPTransportError,
)
from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorError,
    MinerUOrchestratorHealthClient,
    MinerUOrchestratorUnavailableError,
    fetch_mineru_orchestrator_health,
    wait_for_mineru_orchestrator_idle,
)


def _payload(**overrides: object) -> bytes:
    value = {
        "status": "healthy",
        "version": "3.4.4",
        "protocol_version": 2,
        "queued_tasks": 0,
        "processing_tasks": 0,
        "completed_tasks": 1,
        "failed_tasks": 0,
        "max_concurrent_requests": 3,
        "processing_window_size": 16,
        "task_retention_seconds": 600,
        "task_cleanup_interval_seconds": 30,
    }
    value.update(overrides)
    return json.dumps(value).encode("utf-8")


class MinerUOrchestratorTests(unittest.TestCase):
    @staticmethod
    def _transport(payload: bytes, *, status: int = 200) -> Mock:
        transport = Mock()
        transport.get_bytes.return_value = (status, payload)
        return transport

    def test_health_contract_is_strict_and_bounded(self) -> None:
        transport = self._transport(_payload())
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_orchestrator."
            "ThreadOwnedPersistentHTTPClient",
            return_value=transport,
        ):
            health = fetch_mineru_orchestrator_health(
                "http://127.0.0.1:30002",
                expected_task_slots=3,
                expected_task_retention_seconds=600,
                expected_cleanup_interval_seconds=30,
            )
        self.assertEqual(health.active_tasks, 0)
        self.assertEqual(health.max_concurrent_requests, 3)
        transport.close.assert_called_once()

        cases = (
            ({"max_concurrent_requests": 2}, "task-slot limit drifted"),
            ({"version": "3.4.5"}, None),
            ({"protocol_version": 1}, None),
            ({"max_concurrent_requests": 4}, None),
            ({"processing_window_size": 64}, None),
            ({"queued_tasks": -1}, None),
            ({"processing_tasks": 4}, None),
            ({"queued_tasks": 14, "processing_tasks": 3}, None),
            ({"extra": 1}, None),
        )
        for overrides, expected_message in cases:
            with self.subTest(overrides=overrides):
                transport = self._transport(_payload(**overrides))
                with (
                    patch(
                        "disclosure_anchor.adapters.runtime.mineru_orchestrator."
                        "ThreadOwnedPersistentHTTPClient",
                        return_value=transport,
                    ),
                    self.assertRaises(MinerUOrchestratorError) as raised,
                ):
                    fetch_mineru_orchestrator_health(
                        "http://127.0.0.1:30002",
                        expected_task_slots=(1 if expected_message else None),
                    )
                if expected_message:
                    self.assertIn(expected_message, str(raised.exception))

    def test_wait_for_idle_observes_natural_drain_on_one_client(self) -> None:
        client = Mock()
        client.fetch.side_effect = [
            Mock(active_tasks=5),
            Mock(active_tasks=1),
            Mock(active_tasks=0),
        ]
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator."
                "MinerUOrchestratorHealthClient",
                return_value=client,
            ),
            patch("disclosure_anchor.adapters.runtime.mineru_orchestrator.time.sleep"),
        ):
            health, _duration = wait_for_mineru_orchestrator_idle(
                "http://127.0.0.1:30002",
                timeout_seconds=10,
            )
        self.assertEqual(health.active_tasks, 0)
        self.assertEqual(client.fetch.call_count, 3)
        client.close.assert_called_once()

    def test_wait_for_idle_retries_transport_until_processing_then_idle(self) -> None:
        processing = Mock(active_tasks=1)
        idle = Mock(active_tasks=0)
        client = Mock()
        client.fetch.side_effect = (
            MinerUOrchestratorUnavailableError("temporary route loss"),
            processing,
            idle,
        )
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator."
                "MinerUOrchestratorHealthClient",
                return_value=client,
            ),
            patch("disclosure_anchor.adapters.runtime.mineru_orchestrator.time.sleep"),
        ):
            health, _duration = wait_for_mineru_orchestrator_idle(
                "http://127.0.0.1:30002",
                timeout_seconds=10,
                poll_seconds=0.01,
            )
        self.assertIs(health, idle)
        self.assertEqual(client.fetch.call_count, 3)

    def test_wait_for_idle_permanent_transport_loss_exhausts_one_deadline(
        self,
    ) -> None:
        client = Mock()
        client.fetch.side_effect = MinerUOrchestratorUnavailableError(
            "route unavailable"
        )
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator."
                "MinerUOrchestratorHealthClient",
                return_value=client,
            ),
            self.assertRaisesRegex(
                MinerUOrchestratorError,
                "transport-unproved|queued/processing drain",
            ),
        ):
            wait_for_mineru_orchestrator_idle(
                "http://127.0.0.1:30002",
                timeout_seconds=0.01,
                poll_seconds=0.001,
            )
        self.assertGreater(client.fetch.call_count, 1)

    def test_wait_for_idle_does_not_retry_strict_contract_failure(self) -> None:
        client = Mock()
        client.fetch.side_effect = MinerUOrchestratorError("protocol drifted")
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator."
                "MinerUOrchestratorHealthClient",
                return_value=client,
            ),
            self.assertRaisesRegex(MinerUOrchestratorError, "protocol drifted"),
        ):
            wait_for_mineru_orchestrator_idle(
                "http://127.0.0.1:30002",
                timeout_seconds=10,
            )
        client.fetch.assert_called_once()

    def test_health_probe_classifies_http_statuses(self) -> None:
        for status, expected_error in (
            (404, MinerUOrchestratorError),
            (503, MinerUOrchestratorUnavailableError),
            (501, MinerUOrchestratorError),
        ):
            with self.subTest(status=status):
                transport = self._transport(b"failure", status=status)
                with (
                    patch(
                        "disclosure_anchor.adapters.runtime.mineru_orchestrator."
                        "ThreadOwnedPersistentHTTPClient",
                        return_value=transport,
                    ),
                    self.assertRaises(expected_error),
                ):
                    MinerUOrchestratorHealthClient(
                        "http://127.0.0.1:30002"
                    ).fetch()

    def test_health_probe_classifies_truncated_response_as_unavailable(self) -> None:
        transport = Mock()
        transport.get_bytes.side_effect = BoundedHTTPTransportError("truncated")
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator."
                "ThreadOwnedPersistentHTTPClient",
                return_value=transport,
            ),
            self.assertRaises(MinerUOrchestratorUnavailableError),
        ):
            MinerUOrchestratorHealthClient("http://127.0.0.1:30002").fetch()

    def test_health_probe_never_hides_first_transport_failure(self) -> None:
        transport = Mock()
        transport.get_bytes.side_effect = (
            BoundedHTTPTransportError("first attempt failed"),
            (200, _payload()),
        )
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator."
                "ThreadOwnedPersistentHTTPClient",
                return_value=transport,
            ),
            self.assertRaises(MinerUOrchestratorUnavailableError),
        ):
            MinerUOrchestratorHealthClient("http://127.0.0.1:30002").fetch()
        transport.get_bytes.assert_called_once_with(
            "/health",
            timeout_seconds=15.0,
            transport_attempts=1,
            maximum_attempt_timeout_seconds=4.5,
        )


if __name__ == "__main__":
    unittest.main()
