"""Records above the historical 8 MiB envelope through the real publication chain.

Two record families under the release publication envelope policy:

* ``request_over_8_mib``: three Units whose whole request exceeds 8 MiB while
  every Unit record stays below 8 MiB;
* ``unit_over_8_mib``: one Unit whose own canonical payload and Unit record
  exceed 8 MiB.

Payloads are valid PostgreSQL text dense in JSON escapes (quotes, backslashes,
short and ``\\uXXXX`` control escapes) and in raw U+2028/U+2029 and
multi-byte UTF-8 (CJK, emoji). The synthetic Units keep the fixture Unit's
locator shape: they bind only to the fixture provider-document hash and its
source pages and claim no provider block, heading, table part or source-text
slice.

The DB-free class runs in default discovery with the real readiness adapter,
immutable store and exact-tree promotion: the whole plan is measured before
the first write, the process is cut right after the durable preparation, and
a fresh adapter reopens the original request from disk and completes the
measured plan under the originally assigned Unit IDs. The managed-scratch
class (``make test-integration``) adds transaction P: a cut after preparation
or after readiness, a restart whose request builder must never run, a lost P
response resolved by winner reload, a second restart after the commit, exact
persisted content, and no new submission, cleanup or ACK evidence and no
duplicate winner, outbox or durable-base row.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import fields, replace
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Event
from typing import Any, cast
import unittest

import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.adapters.db.postgres.atomic_document_publisher_v4 import (
    PostgresAtomicWholeDocumentPublisherV4,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.adapters.storage.atomic_publication_artifact_readiness_v4 import (
    FilesystemAtomicPublicationArtifactReadinessV4,
)
from disclosure_anchor.adapters.storage.immutable_artifact_store import (
    ImmutableArtifactStore,
)
from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
    atomic_publication_request_sha256_v4,
    seal_atomic_publication_request_v4,
    seal_pre_id_unit_publication_v4,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    ATOMIC_PUBLICATION_PREPARATION_FILENAME,
    ATOMIC_PUBLICATION_READINESS_FILENAME,
    decode_atomic_publication_preparation_v1,
)
from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.provider_unit import (
    provider_unit_locator_from_payload,
)
from disclosure_anchor.application.contracts.publication_envelope_policy import (
    PUBLICATION_ENVELOPE_POLICY_V1,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    semantic_adjudication_terminal_v1,
    semantic_route_receipts_file_bytes_v3,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    final_unit_row_sha256_v4,
    lineage_row_sha256_v4,
)
from disclosure_anchor.application.ports.staged_execution import StageNote
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
    V4ClaimWitness,
)
from disclosure_anchor.application.services.atomic_publication_request_factory_v4 import (
    RecoverableAtomicPublicationRequestFactoryV4,
)
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseGuard
from disclosure_anchor.application.use_cases.prepare_and_publish_whole_document_v4 import (
    PrepareAndPublishWholeDocumentV4,
)
from disclosure_anchor.domain.services.unit_hashing import (
    compute_unit_hashes,
    content_hash_aggregate,
    structure_hash_aggregate,
)
from tests.integration import test_prepare_and_publish_whole_document_v4 as publish_scaffold
from tests.integration._remote_parse_v4_factory import (
    V4AuthorityFixture,
    build_atomic_publication_request_v4,
    build_v4_authority_fixture,
)
from tests.integration.test_staged_v4_end_to_end import _LoseFirstPublicationResponse
from tests.unit.test_atomic_publication_artifact_readiness_adapter_v4 import (
    _FailAfterCreateStore,
)


MIB = 1024 * 1024
POLICY = PUBLICATION_ENVELOPE_POLICY_V1
# The release's finite byte domain; the winner budget stays the applied CHECK.
SELECTED_POLICY_BYTES = {
    "request_bytes": 64 * MIB,
    "unit_bytes": 64 * MIB,
    "preparation_bytes": 160 * MIB,
    "readiness_bytes": 8 * MIB,
    "winner_bytes": 8 * MIB,
    "snapshot_bytes": 128 * MIB,
    "semantic_bytes": 64 * MIB,
}
# Raw UTF-8 text bytes per Unit; canonical escaping grows a Unit record ~1.26x.
FAMILIES: dict[str, tuple[int, ...]] = {
    "request_over_8_mib": (int(2.8 * MIB),) * 3,
    "unit_over_8_mib": (int(7.5 * MIB),),
}
WINNER_CHECK = "ck_atomic_publication_winner_v4_identity"
# PostgreSQL deparses BETWEEN into a pair of comparisons.
WINNER_CHECK_BOUND = rf"winner_byte_count (?:<=|BETWEEN 1 AND) {8 * MIB}\b"


def _canonical_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _text_digest(value: str) -> str:
    # Large texts compare by exact digest so a failure never diffs megabytes.
    return _sha256(value.encode("utf-8"))


def _large_text(byte_count: int, *, salt: str) -> str:
    """Deterministic valid text: no U+0000 or surrogate, every segment distinct."""

    segments: list[str] = []
    size = 0
    index = 0
    while size < byte_count:
        segment = (
            f"No.{index:07d}/{salt} 第{index}段“引号” \"quote\" \\back\\slash/ "
            "\t tab \n line \r cr \u0001 \u001f ctl    \u007f "
            "é ñ ß 表格数据 😀🧾\n"
        )
        segments.append(segment)
        size += len(segment.encode("utf-8"))
        index += 1
    return "".join(segments)


def _unsealed(value: Any, derived: str) -> dict[str, Any]:
    return {item.name: getattr(value, item.name) for item in fields(value) if item.name != derived}


def _large_request(fixture: V4AuthorityFixture, raw_byte_counts: tuple[int, ...]) -> AtomicPublicationRequestV4:
    """Replace the fixture request's one Unit with large synthetic text Units.

    Each Unit copies the fixture Unit (keys, quality, locator shape) and cites
    only fixture source pages; each route reuses the fixture's fallback
    receipt. Hashes, routed drafts, aggregates and the semantic terminal are
    recomputed and the product sealers close the request.
    """

    base = build_atomic_publication_request_v4(fixture)
    base_unit = base.units[0]
    base_route = base.semantic_route_receipts[0]
    pages = tuple(range(1, base.source_page_count + 1))
    units = []
    routes = []
    for index, byte_count in enumerate(raw_byte_counts, start=1):
        payload = {"text": _large_text(byte_count, salt=f"unit-{index}")}
        page_numbers = pages if len(raw_byte_counts) == 1 else (pages[(index - 1) % len(pages)],)
        hashes = compute_unit_hashes(
            payload_kind=base_unit.payload_kind,
            payload=payload,
            title=base_unit.title,
            heading_path=list(base_unit.heading_path),
            semantic_keys=None if base_unit.semantic_keys is None else list(base_unit.semantic_keys),
            section_keys=None if base_unit.section_keys is None else list(base_unit.section_keys),
            quality_status=base_unit.quality_status,
            applicability=base_unit.applicability,
            order_index=index,
        )
        locator = json.loads(base_unit.canonical_artifact_locator_json)
        locator["unit_index"] = index - 1
        locator_json = _canonical_text(locator)
        unit = seal_pre_id_unit_publication_v4(
            **{
                **_unsealed(base_unit, "routed_draft_sha256"),
                "unit_index": index,
                "canonical_payload_json": _canonical_text(payload),
                "content_hash": hashes.content_hash,
                "structure_hash": hashes.structure_hash,
                "query_projection_hash": hashes.query_projection_hash,
                "page_no": page_numbers[0],
                "page_numbers": page_numbers,
                "canonical_artifact_locator_json": locator_json,
                "provider_locator_sha256": _sha256(locator_json.encode("utf-8")),
            }
        )
        units.append(unit)
        routes.append(
            replace(
                base_route,
                unit_order_index=index,
                provider_locator_sha256=unit.provider_locator_sha256,
                routed_draft_sha256=unit.routed_draft_sha256,
            )
        )
    terminal = semantic_adjudication_terminal_v1(tuple(route.receipt for route in routes))
    projection = json.loads(base.processing_run_projection_json)
    projection.update(
        unit_count=len(units),
        content_hash_aggregate=content_hash_aggregate([unit.content_hash for unit in units]),
        structure_hash_aggregate=structure_hash_aggregate([unit.structure_hash for unit in units]),
        semantic_route_receipts_sha256=_sha256(semantic_route_receipts_file_bytes_v3(tuple(routes))),
        semantic_adjudication_status=terminal.status,
        semantic_adjudication_summary=terminal.summary,
        semantic_degraded_unit_count=terminal.degraded_unit_count,
        semantic_failover_group_count=terminal.failover_group_count,
    )
    projection_json = _canonical_text(projection)
    return seal_atomic_publication_request_v4(
        **{
            **_unsealed(base, "request_sha256"),
            "units": tuple(units),
            "semantic_route_receipts": tuple(routes),
            "processing_run_projection_json": projection_json,
            "processing_run_projection_sha256": _sha256(projection_json.encode("utf-8")),
        }
    )


class _NoRebuild:
    """A replay must reopen the durable preparation, never rebuild the request."""

    def build(self, **_: object) -> object:
        raise AssertionError("replay rebuilt the publication request instead of reopening it")


class _PlanNotes:
    """Record each whole-plan measurement with the durable writes before it."""

    def __init__(self, store: _FailAfterCreateStore) -> None:
        self.store = store
        self.measured: list[tuple[dict[str, Any], int]] = []
        self.failures: list[BaseException] = []

    def note(self, record: StageNote) -> None:
        if record.kind == "publication_plan_measured":
            self.measured.append((dict(record.scalars), len(self.store.created)))

    def record_failure(self, error: BaseException) -> None:
        self.failures.append(error)


class _LargeRecordAssertions(unittest.TestCase):
    def assert_family(self, family: str, request: AtomicPublicationRequestV4) -> None:
        units = [len(unit.canonical_bytes) for unit in request.units]
        payloads = [len(unit.canonical_payload_json.encode("utf-8")) for unit in request.units]
        whole = len(request.canonical_bytes)
        if family == "request_over_8_mib":
            self.assertGreater(whole, 8 * MIB)
            self.assertLess(max(units), 8 * MIB)
        else:
            self.assertEqual(len(units), 1)
            self.assertGreater(payloads[0], 8 * MIB)
            self.assertGreater(units[0], 8 * MIB)
        self.assertLessEqual(whole, POLICY.request_bytes)
        self.assertLessEqual(max(units), POLICY.unit_bytes)
        pages = set(range(1, request.source_page_count + 1))
        for unit in request.units:
            locator = provider_unit_locator_from_payload(json.loads(unit.canonical_artifact_locator_json))
            self.assertEqual(locator.provider_document_sha256, request.upstream_evidence.provider_document_sha256)
            self.assertEqual(
                (locator.heading_chain, locator.parts, locator.evidence_only_block_source_indices,
                 locator.unbound_table_parts, locator.evidence_artifacts, locator.search_targets,
                 locator.source_text_reconciliations, locator.source_quality_findings),
                ((),) * 8,
            )
            self.assertTrue(set(unit.page_numbers) <= pages)

    def assert_exact_derived_files(
        self, root: Path, request: AtomicPublicationRequestV4, preparation: Any, unit_ids: tuple[str, ...],
    ) -> None:
        for plan in (
            preparation.provider_document_plan,
            preparation.document_unit_snapshot_plan,
            preparation.semantic_route_receipts_plan,
        ):
            payload = (root / plan.relpath).read_bytes()
            self.assertEqual((_sha256(payload), len(payload)), (plan.sha256, plan.byte_count))
        # Raw U+2028/U+2029 stay unescaped in JSONL, so rows split on "\n" only.
        lines = (root / preparation.document_unit_snapshot_plan.relpath).read_bytes().split(b"\n")
        self.assertEqual(lines[-1], b"")
        rows = [json.loads(line) for line in lines[:-1]]
        self.assertEqual([row["asset_id"] for row in rows], list(unit_ids))
        self.assertEqual(
            [_text_digest(_canonical_text(row["payload"])) for row in rows],
            [_text_digest(unit.canonical_payload_json) for unit in request.units],
        )
        self.assertEqual(
            [_canonical_text(row["artifact_locator"]) for row in rows],
            [unit.canonical_artifact_locator_json for unit in request.units],
        )
        self.assertEqual(len(tuple(root.rglob(ATOMIC_PUBLICATION_READINESS_FILENAME))), 1)


class LargePublicationPolicyAndReplayTests(_LargeRecordAssertions):
    """DB-free: the release envelope and replay from the durable preparation."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.fixture = build_v4_authority_fixture()
        self.checkpoint = self.fixture.local_materialized
        self.materialized = MaterializedProviderDocumentV4(
            receipt=self.fixture.local_materialization_receipt,
            intent=self.fixture.materialization_intent,
            provider_envelope=self.fixture.provider_envelope,
            manifest=self.fixture.materialization_manifest,
        )
        self.claim = V4ClaimWitness(
            attempt_id=self.checkpoint.attempt_id,
            fence_identity=self.checkpoint.fence_identity,
            state=self.checkpoint.state,
            lifecycle_version=self.checkpoint.lifecycle_version,
            checkpoint_sha256=self.checkpoint.sha256,
            claim_owner_identity="worker-1",
            claim_generation=1,
        )
        output_files = {
            LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME: self.fixture.materialization_manifest.canonical_bytes,
            PROVIDER_DOCUMENT_FILENAME: provider_document_envelope_to_bytes(self.fixture.provider_envelope),
            **dict(self.fixture.parser_artifact_files),
        }
        source = self.root / self.fixture.materialization_intent.output_relpath
        for relpath, payload in output_files.items():
            path = source / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        self.promotion = publish_scaffold._ExactTreePromotion(
            root=self.root,
            expected_files=output_files,
            inventory_sha256=self.fixture.local_materialization_receipt.output_files_sha256,
            byte_count=self.fixture.local_materialization_receipt.output_byte_count,
        )

    def test_release_policy_is_the_selected_finite_byte_domain(self) -> None:
        self.assertEqual({name: getattr(POLICY, name) for name in SELECTED_POLICY_BYTES}, SELECTED_POLICY_BYTES)
        table = cast(sa.Table, models.AtomicPublicationWinnerV4.__table__)
        check = next(
            constraint for constraint in table.constraints
            if isinstance(constraint, sa.CheckConstraint) and constraint.name == WINNER_CHECK
        )
        # The winner budget is the model's applied CHECK, not a new bound.
        self.assertRegex(str(check.sqltext), WINNER_CHECK_BOUND)

    def test_request_over_8_mib_is_measured_then_replayed_from_its_durable_preparation(self) -> None:
        self._cut_after_preparation_then_replay("request_over_8_mib")

    def test_unit_over_8_mib_is_measured_then_replayed_from_its_durable_preparation(self) -> None:
        self._cut_after_preparation_then_replay("unit_over_8_mib")

    def _cut_after_preparation_then_replay(self, family: str) -> None:
        request = _large_request(self.fixture, FAMILIES[family])
        self.assert_family(family, request)
        paths = publish_scaffold._Paths(self.root)
        store = _FailAfterCreateStore(
            ImmutableArtifactStore(paths),  # type: ignore[arg-type]
            fail_name=ATOMIC_PUBLICATION_PREPARATION_FILENAME,
        )
        notes = _PlanNotes(store)
        first = FilesystemAtomicPublicationArtifactReadinessV4(
            paths=paths, immutable_store=store, output_promotion=self.promotion,  # type: ignore[arg-type]
        )
        arguments: dict[str, Any] = dict(
            checkpoint=self.checkpoint, materialized=self.materialized, claim=self.claim,
            claim_guard=publish_scaffold._Guard(),
        )
        with self.assertRaisesRegex(RuntimeError, "after"):
            first.prepare_or_replay(
                request=request,
                stage_guard=StageLeaseGuard(
                    deadline_monotonic=60.0, _revoked=Event(), _monotonic=lambda: 0.0, observer=notes,
                ),
                **arguments,
            )
        preparation_relpath, readiness_relpath = first._authority_paths(request)
        durable = (self.root / preparation_relpath).read_bytes()
        # One whole-plan measurement before the first write; the cut left the
        # preparation as the only publication record.
        self.assertEqual(notes.failures, [])
        self.assertEqual(len(notes.measured), 1)
        measured, writes_before = notes.measured[0]
        self.assertEqual(writes_before, 0)
        self.assertEqual(store.created, [ATOMIC_PUBLICATION_PREPARATION_FILENAME])
        self.assertFalse((self.root / readiness_relpath).exists())
        self.assertEqual(
            (measured["request_bytes"], measured["preparation_bytes"]),
            (len(request.canonical_bytes), len(durable)),
        )
        self.assertLessEqual(len(durable), POLICY.preparation_bytes)
        prepared = decode_atomic_publication_preparation_v1(durable)
        self.assertEqual(_text_digest(prepared.canonical_request_json), _sha256(request.canonical_bytes))
        self.assertEqual(
            (prepared.request_byte_count, prepared.request_sha256),
            (len(request.canonical_bytes), request.request_sha256),
        )
        original_ids = tuple(item.asset_id for item in prepared.unit_bindings)

        restarted = FilesystemAtomicPublicationArtifactReadinessV4(
            paths=paths,  # type: ignore[arg-type]
            immutable_store=ImmutableArtifactStore(paths),  # type: ignore[arg-type]
            output_promotion=self.promotion,
        )
        reopened = restarted.reopen_prepared_request(checkpoint=self.checkpoint, materialized=self.materialized)
        assert reopened is not None
        self.assertTrue(reopened == request)
        self.assertEqual(_sha256(reopened.canonical_bytes), _sha256(request.canonical_bytes))
        self.assertEqual(atomic_publication_request_sha256_v4(reopened), request.request_sha256)
        reference = restarted.prepare_or_replay(
            request=reopened,
            stage_guard=StageLeaseGuard(deadline_monotonic=60.0, _revoked=Event(), _monotonic=lambda: 0.0),
            **arguments,
        )
        witness = restarted.verify_ready(reference=reference, expected_request=request)
        self.assertEqual(_sha256((self.root / preparation_relpath).read_bytes()), _sha256(durable))
        self.assertEqual(tuple(item.asset_id for item in witness.preparation.unit_bindings), original_ids)
        # The plan measured before the cut is exactly the plan the replay wrote.
        self.assertEqual(
            {name: measured[name] for name in (
                "snapshot_bytes", "semantic_bytes", "readiness_bytes", "units", "previous_units", "policy",
            )},
            {
                "snapshot_bytes": witness.preparation.document_unit_snapshot_plan.byte_count,
                "semantic_bytes": witness.preparation.semantic_route_receipts_plan.byte_count,
                "readiness_bytes": reference.manifest_byte_count,
                "units": len(request.units),
                "previous_units": 0,
                "policy": POLICY.identity,
            },
        )
        self.assertLessEqual(measured["winner_upper_bound_bytes"], POLICY.winner_bytes)
        self.assert_exact_derived_files(self.root, request, witness.preparation, original_ids)


