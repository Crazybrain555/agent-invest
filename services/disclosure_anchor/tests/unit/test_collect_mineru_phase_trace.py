"""Phase-trace binding to held-out validation regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from disclosure_anchor.adapters.runtime.mineru_identity import (
    canonical_payload_sha256,
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
        with tempfile.TemporaryDirectory() as tmp:
            value = _validation_identity(self._validation(Path(tmp)))

        self.assertEqual(value[6], 2)
        self.assertEqual(value[7], 7)
        self.assertRegex(value[2], r"^sha256:[a-f0-9]{64}$")
        self.assertEqual(value[5], "5" * 64)

    def test_hash_epoch_and_page_drift_fail_closed(self) -> None:
        for tamper in ("hash", "epoch", "page"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                receipt = self._validation(Path(tmp))
                if tamper == "hash":
                    receipt["documents"][0]["receipt"]["provider"][
                        "page_count"
                    ] += 1
                elif tamper == "epoch":
                    after = receipt["epoch_after"]
                    after["receipt"]["service_epoch"]["api_container_id"] = "6" * 64
                    after["receipt"]["service_epoch_sha256"] = (
                        canonical_payload_sha256(
                            after["receipt"]["service_epoch"]
                        )
                    )
                    after["receipt_sha256"] = canonical_payload_sha256(
                        after["receipt"]
                    )
                else:
                    document = receipt["documents"][0]
                    document["receipt"]["provider"]["page_count"] += 1
                    document["receipt_sha256"] = canonical_payload_sha256(
                        document["receipt"]
                    )
                with self.assertRaises(ValueError):
                    _validation_identity(receipt)

    def test_serialized_receipt_contains_no_document_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            encoded = json.dumps(self._validation(Path(tmp)), sort_keys=True)

        self.assertNotIn("markdown", encoded)
        self.assertNotIn("text_content", encoded)


if __name__ == "__main__":
    unittest.main()
