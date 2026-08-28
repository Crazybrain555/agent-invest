import io
import http.client
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorError,
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


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class MinerUOrchestratorTests(unittest.TestCase):
    def test_health_contract_is_strict_and_bounded(self) -> None:
        opener = Mock()
        opener.open.return_value = _Response(_payload())
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_orchestrator.urllib.request.build_opener",
            return_value=opener,
        ):
            health = fetch_mineru_orchestrator_health(
                "http://127.0.0.1:30002",
                expected_task_slots=3,
                expected_task_retention_seconds=600,
                expected_cleanup_interval_seconds=30,
            )
        self.assertEqual(health.active_tasks, 0)
        self.assertEqual(health.max_concurrent_requests, 3)

        opener.open.return_value = _Response(_payload(max_concurrent_requests=2))
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator.urllib.request.build_opener",
                return_value=opener,
            ),
            self.assertRaisesRegex(MinerUOrchestratorError, "task-slot limit drifted"),
        ):
            fetch_mineru_orchestrator_health(
                "http://127.0.0.1:30002",
                expected_task_slots=1,
            )

        for overrides in (
            {"version": "3.4.5"},
            {"protocol_version": 1},
            {"max_concurrent_requests": 4},
            {"processing_window_size": 64},
            {"queued_tasks": -1},
            {"processing_tasks": 4},
            {"queued_tasks": 14, "processing_tasks": 3},
            {"extra": 1},
        ):
            opener.open.return_value = _Response(_payload(**overrides))
            with (
                self.subTest(overrides=overrides),
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_orchestrator.urllib.request.build_opener",
                    return_value=opener,
                ),
                self.assertRaises(MinerUOrchestratorError),
            ):
                fetch_mineru_orchestrator_health("http://127.0.0.1:30002")

    def test_wait_for_idle_observes_natural_drain(self) -> None:
        health_payloads = [
            _payload(queued_tasks=2, processing_tasks=3),
            _payload(queued_tasks=0, processing_tasks=1),
            _payload(queued_tasks=0, processing_tasks=0),
        ]
        opener = Mock()
        opener.open.side_effect = [_Response(item) for item in health_payloads]
        ticks = iter((0.0, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5))
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator.urllib.request.build_opener",
                return_value=opener,
            ),
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator.time.monotonic",
                side_effect=lambda: next(ticks),
            ),
            patch("disclosure_anchor.adapters.runtime.mineru_orchestrator.time.sleep"),
        ):
            health, _duration = wait_for_mineru_orchestrator_idle(
                "http://127.0.0.1:30002",
                timeout_seconds=10,
            )
        self.assertEqual(health.active_tasks, 0)
        self.assertEqual(opener.open.call_count, 3)

    def test_wait_for_idle_retries_transport_until_processing_then_idle(self) -> None:
        processing = Mock(active_tasks=1)
        idle = Mock(active_tasks=0)
        fetch = Mock(
            side_effect=(
                MinerUOrchestratorUnavailableError("temporary route loss"),
                processing,
                idle,
            )
        )
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator.fetch_mineru_orchestrator_health",
                fetch,
            ),
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator.time.sleep"
            ),
        ):
            health, _duration = wait_for_mineru_orchestrator_idle(
                "http://127.0.0.1:30002",
                timeout_seconds=10,
                poll_seconds=0.01,
            )

        self.assertIs(health, idle)
        self.assertEqual(fetch.call_count, 3)

    def test_wait_for_idle_permanent_transport_loss_exhausts_one_deadline(
        self,
    ) -> None:
        fetch = Mock(
            side_effect=MinerUOrchestratorUnavailableError("route unavailable")
        )
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator.fetch_mineru_orchestrator_health",
                fetch,
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
        self.assertGreater(fetch.call_count, 1)

    def test_wait_for_idle_does_not_retry_strict_contract_failure(self) -> None:
        fetch = Mock(side_effect=MinerUOrchestratorError("protocol drifted"))
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator.fetch_mineru_orchestrator_health",
                fetch,
            ),
            self.assertRaisesRegex(MinerUOrchestratorError, "protocol drifted"),
        ):
            wait_for_mineru_orchestrator_idle(
                "http://127.0.0.1:30002",
                timeout_seconds=10,
            )
        fetch.assert_called_once()

    def test_health_probe_classifies_http_statuses(self) -> None:
        opener = Mock()
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_orchestrator.urllib.request.build_opener",
            return_value=opener,
        ):
            for status, expected_error in (
                (404, MinerUOrchestratorError),
                (503, MinerUOrchestratorUnavailableError),
                (501, MinerUOrchestratorError),
            ):
                with self.subTest(status=status):
                    opener.open.side_effect = urllib.error.HTTPError(
                        "http://127.0.0.1:30002/health",
                        status,
                        "probe failed",
                        {},
                        None,
                    )
                    with self.assertRaises(expected_error):
                        fetch_mineru_orchestrator_health(
                            "http://127.0.0.1:30002"
                        )

    def test_health_probe_classifies_truncated_response_as_unavailable(self) -> None:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.side_effect = http.client.IncompleteRead(b"{", 100)
        opener = Mock()
        opener.open.return_value = response
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_orchestrator.urllib.request.build_opener",
                return_value=opener,
            ),
            self.assertRaises(MinerUOrchestratorUnavailableError),
        ):
            fetch_mineru_orchestrator_health("http://127.0.0.1:30002")


if __name__ == "__main__":
    unittest.main()