class LargePublicationTransactionPScratchTests(_LargeRecordAssertions):
    """Managed scratch PostgreSQL and real files: cut, restart, lost P, reload, replay."""

    def setUp(self) -> None:
        # The existing P scaffold owns the scratch rows, output tree and teardown.
        self.scaffold = publish_scaffold.PrepareAndPublishWholeDocumentV4IntegrationTests(
            "test_real_files_and_transaction_p_close_one_v2_winner"
        )
        self.scaffold.setUp()
        self.addCleanup(self.scaffold.tearDown)
        self.engine = self.scaffold.engine
        self.fixture = self.scaffold.fixture
        self.root = self.scaffold.root

    def test_request_over_8_mib_replays_after_readiness_cut_and_lost_p_response(self) -> None:
        self._cut_restart_publish("request_over_8_mib", cut=ATOMIC_PUBLICATION_READINESS_FILENAME)

    def test_unit_over_8_mib_replays_after_preparation_cut_and_lost_p_response(self) -> None:
        self._cut_restart_publish("unit_over_8_mib", cut=ATOMIC_PUBLICATION_PREPARATION_FILENAME)

    def _cut_restart_publish(self, family: str, *, cut: str) -> None:
        request = _large_request(self.fixture, FAMILIES[family])
        self.assert_family(family, request)
        run_id = self.fixture.processing_run_id
        paths = publish_scaffold._Paths(self.root)
        promotion = publish_scaffold._ExactTreePromotion(
            root=self.root,
            expected_files=self.scaffold.output_files,
            inventory_sha256=self.fixture.local_materialization_receipt.output_files_sha256,
            byte_count=self.fixture.local_materialization_receipt.output_byte_count,
        )
        publisher = PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            claim = uow.remote_parse_v4.load(self.fixture.attempt_id).claim_witness
        before = self._authority_rows()
        arguments: dict[str, Any] = dict(
            checkpoint=self.fixture.local_materialized,
            materialized=self.scaffold.materialized,
            claim=claim,
            claim_guard=publish_scaffold._Guard(),
            stage_guard=publish_scaffold._StageGuard(),
        )

        cut_readiness = FilesystemAtomicPublicationArtifactReadinessV4(
            paths=paths,  # type: ignore[arg-type]
            immutable_store=_FailAfterCreateStore(
                ImmutableArtifactStore(paths), fail_name=cut,  # type: ignore[arg-type]
            ),
            output_promotion=promotion,
        )
        with self.assertRaisesRegex(RuntimeError, "after"):
            self._use_case(cut_readiness, publisher, publish_scaffold._RequestBuilder(request)).execute(**arguments)
        self.assertIsNone(publisher.reload_commit_winner_by_processing_run_id(processing_run_id=run_id))
        preparation_relpath, _ = cut_readiness._authority_paths(request)
        durable = (self.root / preparation_relpath).read_bytes()
        original_ids = tuple(item.asset_id for item in decode_atomic_publication_preparation_v1(durable).unit_bindings)
        durable_sha256 = _sha256(durable)
        del durable

        # Restart: fresh adapter and factory, the builder must never run, and
        # the first P response is lost after its commit.
        restarted = FilesystemAtomicPublicationArtifactReadinessV4(
            paths=paths,  # type: ignore[arg-type]
            immutable_store=ImmutableArtifactStore(paths),  # type: ignore[arg-type]
            output_promotion=promotion,
        )
        transport = _LoseFirstPublicationResponse(publisher)
        winner = self._use_case(restarted, transport, _NoRebuild()).execute(**arguments)
        self.assertTrue(transport.lost)
        # A second restart after the commit replays to the same winner.
        replayed = self._use_case(
            FilesystemAtomicPublicationArtifactReadinessV4(
                paths=paths,  # type: ignore[arg-type]
                immutable_store=ImmutableArtifactStore(paths),  # type: ignore[arg-type]
                output_promotion=promotion,
            ),
            publisher,
            _NoRebuild(),
        ).execute(**arguments)
        self.assertTrue(replayed == winner)

        self.assertEqual(_sha256((self.root / preparation_relpath).read_bytes()), durable_sha256)
        self.assertEqual((winner.request_sha256, winner.winner_row_version), (request.request_sha256, 2))
        self.assertTrue(publisher.reload_commit_winner_by_processing_run_id(processing_run_id=run_id) == winner)
        assert winner.artifact_readiness is not None
        witness = restarted.verify_ready(
            reference=winner.artifact_readiness, expected_request=request, expected_winner=winner,
        )
        self.assertEqual(tuple(item.asset_id for item in winner.unit_assets), original_ids)
        self.assert_exact_derived_files(self.root, request, witness.preparation, original_ids)
        for unit, asset in zip(request.units, winner.unit_assets, strict=True):
            self.assertEqual(
                (asset.final_unit_row_sha256, asset.lineage_row_sha256),
                (
                    final_unit_row_sha256_v4(request=request, unit_index=unit.unit_index, asset_id=asset.asset_id),
                    lineage_row_sha256_v4(request=request, unit_index=unit.unit_index, asset_id=asset.asset_id),
                ),
            )
        self._assert_persisted_publication(request, winner, original_ids, before)

    def _use_case(self, readiness: Any, publisher: Any, builder: Any) -> PrepareAndPublishWholeDocumentV4:
        return PrepareAndPublishWholeDocumentV4(
            uow_factory=lambda: SqlAlchemyUnitOfWork(engine=self.engine),
            publication_requests=RecoverableAtomicPublicationRequestFactoryV4(
                readiness=readiness, new_request_builder=builder,
            ),
            readiness=readiness,
            publisher=publisher,
        )

    def _authority_rows(self) -> dict[str, Any]:
        """Submission, secret, cleanup and ACK evidence of the fixture attempt."""

        with self.engine.connect() as conn:
            evidence = conn.execute(sa.text(
                "SELECT evidence_kind,evidence_sha256 FROM disclosure_ops.remote_parse_v4_evidence "
                "WHERE attempt_id=:attempt ORDER BY evidence_kind,evidence_sha256"
            ), {"attempt": self.fixture.attempt_id}).all()
            secrets = conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_ops.remote_parse_v4_secret WHERE attempt_id=:attempt"
            ), {"attempt": self.fixture.attempt_id}).scalar_one()
        return {"evidence": [tuple(row) for row in evidence], "secrets": secrets}

    def _assert_persisted_publication(
        self, request: AtomicPublicationRequestV4, winner: Any, unit_ids: tuple[str, ...], before: dict[str, Any],
    ) -> None:
        run_id = request.identity.processing_run_id
        attempt_id = request.identity.attempt_id
        with self.engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT asset_id,order_index,payload_kind,heading_path,title,semantic_keys,section_keys,payload,"
                "content_hash,structure_hash,quality_status,applicability,page_no,query_projection_hash,"
                "artifact_locator,provider_document_id FROM disclosure_core.document_unit "
                "WHERE processing_run_id=:run ORDER BY order_index"
            ), {"run": run_id}).mappings().all()
            winners = conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_ops.atomic_publication_winner_v4 "
                "WHERE processing_run_id=:run OR attempt_id=:attempt"
            ), {"run": run_id, "attempt": attempt_id}).scalar_one()
            events = conn.execute(sa.text(
                "SELECT event_id,event_kind,asset_id FROM disclosure_ops.outbox_event WHERE processing_run_id=:run"
            ), {"run": run_id}).mappings().all()
            bases = conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_ops.durable_publish_base WHERE processing_run_id=:run"
            ), {"run": run_id}).scalar_one()
            head = conn.execute(sa.text(
                "SELECT state,is_current FROM disclosure_ops.remote_parse_attempt WHERE attempt_id=:attempt"
            ), {"attempt": attempt_id}).mappings().one()
            document = conn.execute(sa.text(
                "SELECT status,current_processing_run_id FROM disclosure_core.document WHERE document_id=:document"
            ), {"document": request.identity.document_id}).mappings().one()
            check = conn.execute(sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname=:name"
            ), {"name": WINNER_CHECK}).scalar_one()
        self.assertEqual([row["asset_id"] for row in rows], list(unit_ids))
        for unit, row in zip(request.units, rows, strict=True):
            # Exact canonical content and every hash survive JSONB storage.
            self.assertEqual(_text_digest(_canonical_text(row["payload"])), _text_digest(unit.canonical_payload_json))
            self.assertEqual(_canonical_text(row["artifact_locator"]), unit.canonical_artifact_locator_json)
            self.assertEqual(
                (row["order_index"], row["payload_kind"], tuple(row["heading_path"]), row["title"],
                 None if row["semantic_keys"] is None else tuple(row["semantic_keys"]),
                 None if row["section_keys"] is None else tuple(row["section_keys"]),
                 row["content_hash"], row["structure_hash"], row["quality_status"], row["applicability"],
                 row["page_no"], row["query_projection_hash"], row["provider_document_id"]),
                (unit.unit_index, unit.payload_kind, unit.heading_path, unit.title, unit.semantic_keys,
                 unit.section_keys, unit.content_hash, unit.structure_hash, unit.quality_status,
                 unit.applicability, unit.page_no, unit.query_projection_hash, unit.provider_document_id),
            )
            recomputed = compute_unit_hashes(
                payload_kind=row["payload_kind"], payload=row["payload"], title=row["title"],
                heading_path=list(row["heading_path"]), semantic_keys=row["semantic_keys"],
                section_keys=row["section_keys"], quality_status=row["quality_status"],
                applicability=row["applicability"], order_index=row["order_index"],
            )
            self.assertEqual(
                (recomputed.content_hash, recomputed.structure_hash, recomputed.query_projection_hash),
                (unit.content_hash, unit.structure_hash, unit.query_projection_hash),
            )
        # One winner and durable base; one outbox row per created Unit plus
        # the published run, all under the originally prepared IDs.
        self.assertEqual((winners, bases), (1, 1))
        self.assertEqual(
            Counter(event["event_kind"] for event in events),
            Counter({"document_unit_created": len(request.units), "processing_run_published": 1}),
        )
        self.assertEqual(len({event["event_id"] for event in events}), len(events))
        self.assertEqual({event["asset_id"] for event in events if event["asset_id"]}, set(unit_ids))
        self.assertEqual(winner.inserted_count, len(request.units))
        self.assertLessEqual(len(winner.canonical_bytes), POLICY.winner_bytes)
        self.assertRegex(check, WINNER_CHECK_BOUND)
        # P holds no provider port: the attempt keeps exactly its submission
        # and secret evidence and gains no cleanup or ACK record.
        self.assertEqual(dict(head), {"state": "publish_committed", "is_current": True})
        self.assertEqual(self._authority_rows(), before)
        self.assertEqual(dict(document), {"status": "published", "current_processing_run_id": run_id})


if __name__ == "__main__":
    unittest.main()
