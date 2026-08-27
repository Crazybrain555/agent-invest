"""Exact-current runtime identity gates for passive capacity observation."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import capacity_runtime_identity as identity
from disclosure_anchor.adapters.runtime.mineru_identity import (
    VerifiedMinerURuntimeManifest,
)
from tests.unit.test_capacity_observer import _settings


def _endpoint(url: str) -> str:
    return "sha256:" + hashlib.sha256(url.rstrip("/").encode("utf-8")).hexdigest()


class CapacityRuntimeIdentityTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[object, Path, dict[str, str]]:
        urls = {
            "api": "http://127.0.0.1:30002",
            "observability": "http://127.0.0.1:30003",
            "inference": "http://127.0.0.1:30001/v1",
        }
        settings = _settings(root).model_copy(
            update={
                "disclosure_mineru_bin": root / "mineru",
                "disclosure_mineru_api_url": urls["api"],
                "disclosure_mineru_observability_url": urls["observability"],
                "disclosure_mineru_inference_upstream_url": urls["inference"],
                "disclosure_mineru_api_task_slots": 1,
            }
        )
        manifest_path = root / "runtime.json"
        manifest_path.write_text("{}", encoding="utf-8")
        return settings, manifest_path, urls

    def _verified(
        self,
        urls: dict[str, str],
        *,
        slots: int = 1,
        api_identity: str | None = None,
    ) -> VerifiedMinerURuntimeManifest:
        return VerifiedMinerURuntimeManifest(
            manifest={
                "topology": {
                    "api_endpoint_sha256": api_identity or _endpoint(urls["api"]),
                    "observability_endpoint_sha256": _endpoint(
                        urls["observability"]
                    ),
                    "inference_upstream_sha256": _endpoint(urls["inference"]),
                    "windows_collector_sha256": "sha256:" + "2" * 64,
                    "windows_node_identity_sha256": "sha256:" + "3" * 64,
                    "ssh_host_key_sha256": "sha256:" + "4" * 64,
                }
            },
            identity_sha256="sha256:" + "1" * 64,
            orchestrator_identity_sha256="sha256:" + "5" * 64,
            provider_identity_sha256="sha256:" + "6" * 64,
            served_model_id="model",
            max_concurrent_requests=slots,
        )

    def test_verified_topology_returns_closed_host_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, manifest_path, urls = self._fixture(Path(tmp))
            with patch.object(
                identity,
                "client_bundle_identity",
                return_value=object(),
            ), patch.object(
                identity,
                "writer_code_digest",
                return_value="sha256:" + "7" * 64,
            ), patch.object(
                identity,
                "verify_runtime_manifest_payload",
                return_value=self._verified(urls),
            ):
                topology = identity.verify_capacity_runtime_topology(
                    settings=settings,  # type: ignore[arg-type]
                    runtime_manifest_path=manifest_path,
                )

            self.assertEqual(topology.windows_collector_sha256, "sha256:" + "2" * 64)
            self.assertEqual(topology.ssh_host_key_sha256, "sha256:" + "4" * 64)

    def test_slots_and_endpoint_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, manifest_path, urls = self._fixture(Path(tmp))
            for verified, message in (
                (self._verified(urls, slots=2), "task slots drifted"),
                (
                    self._verified(urls, api_identity="sha256:" + "9" * 64),
                    "endpoint identity drifted",
                ),
            ):
                with self.subTest(message=message), patch.object(
                    identity,
                    "client_bundle_identity",
                    return_value=object(),
                ), patch.object(
                    identity,
                    "writer_code_digest",
                    return_value="sha256:" + "7" * 64,
                ), patch.object(
                    identity,
                    "verify_runtime_manifest_payload",
                    return_value=verified,
                ), self.assertRaisesRegex(ValueError, message):
                    identity.verify_capacity_runtime_topology(
                        settings=settings,  # type: ignore[arg-type]
                        runtime_manifest_path=manifest_path,
                    )


if __name__ == "__main__":
    unittest.main()
