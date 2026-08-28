"""Deterministic tests for immutable MinerU Auto-capacity catalogs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.build_mineru_capacity_catalog import (
    build_capacity_catalog,
    main,
)
from disclosure_anchor.adapters.runtime.mineru_capacity_evaluator_identity import (
    commissioning_evaluator_identity,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _profile() -> dict[str, object]:
    return {
        "inner_inference_concurrency": 7,
        "max_document_pages": 10000,
        "max_resident_pages": 16,
        "max_source_pdf_bytes": 1024 * 1024 * 1024,
        "min_document_pages": 9,
        "pipeline_depth": 1,
        "profile_id": "rtx5080-w8-d1-c7-s128-v1",
        "schema": "mineru-execution-profile.v2",
        "vllm_max_num_seqs": 128,
        "window_size": 8,
    }


def _collector_sha256() -> str:
    collector = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "windows"
        / "collect_mineru_runtime.ps1"
    )
    return "sha256:" + hashlib.sha256(collector.read_bytes()).hexdigest()


def _receipt(profile: dict[str, object]) -> dict[str, object]:
    profile_sha256 = "sha256:" + hashlib.sha256(_canonical(profile)).hexdigest()
    collector_sha256 = _collector_sha256()
    return {
        "collector_sha256": collector_sha256,
        "evaluation": {
            "arm_finished_at_utc": [
                "2026-08-28T00:01:00+00:00",
                "2026-08-28T00:03:00+00:00",
                "2026-08-28T00:05:00+00:00",
                "2026-08-28T00:07:00+00:00",
            ],
            "arm_execution_ids": ["a1", "b1", "b2", "a2"],
            "arm_modes": ["legacy", "candidate", "candidate", "legacy"],
            "arm_pages_per_host_hour_milli": [100, 120, 121, 101],
            "arm_started_at_utc": [
                "2026-08-28T00:00:00+00:00",
                "2026-08-28T00:02:00+00:00",
                "2026-08-28T00:04:00+00:00",
                "2026-08-28T00:06:00+00:00",
            ],
            "baseline_ceiling_pages_per_host_hour_milli": 101,
            "baseline_profile_sha256": "sha256:" + "d" * 64,
            "baseline_repeat_spread_basis_points": 100,
            "candidate_absolute_gain_pages_per_host_hour_milli": 19,
            "candidate_floor_pages_per_host_hour_milli": 120,
            "candidate_profile_sha256": profile_sha256,
            "candidate_relative_gain_basis_points": 1881,
            "candidate_repeat_spread_basis_points": 83,
            "collector_path": (
                "C:\\ProgramData\\agent-invest\\mineru-runtime-v6\\"
                "collect_mineru_runtime.ps1"
            ),
            "collector_sha256": collector_sha256,
            "decision": "COMMISSION",
            "docker_memory_reserve_bytes": 7 * 1024**3,
            "empirical_repeat_noise_pages_per_host_hour_milli": 1,
            "findings": [],
            "maximum_repeat_spread_basis_points": 300,
            "minimum_improvement_basis_points": 500,
            "output_semantics": (
                "source-page-block-equality-with-per-arm-bundle-validation.v1"
            ),
            "page_count_per_arm": 100,
            "profile_commissioning_authorized": True,
            "schema": "mineru-capacity-commissioning.v2",
            "selection_rule": (
                "min(B)>max(A)+repeat_noise; gain>=minimum_bps; "
                "A_spread,B_spread<=maximum_bps"
            ),
            "windows_node_identity_sha256": "sha256:" + "e" * 64,
        },
        "evaluator": commissioning_evaluator_identity(),
        "generated_at_utc": "2026-08-28T00:08:00+00:00",
        "input_evidence": [
            {"role": f"{arm}_{kind}", "sha256": "sha256:" + digit * 64}
            for arm, digit in zip(("a1", "b1", "b2", "a2"), "1234", strict=True)
            for kind in ("staged_load", "phase_trace")
        ],
        "schema": "mineru-capacity-commissioning-receipt.v2",
    }


class BuildMinerUCapacityCatalogTests(unittest.TestCase):
    def test_catalog_binds_exact_receipt_profile_evaluator_and_runtime(self) -> None:
        profile = _profile()
        receipt_bytes = _canonical(_receipt(profile)) + b"\n"
        profile_bytes = _canonical(profile)

        catalog = build_capacity_catalog(
            commissioning_receipt_bytes=receipt_bytes,
            profile_bytes=profile_bytes,
            runtime_compatibility_sha256="sha256:" + "b" * 64,
        )

        self.assertEqual(catalog["schema"], "mineru-capacity-catalog.v1")
        self.assertEqual(
            catalog["profile_sha256"],
            "sha256:" + hashlib.sha256(profile_bytes).hexdigest(),
        )
        self.assertEqual(
            catalog["commissioning_receipt_sha256"],
            "sha256:" + hashlib.sha256(receipt_bytes).hexdigest(),
        )
        self.assertEqual(
            catalog["commissioning_evaluator_sha256"],
            _receipt(profile)["evaluator"]["bundle_sha256"],  # type: ignore[index]
        )

    def test_catalog_rejects_stop_and_profile_drift(self) -> None:
        profile = _profile()
        stopped = _receipt(profile)
        stopped["evaluation"]["decision"] = "STOP"  # type: ignore[index]
        stopped["evaluation"]["profile_commissioning_authorized"] = False  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "does not authorize"):
            build_capacity_catalog(
                commissioning_receipt_bytes=_canonical(stopped),
                profile_bytes=_canonical(profile),
                runtime_compatibility_sha256="sha256:" + "b" * 64,
            )

        drifted = dict(profile)
        drifted["window_size"] = 7
        with self.assertRaisesRegex(ValueError, "exact profile"):
            build_capacity_catalog(
                commissioning_receipt_bytes=_canonical(_receipt(profile)) + b"\n",
                profile_bytes=_canonical(drifted),
                runtime_compatibility_sha256="sha256:" + "b" * 64,
            )

        forged = _receipt(profile)
        forged["evaluator"]["bundle_sha256"] = "sha256:" + "f" * 64  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "evaluator bundle drifted"):
            build_capacity_catalog(
                commissioning_receipt_bytes=_canonical(forged) + b"\n",
                profile_bytes=_canonical(profile),
                runtime_compatibility_sha256="sha256:" + "b" * 64,
            )

        stale_collector = _receipt(profile)
        stale_collector["collector_sha256"] = "sha256:" + "f" * 64
        stale_collector["evaluation"]["collector_sha256"] = "sha256:" + "f" * 64  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "collector is not current"):
            build_capacity_catalog(
                commissioning_receipt_bytes=_canonical(stale_collector) + b"\n",
                profile_bytes=_canonical(profile),
                runtime_compatibility_sha256="sha256:" + "b" * 64,
            )

    def test_cli_writes_new_only_canonical_private_catalog(self) -> None:
        profile = _profile()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = root / "commissioning.json"
            profile_path = root / "profile.json"
            catalog = root / "catalog.json"
            receipt.write_bytes(_canonical(_receipt(profile)) + b"\n")
            profile_path.write_bytes(_canonical(profile))

            service_root = Path(__file__).resolve().parents[2]
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(service_root / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/build_mineru_capacity_catalog.py",
                    "--commissioning-receipt",
                    str(receipt),
                    "--profile",
                    str(profile_path),
                    "--runtime-compatibility-sha256",
                    "sha256:" + "b" * 64,
                    "--catalog-out",
                    str(catalog),
                ],
                cwd=service_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(catalog.stat().st_mode & 0o777, 0o600)
            self.assertEqual(catalog.read_bytes(), _canonical(json.loads(catalog.read_bytes())))
            with self.assertRaisesRegex(ValueError, "must be new"):
                main(
                    [
                        "--commissioning-receipt",
                        str(receipt),
                        "--profile",
                        str(profile_path),
                        "--runtime-compatibility-sha256",
                        "sha256:" + "b" * 64,
                        "--catalog-out",
                        str(catalog),
                    ]
                )


if __name__ == "__main__":
    unittest.main()
