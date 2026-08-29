"""Content-free MinerU service epoch freeze regressions."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.mineru_identity import (
    canonical_payload_sha256,
)
from scripts.freeze_mineru_campaign_epoch import main
from tests.unit.test_capacity_host_observer import COLLECTOR, NODE, _payload


class FreezeMineruServiceEpochTests(unittest.TestCase):
    def test_freeze_records_clean_epoch_without_memory_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {
                "contract_version": "mineru-runtime-bundle.v8",
                "topology": {
                    "windows_collector_sha256": COLLECTOR,
                    "windows_node_identity_sha256": NODE,
                    "ssh_host_key_sha256": "sha256:" + "6" * 64,
                    "windows_compose_sha256": "sha256:" + "7" * 64,
                },
                "client": {"writer_code_sha256": "sha256:" + "8" * 64},
                "orchestrator": {"container_image_digest": "sha256:" + "9" * 64},
            }
            wrapper = {
                "identity_sha256": canonical_payload_sha256(manifest),
                "manifest": manifest,
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(wrapper), encoding="utf-8")
            manifest_path.chmod(0o600)
            identity = root / "identity"
            known_hosts = root / "known_hosts"
            identity.write_text("unused", encoding="utf-8")
            known_hosts.write_text("unused", encoding="utf-8")
            identity.chmod(0o600)
            known_hosts.chmod(0o600)
            receipt = root / "epoch.json"
            with (
                patch(
                    "scripts.freeze_mineru_campaign_epoch."
                    "build_host_observer_ssh_command",
                    return_value=["ssh"],
                ),
                patch(
                    "scripts.freeze_mineru_campaign_epoch."
                    "MineruHostCapacitySampler.sample_payload",
                    return_value=_payload(),
                ),
            ):
                result = main(
                    [
                        "--runtime-manifest",
                        str(manifest_path),
                        "--receipt-out",
                        str(receipt),
                        "--ssh-host",
                        "host",
                        "--ssh-user",
                        "operator",
                        "--ssh-identity",
                        str(identity),
                        "--ssh-known-hosts",
                        str(known_hosts),
                    ]
                )
            payload = json.loads(receipt.read_bytes())

        self.assertEqual(result, 0)
        self.assertEqual(payload["schema"], "mineru-service-epoch-freeze.v2")
        self.assertEqual(payload["safety"]["restart_count_total"], 0)
        self.assertNotIn("reserve", json.dumps(payload, sort_keys=True))

if __name__ == "__main__":
    unittest.main()
