from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import get_args
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.semantic_route_store import (
    SemanticRouteFileCache,
    SemanticRouteReceiptStore,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_FALLBACK_KEY,
    SEMANTIC_ROUTER_VERSION,
    SemanticAdjudicatorMetadata,
    SemanticAdjudicationDecision,
    SemanticAdjudicatedRoute,
    SemanticRouteContractError,
    SemanticRouteEvidence,
    SemanticRouteEvidenceKind,
    SemanticRouteReceipt,
    SemanticRouteReceiptRow,
)
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticRouteReceiptStoreError,
)


class _Paths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def data_path(self, relpath: Path) -> Path:
        return self.root / relpath


def _row(index: int = 1) -> SemanticRouteReceiptRow:
    return SemanticRouteReceiptRow(
        asset_id=f"asset_{index}",
        order_index=index,
        receipt=SemanticRouteReceipt(
            taxonomy_version="semantic-test.v1",
            router_version=SEMANTIC_ROUTER_VERSION,
            input_hash="sha256:" + f"{index:064x}"[-64:],
            candidate_keys=(),
            semantic_keys=(SEMANTIC_FALLBACK_KEY,),
            decision_source="fallback",
            evidence=(
                SemanticRouteEvidence(
                    key=SEMANTIC_FALLBACK_KEY,
                    kinds=("fallback",),
                    source_ids=(),
                ),
            ),
        ),
    )


class SemanticRouteReceiptStoreTests(unittest.TestCase):
    def test_receipt_sidecar_round_trips_and_rejects_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _Paths(Path(tmp))
            store = SemanticRouteReceiptStore(
                paths=paths,  # type: ignore[arg-type]
                artifacts=ArtifactStore(paths),  # type: ignore[arg-type]
            )
            relpath = Path("derived/test/semantic_route_receipts.v1.jsonl")
            rows = (_row(),)

            result = store.write(relpath=relpath, rows=rows)

            self.assertEqual(
                store.read(
                    relpath=relpath,
                    expected_hash=result.artifact_hash,
                ),
                rows,
            )
            self.assertTrue(result.artifact_hash.startswith("sha256:"))
            path = paths.data_path(relpath)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["extra"] = True
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(SemanticRouteContractError, "hash differs"):
                store.read(
                    relpath=relpath,
                    expected_hash=result.artifact_hash,
                )

    def test_receipt_sidecar_round_trips_every_declared_evidence_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _Paths(Path(tmp))
            store = SemanticRouteReceiptStore(
                paths=paths,  # type: ignore[arg-type]
                artifacts=ArtifactStore(paths),  # type: ignore[arg-type]
            )
            receipt = SemanticRouteReceipt(
                taxonomy_version="semantic-test.v1",
                router_version=SEMANTIC_ROUTER_VERSION,
                input_hash="sha256:" + "a" * 64,
                candidate_keys=("test_route",),
                semantic_keys=("test_route",),
                decision_source="model",
                evidence=(
                    SemanticRouteEvidence(
                        key="test_route",
                        kinds=get_args(SemanticRouteEvidenceKind),
                        source_ids=("u1:title",),
                    ),
                ),
                adjudicator=SemanticAdjudicatorMetadata(
                    adapter="semantic-test.medium",
                    model="semantic-test",
                    prompt_version="semantic-test.v1",
                    cache_key="sha256:" + "b" * 64,
                    response_sha256="sha256:" + "c" * 64,
                    cache_hit=False,
                ),
            )
            rows = (
                SemanticRouteReceiptRow(
                    asset_id="asset_1",
                    order_index=1,
                    receipt=receipt,
                ),
            )
            relpath = Path("derived/test/all-evidence-kinds.jsonl")

            result = store.write(relpath=relpath, rows=rows)

            self.assertEqual(
                store.read(relpath=relpath, expected_hash=result.artifact_hash),
                rows,
            )

    def test_receipt_sidecar_rejects_noncontiguous_rows_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _Paths(Path(tmp))
            store = SemanticRouteReceiptStore(
                paths=paths,  # type: ignore[arg-type]
                artifacts=ArtifactStore(paths),  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(SemanticRouteContractError, "contiguous"):
                store.write(
                    relpath=Path("derived/test/receipts.jsonl"),
                    rows=(_row(2),),
                )

            target = Path(tmp) / "outside.jsonl"
            target.write_text("{}\n", encoding="utf-8")
            link = Path(tmp) / "receipt-link.jsonl"
            link.symlink_to(target)
            with self.assertRaisesRegex(SemanticRouteContractError, "regular file"):
                store.read(
                    relpath=Path("receipt-link.jsonl"),
                    expected_hash="sha256:" + "a" * 64,
                )

    def test_receipt_sidecar_reports_transient_read_failure_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _Paths(Path(tmp))
            store = SemanticRouteReceiptStore(
                paths=paths,  # type: ignore[arg-type]
                artifacts=ArtifactStore(paths),  # type: ignore[arg-type]
            )
            with self.assertRaises(SemanticRouteReceiptStoreError) as caught:
                store.read(
                    relpath=Path("missing-receipt.jsonl"),
                    expected_hash="sha256:" + "a" * 64,
                )

        self.assertTrue(caught.exception.retryable)


class SemanticRouteFileCacheTests(unittest.TestCase):
    def test_cache_round_trips_closed_decision_and_rejects_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = SemanticRouteFileCache(Path(tmp))
            key = "sha256:" + "a" * 64
            decision = SemanticAdjudicationDecision(
                unit_index=3,
                routes=(
                    SemanticAdjudicatedRoute(
                        key="revenue_and_cost",
                        support_ids=("u3:title",),
                    ),
                ),
            )
            self.assertIsNone(cache.get(key))
            cache.put(key, decision)
            self.assertEqual(cache.get(key), decision)

            path = Path(tmp) / "aa" / f"{'a' * 64}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["cache_key"] = "sha256:" + "b" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(SemanticRouteContractError, "identity"):
                cache.get(key)

    def test_cache_preserves_transient_filesystem_error_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = SemanticRouteFileCache(Path(tmp))
            with (
                patch.object(Path, "lstat", side_effect=OSError("unavailable")),
                self.assertRaisesRegex(OSError, "unavailable"),
            ):
                cache.get("sha256:" + "a" * 64)


if __name__ == "__main__":
    unittest.main()
