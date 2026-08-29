"""Phase-trace binding to held-out validation regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    MinerUDeploymentGateError,
    VerifiedMinerUHeldoutValidation,
)
from scripts.build_mineru_validation_receipt import build_receipt
from scripts.collect_mineru_phase_trace import (
    _read_regular,
    _validation_identity,
    _write_new_private,
)
from tests.unit.test_build_mineru_validation_receipt import (
    _epoch,
    _smoke,
    _write_private,
)


class CollectMineruPhaseTraceTests(unittest.TestCase):
    def _validation(self, root: Path) -> dict[str, object]:
        now = datetime.now(UTC)
        paths = []
        for index, offset in ((1, 40), (2, 30)):
            path = root / f"smoke-{index}.json"
            _write_private(
                path,
                _smoke(index=index, start=now - timedelta(seconds=offset)),
            )
            paths.append(path)
        before = root / "before.json"
        after = root / "after.json"
        _write_private(before, _epoch(now - timedelta(seconds=50)))
        _write_private(after, _epoch(now - timedelta(seconds=20)))
        return build_receipt(
            paths,
            epoch_before_path=before,
            epoch_after_path=after,
        )

    def test_identity_conserves_validation_documents_and_pages(self) -> None:
        now = datetime.now(UTC)
        verified = VerifiedMinerUHeldoutValidation(
            started_at_utc=now - timedelta(seconds=10),
            finished_at_utc=now - timedelta(seconds=5),
            runtime_identity_sha256="sha256:" + "1" * 64,
            collector_sha256="sha256:" + "2" * 64,
            windows_node_identity_sha256="sha256:" + "3" * 64,
            api_container_id="5" * 64,
            document_count=2,
            page_count=7,
        )
        with patch(
            "scripts.collect_mineru_phase_trace."
            "verify_mineru_heldout_validation",
            return_value=verified,
        ) as shared:
            value = _validation_identity({}, contract={})

        self.assertEqual(value[6], 2)
        self.assertEqual(value[7], 7)
        self.assertRegex(value[2], r"^sha256:[a-f0-9]{64}$")
        self.assertEqual(value[5], "5" * 64)
        shared.assert_called_once_with({})

    def test_shared_validation_failure_is_fail_closed(self) -> None:
        with patch(
            "scripts.collect_mineru_phase_trace."
            "verify_mineru_heldout_validation",
            side_effect=MinerUDeploymentGateError("page drift"),
        ), self.assertRaisesRegex(ValueError, "page drift"):
            _validation_identity({}, contract={})

    def test_serialized_receipt_contains_no_document_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            encoded = json.dumps(self._validation(Path(tmp)), sort_keys=True)

        self.assertNotIn("markdown", encoded)
        self.assertNotIn("text_content", encoded)

    def test_regular_reader_rejects_same_size_overwrite_with_restored_mtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            path.write_bytes(b'{"value":"aaaa"}')
            path.chmod(0o600)
            original_times = path.stat()
            real_fstat = os.fstat
            calls = 0

            def overwriting_fstat(descriptor: int) -> os.stat_result:
                nonlocal calls
                metadata = real_fstat(descriptor)
                calls += 1
                if calls == 1:
                    path.write_bytes(b'{"value":"bbbb"}')
                    os.utime(
                        path,
                        ns=(original_times.st_atime_ns, original_times.st_mtime_ns),
                    )
                return metadata

            with patch(
                "scripts.collect_mineru_phase_trace.os.fstat",
                side_effect=overwriting_fstat,
            ), self.assertRaisesRegex(ValueError, "changed while reading"):
                _read_regular(path, label="evidence", maximum_bytes=1024)

    def test_capture_writer_fsyncs_parent_and_preserves_fsync_error(self) -> None:
        original = OSError("parent fsync failed")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "capture.jsonl"
            with patch(
                "scripts.collect_mineru_phase_trace.os.fsync",
                side_effect=(None, original),
            ) as fsync, self.assertRaises(OSError) as caught:
                _write_new_private(output, b"capture")

            self.assertIs(caught.exception, original)
            self.assertEqual(fsync.call_count, 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
