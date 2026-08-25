from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from typing import get_args
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.semantic_route_store import (
    SemanticRouteFileCache,
    SemanticRouteGroupFileCache,
    SemanticRouteReceiptStore,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_FALLBACK_KEY,
    SEMANTIC_FAILOVER_POLICY_VERSION,
    SEMANTIC_ROUTE_RECEIPT_VERSION,
    SEMANTIC_ROUTER_VERSION,
    SemanticAdjudicationReceipt,
    SemanticAdjudicatorMetadata,
    SemanticAdjudicationDecision,
    SemanticAdjudicatedRoute,
    SemanticProviderIdentity,
    SemanticProviderAttempt,
    SemanticRouteContractError,
    SemanticRouteEvidence,
    SemanticRouteEvidenceKind,
    SemanticRouteReceipt,
    SemanticRouteReceiptRow,
    semantic_route_receipt_row_to_payload,
)
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicationCacheEntry,
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

    def test_receipt_sidecar_round_trips_v2_provider_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _Paths(Path(tmp))
            store = SemanticRouteReceiptStore(
                paths=paths,  # type: ignore[arg-type]
                artifacts=ArtifactStore(paths),  # type: ignore[arg-type]
            )
            identity = _group_entry().provider
            response_hash = "sha256:" + "e" * 64
            attempt = SemanticProviderAttempt(
                ordinal=1,
                provider=identity,
                outcome="succeeded",
                cache_key="sha256:" + "a" * 64,
                response_sha256=response_hash,
            )
            receipt = SemanticRouteReceipt(
                contract_version=SEMANTIC_ROUTE_RECEIPT_VERSION,
                taxonomy_version="semantic-test.v1",
                router_version=SEMANTIC_ROUTER_VERSION,
                input_hash="sha256:" + "1" * 64,
                candidate_keys=("forecast_summary",),
                semantic_keys=("forecast_summary",),
                decision_source="model",
                evidence=(
                    SemanticRouteEvidence(
                        key="forecast_summary",
                        kinds=("model_adjudicated",),
                        source_ids=("u0:title",),
                    ),
                ),
                adjudication=SemanticAdjudicationReceipt(
                    policy_version=SEMANTIC_FAILOVER_POLICY_VERSION,
                    group_hash="sha256:" + "b" * 64,
                    attempts=(attempt,),
                    actual_result_attempt=1,
                    actual_result_identity=identity,
                    group_response_sha256=response_hash,
                ),
            )
            rows = (
                SemanticRouteReceiptRow(
                    asset_id="asset_1",
                    order_index=1,
                    receipt=receipt,
                ),
            )

            result = store.write(
                relpath=Path("derived/test/semantic_route_receipts.v2.jsonl"),
                rows=rows,
            )

            self.assertEqual(
                store.read(
                    relpath=Path("derived/test/semantic_route_receipts.v2.jsonl"),
                    expected_hash=result.artifact_hash,
                ),
                rows,
            )

    def test_receipt_sidecar_reads_historical_router_but_rejects_bad_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _Paths(Path(tmp))
            store = SemanticRouteReceiptStore(
                paths=paths,  # type: ignore[arg-type]
                artifacts=ArtifactStore(paths),  # type: ignore[arg-type]
            )
            row = SemanticRouteReceiptRow(
                asset_id="asset_1",
                order_index=1,
                receipt=SemanticRouteReceipt(
                    contract_version=SEMANTIC_ROUTE_RECEIPT_VERSION,
                    taxonomy_version="semantic-test.v1",
                    router_version="semantic_router.v98",
                    input_hash="sha256:" + "1" * 64,
                    candidate_keys=(),
                    semantic_keys=(),
                    decision_source="fallback",
                    evidence=(),
                ),
            )
            relpath = Path("derived/test/historical-v2.jsonl")
            payload = semantic_route_receipt_row_to_payload(row)
            raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
            paths.data_path(relpath).parent.mkdir(parents=True)
            paths.data_path(relpath).write_bytes(raw)
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()

            self.assertEqual(store.read(relpath=relpath, expected_hash=digest), (row,))

            for invalid in ("semantic_router.v0", "semantic_router.v01", "v98"):
                with self.subTest(invalid=invalid):
                    bad = json.loads(raw)
                    bad["semantic_route"]["router_version"] = invalid
                    bad_raw = (json.dumps(bad, sort_keys=True) + "\n").encode()
                    paths.data_path(relpath).write_bytes(bad_raw)
                    bad_hash = "sha256:" + hashlib.sha256(bad_raw).hexdigest()
                    with self.assertRaisesRegex(
                        SemanticRouteContractError,
                        "identity is invalid",
                    ):
                        store.read(relpath=relpath, expected_hash=bad_hash)

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


def _group_entry(*, response_hash: str | None = None) -> SemanticAdjudicationCacheEntry:
    key = "sha256:" + "a" * 64
    return SemanticAdjudicationCacheEntry(
        cache_key=key,
        group_hash="sha256:" + "b" * 64,
        provider=SemanticProviderIdentity(
            provider_id="luna-primary",
            provider="openai",
            adapter_kind="codex_cli",
            adapter_version="codex_cli.v4",
            canonical_model="gpt-5.6-luna",
            inference_profile="low",
            prompt_version="semantic-test.v1",
            prompt_sha256="sha256:" + "c" * 64,
            output_schema_version="semantic-schema.test",
            output_schema_sha256="sha256:" + "d" * 64,
        ),
        decisions=(
            SemanticAdjudicationDecision(
                unit_index=3,
                routes=(
                    SemanticAdjudicatedRoute(
                        key="revenue_and_cost",
                        support_ids=("u3:title",),
                    ),
                ),
            ),
        ),
        response_sha256=response_hash or ("sha256:" + "e" * 64),
    )


class SemanticRouteGroupFileCacheTests(unittest.TestCase):
    def test_group_cache_round_trips_and_rejects_nondeterminism(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = SemanticRouteGroupFileCache(Path(tmp))
            entry = _group_entry()

            self.assertIsNone(cache.get(entry.cache_key))
            cache.put(entry)
            self.assertEqual(cache.get(entry.cache_key), entry)
            cache.put(entry)
            with self.assertRaisesRegex(SemanticRouteContractError, "nondeterministic"):
                cache.put(_group_entry(response_hash="sha256:" + "f" * 64))

    def test_malformed_bytes_are_quarantined_and_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = SemanticRouteGroupFileCache(root)
            entry = _group_entry()
            path = root / "aa" / f"{'a' * 64}.json"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"not-json")

            self.assertIsNone(cache.get(entry.cache_key))
            self.assertFalse(path.exists())
            quarantined = tuple(path.parent.glob(f"{path.name}.corrupt.*"))
            self.assertEqual(len(quarantined), 1)
            cache.put(entry)
            self.assertEqual(cache.get(entry.cache_key), entry)

    def test_symlink_and_identity_conflict_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = SemanticRouteGroupFileCache(root)
            entry = _group_entry()
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            path = root / "aa" / f"{'a' * 64}.json"
            path.parent.mkdir(parents=True)
            path.symlink_to(target)
            with self.assertRaisesRegex(SemanticRouteContractError, "unsafe"):
                cache.get(entry.cache_key)

            path.unlink()
            cache.put(entry)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["cache_key"] = "sha256:" + "f" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(SemanticRouteContractError, "key drifted"):
                cache.get(entry.cache_key)


if __name__ == "__main__":
    unittest.main()
