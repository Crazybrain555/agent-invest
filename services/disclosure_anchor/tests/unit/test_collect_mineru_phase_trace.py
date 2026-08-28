"""Regressions for staged-receipt-bound MinerU phase capture."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest

from scripts.collect_mineru_phase_trace import _receipt_identity


_COLLECTOR_SHA = "sha256:" + "a" * 64
_NODE_SHA = "sha256:" + "b" * 64
_API_ID = "c" * 64


def _api_container(container_id: str = _API_ID) -> dict[str, object]:
    return {
        "name": "mineru-api",
        "id": container_id,
        "restart_count": 0,
        "oom_killed": False,
        "running": True,
        "status": "running",
        "health": "healthy",
    }


def _receipt() -> dict[str, object]:
    return {
        "schema": "mineru_staged_load_receipt.v6",
        "receipt_schema_version": 6,
        "status": "pass",
        "failure": None,
        "database_access": "none",
        "queue_access": "none",
        "fixed_stage_document_counts": [4, 8, 16],
        "started_at_utc": "2026-08-27T01:00:00+00:00",
        "finished_at_utc": "2026-08-27T01:30:00+00:00",
        "host_capacity": {
            "status": "pass",
            "failure": None,
            "collector_sha256": _COLLECTOR_SHA,
            "windows_node_identity_sha256": _NODE_SHA,
            "violations": [],
            "sampling_failures": [],
            "samples": [
                {"containers": [_api_container()]},
                {"containers": [_api_container()]},
            ],
        },
        "stages": [
            {
                "status": "pass",
                "failure": None,
                "stage_document_count": count,
                "documents": [
                    {
                        "status": "pass",
                        "page_count": index,
                    }
                    for index in range(1, count + 1)
                ],
            }
            for count in (4, 8, 16)
        ],
    }


class CollectMineruPhaseTraceTests(unittest.TestCase):
    def test_make_target_uses_import_safe_module_entrypoint(self) -> None:
        makefile = Path("Makefile").read_text(encoding="utf-8")

        self.assertIn(
            "$(PYTHON) -m scripts.collect_mineru_phase_trace",
            makefile,
        )
        self.assertNotIn(
            "$(PYTHON) scripts/collect_mineru_phase_trace.py",
            makefile,
        )

    def test_receipt_identity_closes_epoch_and_document_page_counts(self) -> None:
        (
            started,
            finished,
            collector_sha,
            node_sha,
            api_id,
            document_count,
            page_count,
        ) = _receipt_identity(_receipt())

        self.assertLess(started, finished)
        self.assertEqual(collector_sha, _COLLECTOR_SHA)
        self.assertEqual(node_sha, _NODE_SHA)
        self.assertEqual(api_id, _API_ID)
        self.assertEqual(document_count, 28)
        self.assertEqual(page_count, 4 * 5 // 2 + 8 * 9 // 2 + 16 * 17 // 2)

    def test_receipt_identity_rejects_api_epoch_drift(self) -> None:
        receipt = deepcopy(_receipt())
        receipt["host_capacity"]["samples"][1]["containers"][0]["id"] = "d" * 64

        with self.assertRaisesRegex(ValueError, "epoch changed"):
            _receipt_identity(receipt)


if __name__ == "__main__":
    unittest.main()
