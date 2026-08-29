"""Held-out MinerU validation receipt builder regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from scripts.build_mineru_validation_receipt import (
    _canonical_sha256,
    _load,
    _write_new,
    build_receipt,
)


RUNTIME = "sha256:" + "1" * 64


def _write_private(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _smoke(*, index: int, start: datetime) -> dict[str, object]:
    target = ParserTargetIdentity(
        name="MinerU",
        package_version="3.4.4",
        backend="hybrid-http-client",
        method="auto",
        language="ch",
        formula=True,
        table=True,
        effort="medium",
        runtime_bundle_identity_sha256=RUNTIME,
    )
    return {
        "schema": "mineru_smoke_receipt.v5",
        "status": "pass",
        "started_at_utc": start.isoformat(),
        "finished_at_utc": (start + timedelta(seconds=2)).isoformat(),
        "database_access": "none",
        "queue_access": "none",
        "input": {
            "profile": "diagnostic_custom",
            "sha256": "sha256:" + f"{index:064x}",
            "page_count": 2 + index,
        },
        "provider": {
            "page_count": 2 + index,
            "target_identity": target.to_payload(),
        },
        "identity": {"runtime_manifest_identity_sha256": RUNTIME},
        "topology": {"identity": "same"},
        "runtime_manifest": {"identity": "same"},
    }


def _epoch(created: datetime) -> dict[str, object]:
    service_epoch = {
        "schema": "mineru-service-epoch.v1",
        "runtime_manifest_identity_sha256": RUNTIME,
        "collector_sha256": "sha256:" + "2" * 64,
        "windows_node_identity_sha256": "sha256:" + "3" * 64,
        "windows_compose_sha256": "sha256:" + "6" * 64,
        "writer_code_sha256": "sha256:" + "7" * 64,
        "api_image_digest": "sha256:" + "8" * 64,
        "container_epoch_sha256": "sha256:" + "4" * 64,
        "api_container_id": "5" * 64,
    }
    return {
        "schema": "mineru-service-epoch-freeze.v2",
        "status": "pass",
        "created_at_utc": created.isoformat(),
        "database_access": "none",
        "queue_access": "none",
        "service_epoch": service_epoch,
        "service_epoch_sha256": _canonical_sha256(service_epoch),
        "safety": {
            "restart_count_total": 0,
            "oom_killed_count": 0,
            "unsafe_container_count": 0,
            "cgroup_oom_total": 0,
            "cgroup_oom_kill_total": 0,
        },
    }


class BuildMineruValidationReceiptTests(unittest.TestCase):
    def _fixture(
        self, root: Path
    ) -> tuple[list[Path], Path, Path, datetime]:
        now = datetime.now(UTC)
        smokes = []
        for index, offset in ((1, 40), (2, 30)):
            path = root / f"smoke-{index}.json"
            _write_private(path, _smoke(index=index, start=now - timedelta(seconds=offset)))
            smokes.append(path)
        before = root / "before.json"
        after = root / "after.json"
        _write_private(before, _epoch(now - timedelta(seconds=50)))
        _write_private(after, _epoch(now - timedelta(seconds=20)))
        return smokes, before, after, now

    def test_builds_hash_bound_receipt_for_complete_distinct_pdfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            smokes, before, after, _ = self._fixture(Path(tmp))
            receipt = build_receipt(
                smokes,
                epoch_before_path=before,
                epoch_after_path=after,
            )

        self.assertEqual(receipt["schema"], "mineru_heldout_validation_receipt.v1")
        self.assertEqual(receipt["document_count"], 2)
        for wrapper in receipt["documents"]:
            self.assertEqual(
                wrapper["receipt_sha256"], _canonical_sha256(wrapper["receipt"])
            )
            self.assertRegex(wrapper["source_bytes_sha256"], r"^sha256:[a-f0-9]{64}$")

    def test_rejects_duplicate_page_loss_epoch_drift_and_bad_timeline(self) -> None:
        for tamper in ("duplicate", "page_loss", "epoch", "timeline"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                smokes, before, after, now = self._fixture(Path(tmp))
                second = json.loads(smokes[1].read_bytes())
                if tamper == "duplicate":
                    first = json.loads(smokes[0].read_bytes())
                    second["input"]["sha256"] = first["input"]["sha256"]
                elif tamper == "page_loss":
                    second["provider"]["page_count"] -= 1
                elif tamper == "timeline":
                    second["finished_at_utc"] = (now - timedelta(seconds=35)).isoformat()
                else:
                    epoch = json.loads(after.read_bytes())
                    epoch["service_epoch"]["api_container_id"] = "6" * 64
                    epoch["service_epoch_sha256"] = _canonical_sha256(
                        epoch["service_epoch"]
                    )
                    _write_private(after, epoch)
                if tamper != "epoch":
                    _write_private(smokes[1], second)
                with self.assertRaises(ValueError):
                    build_receipt(
                        smokes,
                        epoch_before_path=before,
                        epoch_after_path=after,
                    )

    def test_input_must_be_private_single_link_and_stable(self) -> None:
        for tamper in ("mode", "hardlink"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                smokes, _, _, _ = self._fixture(Path(tmp))
                if tamper == "mode":
                    smokes[0].chmod(0o644)
                else:
                    alias = Path(tmp) / "alias.json"
                    os.link(smokes[0], alias)
                with self.assertRaisesRegex(ValueError, "owner-only/bounded"):
                    _load(smokes[0])

        with tempfile.TemporaryDirectory() as tmp:
            smokes, _, _, _ = self._fixture(Path(tmp))
            real_fstat = os.fstat
            injected = False

            def growing_fstat(descriptor: int) -> os.stat_result:
                nonlocal injected
                metadata = real_fstat(descriptor)
                if not injected:
                    injected = True
                    with smokes[0].open("ab") as handle:
                        handle.write(b" ")
                return metadata

            with (
                patch("scripts.build_mineru_validation_receipt.os.fstat", side_effect=growing_fstat),
                self.assertRaisesRegex(ValueError, "changed while reading"),
            ):
                _load(smokes[0])

        with tempfile.TemporaryDirectory() as tmp:
            smokes, _, _, _ = self._fixture(Path(tmp))
            real_fstat = os.fstat
            injected = False

            def overwriting_fstat(descriptor: int) -> os.stat_result:
                nonlocal injected
                metadata = real_fstat(descriptor)
                if not injected:
                    injected = True
                    original = smokes[0].read_bytes()
                    smokes[0].write_bytes(b" " + original[1:])
                return metadata

            with patch(
                "scripts.build_mineru_validation_receipt.os.fstat",
                side_effect=overwriting_fstat,
            ), self.assertRaisesRegex(ValueError, "changed while reading"):
                _load(smokes[0])

    def test_output_enforces_exact_sixteen_mib_budget(self) -> None:
        payload = {"value": "x" * 128}
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(
                "scripts.build_mineru_validation_receipt.MAX_OUTPUT_BYTES",
                len(encoded),
            ):
                _write_new(root / "boundary.json", payload)
            with patch(
                "scripts.build_mineru_validation_receipt.MAX_OUTPUT_BYTES",
                len(encoded) - 1,
            ), self.assertRaisesRegex(ValueError, "16 MiB"):
                _write_new(root / "oversize.json", payload)

    def test_input_rejects_duplicate_keys_and_nonfinite_numbers(self) -> None:
        for encoded in (b'{"a":1,"a":2}', b'{"a":NaN}'):
            with self.subTest(encoded=encoded), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "evidence.json"
                path.write_bytes(encoded)
                path.chmod(0o600)
                with self.assertRaises(ValueError):
                    _load(path)


if __name__ == "__main__":
    unittest.main()
