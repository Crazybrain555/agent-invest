"""Phase-trace binding to held-out validation regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    MinerUDeploymentGateError,
    VerifiedMinerUHeldoutValidation,
)
from scripts.build_mineru_validation_receipt import build_receipt
from scripts.collect_mineru_phase_trace import _validation_identity
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


if __name__ == "__main__":
    unittest.main()
