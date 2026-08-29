"""Regressions for new-only MinerU campaign epoch freezing."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from disclosure_anchor.adapters.runtime.mineru_identity import (
    canonical_payload_sha256,
)
from scripts import freeze_mineru_campaign_epoch as freeze


class FreezeMineruCampaignEpochTests(unittest.TestCase):
    def _runtime_manifest(self, root: Path) -> Path:
        manifest = {
            "contract_version": "mineru-runtime-bundle.v8",
            "topology": {
                "windows_collector_sha256": "sha256:" + "a" * 64,
                "windows_node_identity_sha256": "sha256:" + "b" * 64,
            },
        }
        path = root / "runtime.json"
        path.write_text(
            json.dumps(
                {
                    "identity_sha256": canonical_payload_sha256(manifest),
                    "manifest": manifest,
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    @staticmethod
    def _host_sample() -> dict[str, object]:
        return {
            "schema": "mineru-host-capacity-sample.v1",
            "containers": [
                {
                    "name": "mineru-api",
                    "id": "1" * 64,
                    "started_at_utc": "2026-08-29T01:03:38+00:00",
                },
                {
                    "name": "mineru-api-proxy",
                    "id": "2" * 64,
                    "started_at_utc": "2026-08-24T18:51:44+00:00",
                },
                {
                    "name": "mineru-openai-server",
                    "id": "3" * 64,
                    "started_at_utc": "2026-08-24T18:51:44+00:00",
                },
            ],
        }

    @staticmethod
    def _argv(manifest: Path, receipt: Path) -> list[str]:
        return [
            "--runtime-manifest",
            str(manifest),
            "--receipt-out",
            str(receipt),
            "--ssh-host",
            "windows.example.invalid",
            "--ssh-user",
            "operator",
            "--ssh-identity",
            "/private/operator-key",
            "--ssh-known-hosts",
            "/private/known-hosts",
            "--docker-memory-reserve-bytes",
            "7516192768",
        ]

    def test_make_target_uses_import_safe_module_entrypoint(self) -> None:
        makefile = Path("Makefile").read_text(encoding="utf-8")

        self.assertIn(
            "$(PYTHON) -m scripts.freeze_mineru_campaign_epoch",
            makefile,
        )

    def test_private_manifest_read_is_owner_only_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._runtime_manifest(Path(tmp))
            payload = freeze._read_private_json(path)

            self.assertEqual(
                payload["identity_sha256"],
                canonical_payload_sha256(payload["manifest"]),
            )
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                freeze._read_private_json(path)

    def test_private_manifest_read_rejects_metadata_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._runtime_manifest(Path(tmp))
            before = path.stat(follow_symlinks=False)
            after = SimpleNamespace(
                **{
                    field: getattr(before, field)
                    for field in (
                        "st_dev",
                        "st_ino",
                        "st_mode",
                        "st_uid",
                        "st_nlink",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    )
                }
            )
            after.st_mtime_ns += 1

            with (
                patch.object(freeze.os, "fstat", side_effect=[before, after]),
                self.assertRaisesRegex(ValueError, "changed while reading"),
            ):
                freeze._read_private_json(path)

    def test_main_writes_new_private_epoch_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._runtime_manifest(root)
            receipt = root / "epoch.json"
            transport = MagicMock()
            with (
                patch.object(
                    freeze,
                    "_HostObserverControlMaster",
                    return_value=transport,
                ),
                patch.object(
                    freeze,
                    "_fetch_host_capacity_sample",
                    return_value=self._host_sample(),
                ) as fetch,
            ):
                result = freeze.main(self._argv(manifest, receipt))

            self.assertEqual(result, 0)
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], freeze.FREEZE_SCHEMA)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["database_access"], "none")
            self.assertEqual(payload["queue_access"], "none")
            self.assertEqual(
                payload["campaign_epoch"]["services"]["inference"]["container_id"],
                "3" * 64,
            )
            self.assertEqual(
                payload["campaign_epoch"]["services"]["proxy"]["container_id"],
                "2" * 64,
            )
            transport.start.assert_called_once_with()
            transport.close.assert_called_once_with()
            fetch.assert_called_once()

    def test_invalid_manifest_fails_before_remote_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._runtime_manifest(root)
            wrapper = json.loads(manifest.read_text(encoding="utf-8"))
            wrapper["identity_sha256"] = "sha256:" + "9" * 64
            manifest.write_text(json.dumps(wrapper), encoding="utf-8")
            manifest.chmod(0o600)
            receipt = root / "epoch.json"

            with (
                patch.object(freeze, "_HostObserverControlMaster") as transport,
                self.assertRaisesRegex(SystemExit, "identity is invalid"),
            ):
                freeze.main(self._argv(manifest, receipt))

            transport.assert_not_called()
            self.assertFalse(receipt.exists())

    def test_existing_output_fails_before_remote_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._runtime_manifest(root)
            receipt = root / "epoch.json"
            receipt.write_text("existing", encoding="utf-8")

            with (
                patch.object(freeze, "_HostObserverControlMaster") as transport,
                self.assertRaisesRegex(SystemExit, "output already exists"),
            ):
                freeze.main(self._argv(manifest, receipt))

            transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
