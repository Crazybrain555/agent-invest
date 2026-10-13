"""One fixed publication envelope: policy, whole-plan measurement, winner bound and boundary families."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.adapters.storage.atomic_publication_artifact_readiness_v4 import (
    FilesystemAtomicPublicationArtifactReadinessV4,
)
from disclosure_anchor.adapters.storage.immutable_artifact_store import ImmutableArtifactStore
from disclosure_anchor.application.contracts import atomic_document_publication_v4 as request_contract
from disclosure_anchor.application.contracts import (
    atomic_publication_artifact_readiness_v4 as readiness_contract,
)
from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
    PublicationEnvelopeExceededError,
    WholeDocumentPublicationV4Error,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactConflict,
    AtomicPublicationArtifactReadinessError,
    AtomicPublicationUnitBindingV4,
    decode_atomic_publication_preparation_v1,
    decode_atomic_publication_readiness_v1,
    document_unit_snapshot_file_bytes_v1,
)
from disclosure_anchor.application.contracts.publication_envelope_policy import (
    ATOMIC_PUBLICATION_WINNER_DB_MAX_BYTES,
    PUBLICATION_BYTE_BOUNDS,
    PUBLICATION_ENVELOPE_POLICY_V1,
    PUBLICATION_RECORD_BUDGETS,
    PublicationCapacityFactV1,
    PublicationEnvelopePolicyV1,
    canonical_publication_json,
)
from disclosure_anchor.application.contracts.semantic_routes import semantic_route_receipts_file_bytes_v3
from disclosure_anchor.application.ports import atomic_document_publisher_v4 as publisher_contract
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationWinnerV4,
    atomic_publication_winner_byte_upper_bound_v4,
    build_atomic_publication_outbox_events_v4,
    decode_atomic_publication_winner_v4,
    seal_atomic_publication_winner_v4,
    seal_published_outbox_event_v4,
    seal_unit_asset_winners_v4,
    validate_atomic_publication_artifacts_ready_v4,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
    V4ClaimWitness,
)
from disclosure_anchor.application.services.publication_envelope_plan_v4 import (
    measure_atomic_publication_plan_v4,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CAPACITY_HOLD_BYTE_BOUNDS,
    CapacityHoldDetail,
    StageLeaseGuard,
)
from tests.unit._publication_family_fixture import (
    canonical_text,
    family_unit_ids,
    publication_family_request,
)
from tests.unit.test_atomic_document_publication_v4 import (
    _artifact_preparation,
    _artifact_readiness,
    _previous_active_unit,
    _publication_materialized_evidence,
    _request,
    _winner,
)
from tests.unit.test_atomic_publication_artifact_readiness_adapter_v4 import (
    _Guard,
    _Paths,
    _Promotion,
)

MIB = 1024 * 1024
POLICY = PUBLICATION_ENVELOPE_POLICY_V1
COMMIT_TIME = datetime(2026, 9, 29, 9, 22, 29, 123456, tzinfo=timezone.utc)


def _policy(**budgets: int) -> PublicationEnvelopePolicyV1:
    values = {item.name: getattr(POLICY, item.name) for item in fields(POLICY) if item.name != "contract_version"}
    return PublicationEnvelopePolicyV1(**{**values, **budgets})


class PublicationEnvelopePolicyTests(unittest.TestCase):
    def test_one_frozen_policy_bounds_every_record_kind_below_the_database_check(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            POLICY.request_bytes = 1  # type: ignore[misc]
        # The release's source-fixed budgets, in MiB.
        self.assertEqual(
            {name: value // MIB for name, value in asdict(POLICY).items() if name != "contract_version"},
            {"request_bytes": 64, "unit_bytes": 64, "preparation_bytes": 160, "readiness_bytes": 8,
             "winner_bytes": 8, "snapshot_bytes": 128, "semantic_bytes": 64},
        )
        self.assertEqual(
            POLICY.identity, "sha256:" + hashlib.sha256(canonical_publication_json(asdict(POLICY))).hexdigest(),
        )
        for record_kind, budget in PUBLICATION_RECORD_BUDGETS.items():
            self.assertEqual(POLICY.limit(record_kind), getattr(POLICY, budget), record_kind)
        with self.assertRaisesRegex(ValueError, "closed vocabulary"):
            POLICY.limit("provider_document")
        # The one database fact the policy mirrors: the applied 0057 CHECK.
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions.0057_remote_parse_v4_authority"
        )
        self.assertEqual(
            (ATOMIC_PUBLICATION_WINNER_DB_MAX_BYTES, models._MAX_WINNER_BYTES, migration._MAX_WINNER_BYTES),
            (8 * MIB,) * 3,
        )
        self.assertLessEqual(POLICY.winner_bytes, ATOMIC_PUBLICATION_WINNER_DB_MAX_BYTES)
        self.assertLessEqual(POLICY.unit_bytes, POLICY.request_bytes)
        for invalid, message in (
            ({"unit_bytes": POLICY.request_bytes + 1}, "Unit record cannot be larger"),
            ({"winner_bytes": ATOMIC_PUBLICATION_WINNER_DB_MAX_BYTES + 1}, "database CHECK"),
            ({"snapshot_bytes": 0}, "outside its range"),
            ({"semantic_bytes": True}, "outside its range"),
            ({"preparation_bytes": (1 << 30) + 1}, "outside its range"),
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, message):
                _policy(**invalid)
        with self.assertRaisesRegex(ValueError, "contract"):
            replace(POLICY, contract_version="publication-envelope-policy.v2")

    def test_capacity_fact_is_closed_and_fits_the_coordinator_hold_detail(self) -> None:
        self.assertEqual(PUBLICATION_BYTE_BOUNDS, {"exact", "lower_bound", "upper_bound"})
        # The backend hands every fact to the coordinator as a typed hold
        # detail: each record kind and bound is a value that detail accepts.
        self.assertEqual(PUBLICATION_BYTE_BOUNDS, set(CAPACITY_HOLD_BYTE_BOUNDS))
        for record_kind in PUBLICATION_RECORD_BUDGETS:
            limit = POLICY.limit(record_kind)
            self.assertIsNone(POLICY.exceeded(record_kind, limit))
            for bound in sorted(PUBLICATION_BYTE_BOUNDS):
                fact = POLICY.exceeded(record_kind, limit + 1, bound=bound)
                assert fact is not None
                self.assertEqual(
                    (fact.record_kind, fact.bound, fact.byte_count, fact.limit, fact.policy_identity),
                    (record_kind, bound, limit + 1, limit, POLICY.identity),
                )
                CapacityHoldDetail(record_kind=fact.record_kind, byte_count=fact.byte_count, limit=fact.limit,
                                   bound=fact.bound, policy_sha256=fact.policy_identity)
        fact = POLICY.exceeded("winner", POLICY.winner_bytes + 7, bound="upper_bound")
        assert fact is not None
        for invalid in (
            {"record_kind": "payload"},
            {"bound": "estimate"},
            {"byte_count": fact.limit},
            {"byte_count": -1},
            {"policy_identity": "sha256:abc"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                replace(fact, **invalid)  # type: ignore[arg-type]

    def test_readers_refuse_exactly_what_writers_cannot_write(self) -> None:
        """Each writer and reader of one record kind shares the policy's single limit.

        Only encoding past the envelope is the typed capacity fact; bytes past
        it on the read side are an integrity refusal, never a capacity hold.
        """

        cases = (
            ("request", request_contract._canonical_json, request_contract.decode_atomic_publication_request_v4,
             WholeDocumentPublicationV4Error),
            ("readiness", readiness_contract._canonical_json, decode_atomic_publication_readiness_v1,
             AtomicPublicationArtifactReadinessError),
            ("winner", publisher_contract._canonical_json, decode_atomic_publication_winner_v4, ValueError),
        )
        for record_kind, encode, decode, integrity in cases:
            limit = POLICY.limit(record_kind)
            with self.subTest(record_kind=record_kind):
                # `{"text":"` and `"}` frame the text: eleven bytes.
                self.assertEqual(len(encode({"text": "x" * (limit - 11)}, record_kind)), limit)
                with self.assertRaises(PublicationEnvelopeExceededError) as refused:
                    encode({"text": "x" * (limit - 10)}, record_kind)
                self.assertEqual(
                    refused.exception.fact,
                    PublicationCapacityFactV1(
                        record_kind=record_kind, bound="exact", byte_count=limit + 1, limit=limit,
                        policy_identity=POLICY.identity,
                    ),
                )
                self.assertEqual((refused.exception.byte_count, refused.exception.limit), (limit + 1, limit))
                # The refusal still matches every error type its layer raised before.
                self.assertIsInstance(refused.exception, integrity)
                # At the limit the reader parses (and rejects the content);
                # one byte past it the reader refuses before parsing.
                with self.assertRaises(integrity) as parsed:
                    decode(b"[" + b" " * (limit - 2) + b"]")
                self.assertNotIn("outside the envelope", str(parsed.exception))
                with self.assertRaisesRegex(integrity, "outside the envelope") as oversized:
                    decode(bytes(limit + 1))
                self.assertNotIsInstance(oversized.exception, PublicationEnvelopeExceededError)
        # A component hash input is a lower bound of the record it belongs to.
        with self.assertRaises(PublicationEnvelopeExceededError) as component:
            request_contract._canonical_json({"text": "x" * POLICY.unit_bytes}, "unit", bound="lower_bound")
        self.assertEqual((component.exception.record_kind, component.exception.bound), ("unit", "lower_bound"))
        with self.assertRaisesRegex(AtomicPublicationArtifactReadinessError, "outside the envelope"):
            decode_atomic_publication_preparation_v1(bytes(POLICY.preparation_bytes + 1))
        with self.assertRaisesRegex(AtomicPublicationArtifactReadinessError, "exceeds its envelope"):
            readiness_contract._canonical_request("x" * (POLICY.request_bytes + 1))
        # The storage adapter reads a preparation through the same limit.
        with tempfile.TemporaryDirectory() as raw_root:
            paths = _Paths(Path(raw_root))
            adapter = FilesystemAtomicPublicationArtifactReadinessV4(
                paths=paths,  # type: ignore[arg-type]
                immutable_store=ImmutableArtifactStore(paths),  # type: ignore[arg-type]
                output_promotion=_Promotion([]),  # type: ignore[arg-type]
            )
            request = _request()
            relpath, _ = adapter._authority_paths(request)
            target = Path(raw_root) / relpath
            target.parent.mkdir(parents=True)
            with open(target, "wb") as sparse:
                sparse.truncate(POLICY.preparation_bytes + 1)
            with self.assertRaises(AtomicPublicationArtifactConflict):
                adapter.load_preparation(request=request)
            target.write_bytes(b"[]")
            with self.assertRaisesRegex(AtomicPublicationArtifactReadinessError, "must be an object"):
                adapter.load_preparation(request=request)


class PublicationPlanMeasurementTests(unittest.TestCase):
    def _plan(self) -> dict[str, object]:
        request = _request(previous_active_run_id="run-old", previous_active_units=(_previous_active_unit(_request()),))
        preparation = _artifact_preparation(request)
        _, reference = _artifact_readiness(preparation)
        return {
            "request": request,
            "preparation": preparation,
            "preparation_byte_count": len(preparation.canonical_bytes),
            "snapshot_byte_count": preparation.document_unit_snapshot_plan.byte_count,
            "semantic_byte_count": preparation.semantic_route_receipts_plan.byte_count,
            "readiness": reference,
        }

    def test_each_record_is_refused_one_byte_past_its_budget_in_write_order(self) -> None:
        plan = self._plan()
        measured = measure_atomic_publication_plan_v4(**plan, policy=POLICY)  # type: ignore[arg-type]
        self.assertEqual(
            (measured.request_bytes, measured.preparation_bytes, measured.snapshot_bytes,
             measured.semantic_bytes, measured.readiness_bytes, measured.unit_count, measured.previous_unit_count),
            (plan["preparation"].request_byte_count, plan["preparation_byte_count"],  # type: ignore[attr-defined]
             plan["snapshot_byte_count"], plan["semantic_byte_count"],
             plan["readiness"].manifest_byte_count, 1, 1),  # type: ignore[attr-defined]
        )
        self.assertEqual(measured.policy_identity, POLICY.identity)
        self.assertTrue(all(type(value) in (int, str) for value in measured.note_scalars().values()))
        exact = {
            "request_bytes": measured.request_bytes,
            "preparation_bytes": measured.preparation_bytes,
            "snapshot_bytes": measured.snapshot_bytes,
            "semantic_bytes": measured.semantic_bytes,
            "readiness_bytes": measured.readiness_bytes,
            "winner_bytes": measured.winner_upper_bound_bytes,
        }
        tight = _policy(**exact, unit_bytes=measured.request_bytes)
        measure_atomic_publication_plan_v4(**plan, policy=tight)  # type: ignore[arg-type]
        for budget, record_kind, bound in (
            ("request_bytes", "request", "exact"),
            ("preparation_bytes", "preparation", "exact"),
            ("snapshot_bytes", "snapshot", "exact"),
            ("semantic_bytes", "semantic", "exact"),
            ("readiness_bytes", "readiness", "exact"),
            ("winner_bytes", "winner", "upper_bound"),
        ):
            with self.subTest(record_kind=record_kind):
                short = _policy(**{**exact, budget: exact[budget] - 1}, unit_bytes=exact["request_bytes"] - 1)
                with self.assertRaises(PublicationEnvelopeExceededError) as refused:
                    measure_atomic_publication_plan_v4(**plan, policy=short)  # type: ignore[arg-type]
                self.assertEqual(
                    (refused.exception.record_kind, refused.exception.bound,
                     refused.exception.byte_count, refused.exception.limit),
                    (record_kind, bound, exact[budget], exact[budget] - 1),
                )
        # Every record short at once: the first write in order is the one refused.
        starved = _policy(**{name: value - 1 for name, value in exact.items()}, unit_bytes=exact["request_bytes"] - 1)
        with self.assertRaises(PublicationEnvelopeExceededError) as first:
            measure_atomic_publication_plan_v4(**plan, policy=starved)  # type: ignore[arg-type]
        self.assertEqual(first.exception.record_kind, "request")
        # The winner refusal is conservative: a budget that holds the winner
        # transaction P would write for this plan still refuses the plan's bound.
        written = _commit_winner(
            plan["request"], plan["preparation"].unit_bindings,  # type: ignore[arg-type,attr-defined]
            plan["readiness"], first_sequence=1_000_000,
        )
        actual = len(written.canonical_bytes)
        self.assertLess(actual, exact["winner_bytes"])
        with self.assertRaises(PublicationEnvelopeExceededError) as conservative:
            measure_atomic_publication_plan_v4(**plan, policy=_policy(winner_bytes=actual))  # type: ignore[arg-type]
        self.assertEqual(
            (conservative.exception.bound, conservative.exception.byte_count, conservative.exception.limit),
            ("upper_bound", exact["winner_bytes"], actual),
        )

    def test_winner_bound_covers_the_exact_winner_transaction_p_writes(self) -> None:
        initial = _request()
        previous = _previous_active_unit(initial)
        changed_projection = json.loads(previous.canonical_query_projection_json)
        changed_projection["applicability"] = "not_applicable"
        changed_json = json.dumps(changed_projection, sort_keys=True, separators=(",", ":"))
        changed = replace(
            previous,
            query_projection_hash="sha256:" + hashlib.sha256(changed_json.encode()).hexdigest(),
            canonical_query_projection_json=changed_json,
        )
        shapes = {
            "created": publication_family_request(units=40, unit_text_bytes=90, heading_depth=2),
            "removed_and_created": publication_family_request(
                units=30, unit_text_bytes=90, previous_units=45, heading_depth=3,
            ),
            "projection_changed": _request(previous_active_run_id="run-old", previous_active_units=(changed,)),
        }
        for shape, request in shapes.items():
            with self.subTest(shape=shape):
                bindings = _bindings(request)
                reference = _reference()
                winner = _commit_winner(request, bindings, reference, first_sequence=1_000_000)
                bound = atomic_publication_winner_byte_upper_bound_v4(
                    request=request, unit_bindings=bindings, artifact_readiness=reference,
                )
                self.assertLessEqual(len(winner.canonical_bytes), bound)
                # With P's own sequence width and commit time the projection is exact.
                self.assertEqual(
                    publisher_contract._winner_projection_byte_count_v4(
                        request=request,
                        unit_bindings=bindings,
                        artifact_readiness=reference,
                        last_event_sequence=1_000_000 + winner.outbox_commit.event_count - 1,
                        commit_time=COMMIT_TIME,
                    ),
                    len(winner.canonical_bytes),
                )


class PublicationEnvelopeFamilyTests(unittest.TestCase):
    """The supported domain reaches past the 8 MiB records that held the 2026-09-29 documents."""

    def test_multi_unit_request_past_eight_mib_publishes_and_reads_back(self) -> None:
        request = publication_family_request(units=4, unit_text_bytes=9 * MIB // 4, heading_depth=2)
        self.assertGreater(len(request.canonical_bytes), 8 * MIB)
        self._publish_and_read_back(request)

    def test_a_single_unit_past_eight_mib_publishes_and_reads_back(self) -> None:
        # One text Unit whose records all pass the single-Unit 8 MiB of earlier
        # releases, published through readiness, P's pure part and reopen
        # under the release policy itself.
        request = publication_family_request(units=1, unit_text_bytes=9 * MIB)
        self.assertGreater(_routed_draft_input_bytes(request.units[0]), 8 * MIB)
        self.assertGreater(_final_row_bytes(request), 8 * MIB)
        self.assertGreater(len(request.canonical_bytes), 8 * MIB)
        self._publish_and_read_back(request)

    def test_escape_dense_single_unit_embeds_in_its_preparation_and_reads_back(self) -> None:
        request = publication_family_request(units=1, unit_text_bytes=5 * MIB // 2, text_kind="escape")
        request_bytes = len(request.canonical_bytes)
        self.assertGreater(_routed_draft_input_bytes(request.units[0]), 8 * MIB)
        preparation_bytes = self._publish_and_read_back(request)
        # The request is embedded as JSON text: every quote and backslash doubles again.
        self.assertGreater(preparation_bytes, 19 * request_bytes // 10)
        self.assertLessEqual(preparation_bytes, POLICY.preparation_bytes)

    def test_small_records_keep_their_released_bytes(self) -> None:
        """Digests computed with the frozen 56abdb93 source for the same deterministic inputs."""

        request = _request()
        preparation = _artifact_preparation(request)
        manifest, _ = _artifact_readiness(preparation)
        family = publication_family_request(units=3, unit_text_bytes=200, previous_units=2, heading_depth=2)
        bindings = _bindings(family)
        winner = _commit_winner(family, bindings, None, first_sequence=1_000)
        self.assertEqual(
            {name: hashlib.sha256(value).hexdigest() for name, value in (
                ("fixture_request", request.canonical_bytes),
                ("fixture_preparation", preparation.canonical_bytes),
                ("fixture_readiness", manifest.canonical_bytes),
                ("fixture_winner", _winner(request).canonical_bytes),
                ("family_request", family.canonical_bytes),
                ("family_snapshot", document_unit_snapshot_file_bytes_v1(request=family, bindings=bindings)),
                ("family_semantic", semantic_route_receipts_file_bytes_v3(family.semantic_route_receipts)),
                ("family_winner", winner.canonical_bytes),
            )},
            {
                "fixture_request": "5a703ad5f28740bc09d3cf3861175b0efac1b57b80709e9c729451281ebdaf1a",
                "fixture_preparation": "d33cbf4e9305b465b86d090ae7c3d7598c8b607ab257c0d320eb8152670b9899",
                "fixture_readiness": "e724b9710b7c273d52700121757a051af00c749f16ef4b1fc3c75e10fb1ae935",
                "fixture_winner": "3eec2d7243ac758bfd7306d8d9874d6fb185883639f3e23f37a9d6503f3ff2ed",
                "family_request": "07d075d34692da2c37f01ceaf54f7ce63edc15d301d3ec5d1c9c2eaf87bd2768",
                "family_snapshot": "8f11b40a449148926fdd606fca091c1bd2bf8c166af6e853353ca19f691e2ad5",
                "family_semantic": "d37833199fca427e31b087745ab1327b80d831cf2fe5ce8cb66d180d9ef67b18",
                "family_winner": "87741f889ac6353427d509d2f874b034bef2692e5bae0a4326752847496d13ed",
            },
        )

    def _publish_and_read_back(self, request: AtomicPublicationRequestV4) -> int:
        """Readiness, transaction P's pure part and reopen, all against the one policy."""

        _, checkpoint, intent, receipt, manifest, envelope = _publication_materialized_evidence()
        materialized = MaterializedProviderDocumentV4(
            receipt=receipt, intent=intent, provider_envelope=envelope, manifest=manifest,
        )
        claim = V4ClaimWitness(
            attempt_id=checkpoint.attempt_id,
            fence_identity=checkpoint.fence_identity,
            state=checkpoint.state,
            lifecycle_version=checkpoint.lifecycle_version,
            checkpoint_sha256=checkpoint.sha256,
            claim_owner_identity="worker-1",
            claim_generation=1,
        )
        guard = StageLeaseGuard(deadline_monotonic=60.0, _revoked=Event(), _monotonic=lambda: 0.0)
        with tempfile.TemporaryDirectory() as raw_root:
            paths = _Paths(Path(raw_root))

            def adapter() -> FilesystemAtomicPublicationArtifactReadinessV4:
                return FilesystemAtomicPublicationArtifactReadinessV4(
                    paths=paths,  # type: ignore[arg-type]
                    immutable_store=ImmutableArtifactStore(paths),  # type: ignore[arg-type]
                    output_promotion=_Promotion([]),  # type: ignore[arg-type]
                )

            readiness = adapter()
            reference = readiness.prepare_or_replay(
                request=request, checkpoint=checkpoint, materialized=materialized,
                claim=claim, claim_guard=_Guard(), stage_guard=guard,
            )
            ready = readiness.verify_ready(reference=reference, expected_request=request)
            validate_atomic_publication_artifacts_ready_v4(request=request, artifacts_ready=ready)
            winner = _commit_winner(request, ready.preparation.unit_bindings, reference, first_sequence=10**9)
            decoded = decode_atomic_publication_winner_v4(winner.canonical_bytes)
            self.assertEqual(decoded, winner)
            readiness.verify_ready(reference=reference, expected_request=request, expected_winner=decoded)
            self.assertEqual(
                adapter().reopen_prepared_request(checkpoint=checkpoint, materialized=materialized), request,
            )
            return ready.manifest.preparation_byte_count


