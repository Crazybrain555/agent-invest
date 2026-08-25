import io
import json
import unittest
from unittest.mock import Mock, patch

from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorError,
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
                expected_task_retention_seconds=600,
                expected_cleanup_interval_seconds=30,
            )
        self.assertEqual(health.active_tasks, 0)
        self.assertEqual(health.max_concurrent_requests, 3)

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


if __name__ == "__main__":
    unittest.main()
