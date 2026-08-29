"""Regressions for staged-receipt-bound MinerU phase capture."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from disclosure_anchor.adapters.runtime.mineru_canary import (
    canary_request_sha256,
)
from scripts.collect_mineru_phase_trace import _receipt_identity


_COLLECTOR_SHA = "sha256:" + "a" * 64
_NODE_SHA = "sha256:" + "b" * 64
_API_ID = "c" * 64
_PROXY_ID = "d" * 64
_INFERENCE_ID = "e" * 64
_STARTED_AT = "2026-08-27T00:00:00+00:00"
_MODEL_ID = "pinned-model"
_RUNTIME_IDENTITY = "sha256:" + "f" * 64
_OBSERVABILITY_SHA = "sha256:" + "1" * 64


def _container(name: str, container_id: str) -> dict[str, object]:
    return {
        "name": name,
        "id": container_id,
        "started_at_utc": _STARTED_AT,
        "restart_count": 0,
        "oom_killed": False,
        "running": True,
        "status": "running",
        "health": "healthy",
    }


def _receipt() -> dict[str, object]:
    services = {
        "proxy": {
            "name": "mineru-api-proxy",
            "container_id": _PROXY_ID,
            "started_at_utc": _STARTED_AT,
        },
        "inference": {
            "name": "mineru-openai-server",
            "container_id": _INFERENCE_ID,
            "started_at_utc": _STARTED_AT,
        },
    }
    canonical_epoch = {
        "schema": "mineru-campaign-service-epoch.v1",
        "windows_node_identity_sha256": _NODE_SHA,
        "collector_sha256": _COLLECTOR_SHA,
        "services": services,
    }
    campaign_sha256 = "sha256:" + hashlib.sha256(
        json.dumps(
            canonical_epoch,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    def boundary(
        *, phase: str, started: float, finished: float
    ) -> dict[str, object]:
        return {
            "schema": "mineru-arm-boundary-canary.v1",
            "phase": phase,
            "status": "pass",
            "started_at_utc": f"2026-08-27T01:{int(started // 60):02d}:{int(started % 60):02d}+00:00",
            "finished_at_utc": f"2026-08-27T01:{int(finished // 60):02d}:{int(finished % 60):02d}+00:00",
            "started_observed_seconds": started,
            "finished_observed_seconds": finished,
            "elapsed_seconds": finished - started,
            "model_id": _MODEL_ID,
            "attempts": 1,
            "observability_endpoint_sha256": _OBSERVABILITY_SHA,
            "request_sha256": "sha256:" + canary_request_sha256(_MODEL_ID),
            "response_sha256": ["sha256:" + "2" * 64],
            "runtime_manifest_identity_sha256": _RUNTIME_IDENTITY,
            "campaign_epoch_sha256": campaign_sha256,
            "inference_epoch": services["inference"],
            "failure": None,
        }

    return {
        "schema": "mineru_staged_load_receipt.v7",
        "receipt_schema_version": 7,
        "status": "pass",
        "failure": None,
        "secondary_failures": [],
        "database_access": "none",
        "queue_access": "none",
        "fixed_stage_document_counts": [4, 8, 16],
        "safety_limits": {
            "profile": "whole-document-runaway-and-drain.v1",
            "document_runaway_timeout_seconds": 86400,
            "api_drain_timeout_seconds": 86400,
        },
        "started_at_utc": "2026-08-27T01:00:00+00:00",
        "finished_at_utc": "2026-08-27T01:30:00+00:00",
        "topology": {"observability_endpoint_sha256": _OBSERVABILITY_SHA},
        "identity": {
            "served_model_id": _MODEL_ID,
            "runtime_manifest_identity_sha256": _RUNTIME_IDENTITY,
        },
        "campaign_epoch": {
            **canonical_epoch,
            "expected_sha256": campaign_sha256,
            "observed_sha256": campaign_sha256,
        },
        "inference_liveness": {
            "schema": "mineru-arm-inference-liveness.v1",
            "profile": "epoch-bound-multimodal-canary.v1",
            "pre_arm": boundary(phase="pre_arm", started=1.0, finished=2.0),
            "workload_started_at_utc": "2026-08-27T01:00:03+00:00",
            "workload_finished_at_utc": "2026-08-27T01:29:57+00:00",
            "workload_started_observed_seconds": 3.0,
            "workload_finished_observed_seconds": 1797.0,
            "post_arm": boundary(
                phase="post_arm",
                started=1798.0,
                finished=1799.0,
            ),
        },
        "host_capacity": {
            "status": "pass",
            "failure": None,
            "collector_sha256": _COLLECTOR_SHA,
            "windows_node_identity_sha256": _NODE_SHA,
            "violations": [],
            "sampling_failures": [],
            "samples": [
                {
                    "observed_seconds": 0.0,
                    "containers": [
                        _container("mineru-api", _API_ID),
                        _container("mineru-api-proxy", _PROXY_ID),
                        _container("mineru-openai-server", _INFERENCE_ID),
                    ],
                },
                {
                    "observed_seconds": 1800.0,
                    "containers": [
                        _container("mineru-api", _API_ID),
                        _container("mineru-api-proxy", _PROXY_ID),
                        _container("mineru-openai-server", _INFERENCE_ID),
                    ],
                },
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
        receipt["host_capacity"]["samples"][1]["containers"][0]["id"] = "9" * 64

        with self.assertRaisesRegex(ValueError, "epoch changed"):
            _receipt_identity(receipt)

    def test_receipt_identity_rejects_duplicate_host_container_names(self) -> None:
        receipt = deepcopy(_receipt())
        for sample in receipt["host_capacity"]["samples"]:
            sample["containers"].append(
                deepcopy(sample["containers"][1])
            )

        with self.assertRaisesRegex(ValueError, "host containers"):
            _receipt_identity(receipt)

    def test_receipt_identity_rejects_unbound_or_short_safety_limits(self) -> None:
        for tamper in ("missing", "short_document", "short_drain"):
            receipt = deepcopy(_receipt())
            if tamper == "missing":
                receipt.pop("safety_limits")
            elif tamper == "short_document":
                receipt["safety_limits"][
                    "document_runaway_timeout_seconds"
                ] = 1800
            else:
                receipt["safety_limits"]["api_drain_timeout_seconds"] = 1800
            with self.subTest(tamper=tamper), self.assertRaisesRegex(
                ValueError,
                "not PASS",
            ):
                _receipt_identity(receipt)


if __name__ == "__main__":
    unittest.main()