def _bindings(request: AtomicPublicationRequestV4) -> tuple[AtomicPublicationUnitBindingV4, ...]:
    return tuple(
        AtomicPublicationUnitBindingV4(**asdict(item))
        for item in seal_unit_asset_winners_v4(request=request, asset_ids=family_unit_ids(len(request.units)))
    )


def _reference() -> object:
    return _artifact_readiness(_artifact_preparation(_request()))[1]


def _commit_winner(
    request: AtomicPublicationRequestV4,
    bindings: tuple[AtomicPublicationUnitBindingV4, ...],
    reference: object,
    *,
    first_sequence: int,
) -> AtomicPublicationWinnerV4:
    """The winner transaction P seals for these IDs, sequences and commit time."""

    asset_ids = tuple(item.asset_id for item in bindings)
    events = tuple(
        seal_published_outbox_event_v4(
            # Deterministic IDs of the minted outbox ID width ("oe_" + 26).
            event_id=f"oe_{first_sequence + offset:026d}",
            event_sequence=first_sequence + offset,
            event_kind=event.event_kind,
            change_kind=event.change_kind,
            subject_kind=event.subject_kind,
            subject_ref=event.subject_ref,
            document_id=event.document_id,
            processing_run_id=event.processing_run_id,
            asset_id=event.asset_id,
            canonical_payload_json=canonical_text(event.payload),
            occurred_at=COMMIT_TIME,
        )
        for offset, event in enumerate(
            build_atomic_publication_outbox_events_v4(request=request, asset_ids=asset_ids, occurred_at=COMMIT_TIME)
        )
    )
    identity = request.identity
    values: dict[str, object] = {}
    if reference is not None:
        values = {"artifact_readiness": reference, "winner_row_version": 2}
    return seal_atomic_publication_winner_v4(
        request=request,
        asset_ids=asset_ids,
        outbox_events=events,
        attempt_id=identity.attempt_id,
        fence_identity=identity.fence_identity,
        document_id=identity.document_id,
        processing_run_id=identity.processing_run_id,
        publish_attempt_generation=identity.attempt_generation,
        local_checkpoint_sha256=identity.expected_checkpoint_sha256,
        lifecycle_version_before=identity.expected_lifecycle_version,
        lifecycle_version_after=identity.expected_lifecycle_version + 1,
        request_sha256=request.request_sha256,
        upstream_evidence_sha256=request.upstream_evidence.evidence_sha256,
        previous_active_run_id=identity.expected_previous_processing_run_id,
        publish_precommit_at=COMMIT_TIME,
        **values,
    )


def _routed_draft_input_bytes(unit: object) -> int:
    """The sealed Unit's canonical bytes without its derived routed-draft hash."""

    payload = request_contract._pre_id_unit_payload(unit)  # type: ignore[arg-type]
    payload.pop("routed_draft_sha256")
    return len(canonical_publication_json(payload))


def _final_row_bytes(request: AtomicPublicationRequestV4) -> int:
    unit = request.units[0]
    return len(
        canonical_publication_json(
            {
                "applicability": unit.applicability,
                "artifact_locator": json.loads(unit.canonical_artifact_locator_json),
                "asset_id": family_unit_ids(1)[0],
                "content_hash": unit.content_hash,
                "document_id": unit.document_id,
                "heading_path": list(unit.heading_path),
                "order_index": unit.unit_index,
                "page_no": unit.page_no,
                "payload": json.loads(unit.canonical_payload_json),
                "payload_kind": unit.payload_kind,
                "processing_run_id": unit.processing_run_id,
                "provider_document_id": unit.provider_document_id,
                "quality_status": unit.quality_status,
                "query_projection_hash": unit.query_projection_hash,
                "section_keys": None if unit.section_keys is None else list(unit.section_keys),
                "semantic_keys": None if unit.semantic_keys is None else list(unit.semantic_keys),
                "structure_hash": unit.structure_hash,
                "title": unit.title,
            }
        )
    )


if __name__ == "__main__":
    unittest.main()
