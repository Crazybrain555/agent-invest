"""Port and immutable winner contract for transaction-P publication v4."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any, Protocol, cast

from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactsReadyV4,
    AtomicPublicationReadinessReferenceV1,
    AtomicPublicationUnitBindingV4,
    validate_preparation_readiness_pair_v1,
)
from disclosure_anchor.application.ports.staged_provider_parser import V4ClaimWitness
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.entities import outbox_events
from disclosure_anchor.domain.services.unit_hashing import query_projection

if TYPE_CHECKING:
    from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
        AtomicPublicationRequestV4,
        PreIdUnitPublicationV4,
        PreviousActiveUnitV4,
    )


ATOMIC_PUBLICATION_WINNER_V4_CONTRACT = "atomic-publication-winner.v4"
_MAX_BYTES = 8 * 1024 * 1024
_MAX_INT = (1 << 63) - 1
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ASSET_ID = re.compile(r"du_[0-9A-HJKMNP-TV-Z]{26}\Z")


class AtomicPublicationUniqueConflict(RuntimeError):
    """A different immutable winner already owns this publication identity."""


class AtomicPublicationCommitResponseLost(RuntimeError):
    """The caller must reload the immutable winner before retrying."""


@dataclass(frozen=True, slots=True)
class PublishedOutboxEventV4:
    """Exact transaction-assigned outbox row before server sequence assignment."""

    event_id: str
    event_sequence: int
    event_kind: str
    change_kind: str
    subject_kind: str
    subject_ref: str
    document_id: str
    processing_run_id: str
    asset_id: str | None
    canonical_payload_json: str
    occurred_at: datetime
    event_row_sha256: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.event_id, "outbox event"),
            (self.event_kind, "outbox event kind"),
            (self.change_kind, "outbox change kind"),
            (self.subject_kind, "outbox subject kind"),
            (self.subject_ref, "outbox subject"),
            (self.document_id, "outbox document"),
            (self.processing_run_id, "outbox processing run"),
        ):
            _identity(value, label)
        _positive(self.event_sequence, "outbox event sequence")
        if self.asset_id is not None:
            _asset_id(self.asset_id)
        _canonical_json_text(self.canonical_payload_json, "outbox payload")
        _utc(self.occurred_at, "outbox occurred time")
        _sha(self.event_row_sha256, "outbox event row")
        if self.event_row_sha256 != published_outbox_event_row_sha256_v4(self):
            raise ValueError("outbox event row hash does not close")


@dataclass(frozen=True, slots=True)
class PublishedOutboxCommitReference:
    document_id: str
    processing_run_id: str
    first_event_id: str
    last_event_id: str
    event_count: int
    events_sha256: str
    processing_run_published_event_id: str
    processing_run_published_event_sha256: str
    events: tuple[PublishedOutboxEventV4, ...]

    def __post_init__(self) -> None:
        _identity(self.document_id, "outbox document")
        _identity(self.processing_run_id, "outbox processing run")
        _identity(self.first_event_id, "first outbox event")
        _identity(self.last_event_id, "last outbox event")
        _identity(
            self.processing_run_published_event_id,
            "processing-run-published event",
        )
        _positive(self.event_count, "outbox event count")
        _sha(self.events_sha256, "outbox events")
        _sha(
            self.processing_run_published_event_sha256,
            "processing-run-published event",
        )
        if not isinstance(self.events, tuple) or not self.events:
            raise ValueError("outbox reference requires exact event rows")
        if any(type(item) is not PublishedOutboxEventV4 for item in self.events):
            raise ValueError("outbox reference event rows are not exact")
        if len({item.event_id for item in self.events}) != len(self.events):
            raise ValueError("outbox reference repeats an event ID")
        if tuple(item.event_sequence for item in self.events) != tuple(
            sorted(item.event_sequence for item in self.events)
        ) or len({item.event_sequence for item in self.events}) != len(self.events):
            raise ValueError("outbox reference event sequences are not ordered")
        published = tuple(
            item
            for item in self.events
            if item.event_kind == "processing_run_published"
        )
        if (
            self.event_count != len(self.events)
            or self.first_event_id != self.events[0].event_id
            or self.last_event_id != self.events[-1].event_id
            or self.events_sha256 != published_outbox_events_sha256_v4(self.events)
            or len(published) != 1
            or self.processing_run_published_event_id != published[0].event_id
            or self.processing_run_published_event_sha256
            != published[0].event_row_sha256
        ):
            raise ValueError("outbox commit reference does not close")


@dataclass(frozen=True, slots=True)
class DurablePublishBaseCommitReference:
    document_id: str
    processing_run_id: str
    publish_attempt_generation: int
    source_identity_sha256: str
    source_page_count: int
    publish_precommit_at: datetime
    durable_base_sha256: str

    def __post_init__(self) -> None:
        _identity(self.document_id, "durable base document")
        _identity(self.processing_run_id, "durable base run")
        _positive(self.publish_attempt_generation, "publish attempt generation")
        _sha(self.source_identity_sha256, "durable base source identity")
        _positive(self.source_page_count, "durable base source page count")
        _utc(self.publish_precommit_at, "durable base precommit time")
        _sha(self.durable_base_sha256, "durable publish base")


@dataclass(frozen=True, slots=True)
class UnitAssetWinnerV4:
    unit_index: int
    asset_id: str
    routed_draft_sha256: str
    final_unit_row_sha256: str
    lineage_row_sha256: str

    def __post_init__(self) -> None:
        _positive(self.unit_index, "winner unit index")
        if not isinstance(self.asset_id, str) or _ASSET_ID.fullmatch(self.asset_id) is None:
            raise ValueError("winner asset is not a canonical Unit ID")
        for value, label in (
            (self.routed_draft_sha256, "routed draft"),
            (self.final_unit_row_sha256, "final Unit row"),
            (self.lineage_row_sha256, "lineage row"),
        ):
            _sha(value, label)


@dataclass(frozen=True, slots=True)
class AtomicPublicationWinnerV4:
    attempt_id: str
    fence_identity: str
    document_id: str
    processing_run_id: str
    publish_attempt_generation: int
    local_checkpoint_sha256: str
    lifecycle_version_before: int
    lifecycle_version_after: int
    request_sha256: str
    upstream_evidence_sha256: str
    final_units_sha256: str
    lineage_sha256: str
    processing_run_row_sha256: str
    previous_active_run_id: str | None
    inserted_count: int
    updated_count: int
    deleted_count: int
    outbox_commit: PublishedOutboxCommitReference
    durable_base_commit: DurablePublishBaseCommitReference
    unit_assets: tuple[UnitAssetWinnerV4, ...]
    publish_precommit_at: datetime
    artifact_readiness: AtomicPublicationReadinessReferenceV1 | None = None
    winner_row_version: int = 1
    contract_version: str = ATOMIC_PUBLICATION_WINNER_V4_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != ATOMIC_PUBLICATION_WINNER_V4_CONTRACT:
            raise ValueError("atomic publication winner contract is unsupported")
        for value, label in (
            (self.attempt_id, "attempt"),
            (self.fence_identity, "fence"),
            (self.document_id, "document"),
            (self.processing_run_id, "processing run"),
        ):
            _identity(value, label)
        _positive(self.publish_attempt_generation, "publish attempt generation")
        for value, label in (
            (self.local_checkpoint_sha256, "local checkpoint"),
            (self.request_sha256, "publication request"),
            (self.upstream_evidence_sha256, "upstream evidence"),
            (self.final_units_sha256, "final Units"),
            (self.lineage_sha256, "lineage"),
            (self.processing_run_row_sha256, "processing run row"),
        ):
            _sha(value, label)
        _publication_lifecycle_version(
            self.lifecycle_version_before,
            "lifecycle version before",
        )
        if (
            type(self.lifecycle_version_after) is not int
            or not 1 <= self.lifecycle_version_after <= _MAX_INT
        ):
            raise ValueError("lifecycle version after is outside signed BIGINT")
        if self.lifecycle_version_after != self.lifecycle_version_before + 1:
            raise ValueError("winner lifecycle version did not advance exactly")
        if self.previous_active_run_id is not None:
            _identity(self.previous_active_run_id, "previous active run")
        for count_value, label in (
            (self.inserted_count, "inserted count"),
            (self.updated_count, "updated count"),
            (self.deleted_count, "deleted count"),
        ):
            _nonnegative(count_value, label)
        if type(self.outbox_commit) is not PublishedOutboxCommitReference:
            raise ValueError("winner lacks an exact outbox reference")
        if type(self.durable_base_commit) is not DurablePublishBaseCommitReference:
            raise ValueError("winner lacks an exact durable-base reference")
        if (
            self.outbox_commit.document_id != self.document_id
            or self.outbox_commit.processing_run_id != self.processing_run_id
            or self.durable_base_commit.document_id != self.document_id
            or self.durable_base_commit.processing_run_id != self.processing_run_id
            or self.durable_base_commit.publish_attempt_generation
            != self.publish_attempt_generation
        ):
            raise ValueError("durable-base identity drifted from winner")
        if not isinstance(self.unit_assets, tuple) or not self.unit_assets:
            raise ValueError("winner requires ordered Unit assets")
        if tuple(item.unit_index for item in self.unit_assets) != tuple(
            range(1, len(self.unit_assets) + 1)
        ):
            raise ValueError("winner Unit assets are not contiguous and ordered")
        if len({item.asset_id for item in self.unit_assets}) != len(self.unit_assets):
            raise ValueError("winner repeats an asset ID")
        if self.inserted_count != len(self.unit_assets):
            raise ValueError("winner inserted count differs from Unit assets")
        if self.final_units_sha256 != final_unit_rows_sha256_v4(self.unit_assets):
            raise ValueError("winner final-Unit aggregate does not close")
        if self.lineage_sha256 != lineage_rows_sha256_v4(self.unit_assets):
            raise ValueError("winner lineage aggregate does not close")
        _utc(self.publish_precommit_at, "publish precommit time")
        if (
            self.updated_count
            != sum(
                item.event_kind == "document_unit_projection_changed"
                for item in self.outbox_commit.events
            )
            or self.deleted_count
            != sum(
                item.event_kind == "document_unit_removed"
                for item in self.outbox_commit.events
            )
        ):
            raise ValueError("winner mutation counts differ from exact outbox rows")
        if (
            self.durable_base_commit.publish_precommit_at
            != self.publish_precommit_at
        ):
            raise ValueError("durable-base precommit time drifted from winner")
        if self.winner_row_version == 1:
            if self.artifact_readiness is not None:
                raise ValueError("legacy winner v1 cannot bind artifact readiness")
        elif self.winner_row_version == 2:
            if type(self.artifact_readiness) is not AtomicPublicationReadinessReferenceV1:
                raise ValueError("winner v2 lacks exact artifact readiness")
        else:
            raise ValueError("winner row version is unsupported")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(_winner_payload(self))

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes).hexdigest()


class AtomicPublicationWinnerReaderV4Port(Protocol):
    """Read committed winner authority without knowing its attempt ID."""

    def reload_commit_winner_by_processing_run_id(
        self,
        *,
        processing_run_id: str,
    ) -> AtomicPublicationWinnerV4 | None: ...


class AtomicWholeDocumentPublisherV4Port(
    AtomicPublicationWinnerReaderV4Port,
    Protocol,
):
    """One non-async-local-commit PostgreSQL transaction selects the winner.

    The transaction locks the active run and must compare its canonical Unit
    inventory and digest with the request before consuming the ordinary Unit
    IDs sealed by the immutable preparation intent.
    """

    def commit_whole_document(
        self,
        request: AtomicPublicationRequestV4,
        *,
        claim: V4ClaimWitness,
        artifacts_ready: AtomicPublicationArtifactsReadyV4,
    ) -> AtomicPublicationWinnerV4: ...

    def reload_commit_winner(
        self, *, processing_run_id: str, attempt_id: str
    ) -> AtomicPublicationWinnerV4 | None: ...



def validate_atomic_publication_claim_v4(
    *,
    request: AtomicPublicationRequestV4,
    claim: V4ClaimWitness,
) -> None:
    """Validate the supplied operational claim before its transactional CAS.

    The claim is deliberately absent from canonical request/winner bytes.  A
    PostgreSQL implementation must additionally compare its owner, generation,
    and live lease with the locked mutable head in the publication transaction.
    """

    if type(claim) is not V4ClaimWitness:
        raise ValueError("publication requires an exact v4 claim witness")
    identity = request.identity
    _publication_lifecycle_version(
        identity.expected_lifecycle_version,
        "publication lifecycle version",
    )
    if (
        claim.attempt_id != identity.attempt_id
        or claim.fence_identity != identity.fence_identity
        or claim.state != identity.expected_attempt_state
        or claim.lifecycle_version != identity.expected_lifecycle_version
        or claim.checkpoint_sha256 != identity.expected_checkpoint_sha256
    ):
        raise ValueError("publication claim drifted from its request")


def validate_atomic_publication_artifacts_ready_v4(
    *,
    request: AtomicPublicationRequestV4,
    artifacts_ready: AtomicPublicationArtifactsReadyV4,
) -> None:
    if type(artifacts_ready) is not AtomicPublicationArtifactsReadyV4:
        raise ValueError("publication requires an exact artifact-ready witness")
    if artifacts_ready.request.canonical_bytes != request.canonical_bytes:
        raise ValueError("artifact-ready witness belongs to another request")
    validate_preparation_readiness_pair_v1(
        preparation=artifacts_ready.preparation,
        manifest=artifacts_ready.manifest,
        reference=artifacts_ready.reference,
        request=request,
    )
    expected_assets = seal_unit_asset_winners_v4(
        request=request,
        asset_ids=tuple(
            item.asset_id for item in artifacts_ready.preparation.unit_bindings
        ),
    )
    expected_bindings = tuple(
        AtomicPublicationUnitBindingV4(
            unit_index=item.unit_index,
            asset_id=item.asset_id,
            routed_draft_sha256=item.routed_draft_sha256,
            final_unit_row_sha256=item.final_unit_row_sha256,
            lineage_row_sha256=item.lineage_row_sha256,
        )
        for item in expected_assets
    )
    if (
        artifacts_ready.preparation.unit_bindings != expected_bindings
        or artifacts_ready.manifest.unit_bindings != expected_bindings
    ):
        raise ValueError("artifact-ready Unit bindings drifted from request")


def seal_atomic_publication_winner_v4(
    *,
    request: AtomicPublicationRequestV4,
    asset_ids: tuple[str, ...],
    outbox_events: tuple[PublishedOutboxEventV4, ...],
    **values: Any,
) -> AtomicPublicationWinnerV4:
    derived_fields = {
        "final_units_sha256",
        "inserted_count",
        "lineage_sha256",
        "outbox_commit",
        "processing_run_row_sha256",
        "updated_count",
        "deleted_count",
        "durable_base_commit",
        "unit_assets",
    }
    if derived_fields.intersection(values):
        raise ValueError("atomic publication winner derived fields are sealed")
    unit_assets = seal_unit_asset_winners_v4(
        request=request,
        asset_ids=asset_ids,
    )
    outbox_commit = seal_published_outbox_commit_reference_v4(
        events=outbox_events,
    )
    unit_diff = _derive_unit_diff_v4(
        request=request,
        unit_assets=unit_assets,
    )
    updated_count = len(unit_diff.projection_changed)
    deleted_count = len(unit_diff.removed)
    publish_precommit_at = values.get("publish_precommit_at")
    if not isinstance(publish_precommit_at, datetime):
        raise ValueError("winner requires an exact publish precommit time")
    previous_active_run_id = cast(str | None, values.get("previous_active_run_id"))
    final_units_sha256 = final_unit_rows_sha256_v4(unit_assets)
    lineage_sha256 = lineage_rows_sha256_v4(unit_assets)
    processing_run_sha256 = processing_run_row_sha256_v4(request)
    durable_base_commit = DurablePublishBaseCommitReference(
        document_id=request.identity.document_id,
        processing_run_id=request.identity.processing_run_id,
        publish_attempt_generation=request.identity.attempt_generation,
        source_identity_sha256=request.upstream_evidence.source_pdf_sha256,
        source_page_count=request.source_page_count,
        publish_precommit_at=publish_precommit_at,
        durable_base_sha256=durable_publish_base_sha256_v4(
            request=request,
            unit_assets=unit_assets,
            previous_active_run_id=previous_active_run_id,
            updated_count=updated_count,
            deleted_count=deleted_count,
            outbox_commit=outbox_commit,
            publish_precommit_at=publish_precommit_at,
        ),
    )
    winner = AtomicPublicationWinnerV4(
        **{
            **values,
            "final_units_sha256": final_units_sha256,
            "inserted_count": len(unit_assets),
            "updated_count": updated_count,
            "deleted_count": deleted_count,
            "lineage_sha256": lineage_sha256,
            "processing_run_row_sha256": processing_run_sha256,
            "outbox_commit": outbox_commit,
            "durable_base_commit": durable_base_commit,
            "unit_assets": unit_assets,
        }
    )
    validate_atomic_publication_winner_v4(request=request, winner=winner)
    return winner


def seal_unit_asset_winners_v4(
    *,
    request: AtomicPublicationRequestV4,
    asset_ids: tuple[str, ...],
) -> tuple[UnitAssetWinnerV4, ...]:
    if not isinstance(asset_ids, tuple) or len(asset_ids) != len(request.units):
        raise ValueError("transaction-assigned Unit IDs do not close the request")
    result: list[UnitAssetWinnerV4] = []
    for unit, asset_id in zip(request.units, asset_ids, strict=True):
        result.append(
            UnitAssetWinnerV4(
                unit_index=unit.unit_index,
                asset_id=asset_id,
                routed_draft_sha256=unit.routed_draft_sha256,
                final_unit_row_sha256=final_unit_row_sha256_v4(
                    request=request,
                    unit_index=unit.unit_index,
                    asset_id=asset_id,
                ),
                lineage_row_sha256=lineage_row_sha256_v4(
                    request=request,
                    unit_index=unit.unit_index,
                    asset_id=asset_id,
                ),
            )
        )
    return tuple(result)


def seal_published_outbox_event_v4(**values: Any) -> PublishedOutboxEventV4:
    if "event_row_sha256" in values:
        raise ValueError("outbox event row hash is derived")
    expected = {item.name for item in fields(PublishedOutboxEventV4)} - {
        "event_row_sha256"
    }
    if set(values) != expected:
        raise ValueError("PublishedOutboxEventV4 fields are not closed")
    exact_values = dict(values)
    exact_values["event_row_sha256"] = _digest(
        _canonical_json(_published_outbox_event_values_payload(values))
    )
    return PublishedOutboxEventV4(**exact_values)


def published_outbox_event_row_sha256_v4(
    event: PublishedOutboxEventV4,
) -> str:
    return _digest(_canonical_json(_published_outbox_event_payload(event)))


def published_outbox_events_sha256_v4(
    events: tuple[PublishedOutboxEventV4, ...],
) -> str:
    if not isinstance(events, tuple) or not events:
        raise ValueError("outbox event aggregate requires exact rows")
    return _digest(
        _canonical_json(
            [
                {
                    "event_id": item.event_id,
                    "event_row_sha256": item.event_row_sha256,
                }
                for item in events
            ]
        )
    )


def seal_published_outbox_commit_reference_v4(
    *,
    events: tuple[PublishedOutboxEventV4, ...],
) -> PublishedOutboxCommitReference:
    if not isinstance(events, tuple) or not events:
        raise ValueError("outbox commit requires exact event rows")
    published = tuple(
        item for item in events if item.event_kind == "processing_run_published"
    )
    if len(published) != 1:
        raise ValueError("outbox commit requires one processing-run event")
    return PublishedOutboxCommitReference(
        document_id=published[0].document_id,
        processing_run_id=published[0].processing_run_id,
        first_event_id=events[0].event_id,
        last_event_id=events[-1].event_id,
        event_count=len(events),
        events_sha256=published_outbox_events_sha256_v4(events),
        processing_run_published_event_id=published[0].event_id,
        processing_run_published_event_sha256=published[0].event_row_sha256,
        events=events,
    )


def build_atomic_publication_outbox_events_v4(
    *,
    request: AtomicPublicationRequestV4,
    asset_ids: tuple[str, ...],
    occurred_at: datetime,
) -> tuple[e.OutboxEvent, ...]:
    """Build the only outbox ordering admitted by transaction P."""

    _utc(occurred_at, "atomic publication outbox time")
    unit_assets = seal_unit_asset_winners_v4(
        request=request,
        asset_ids=asset_ids,
    )
    unit_diff = _derive_unit_diff_v4(
        request=request,
        unit_assets=unit_assets,
    )
    events: list[e.OutboxEvent] = []
    for old in unit_diff.removed:
        events.append(
            outbox_events.document_unit_removed(
                document_id=request.identity.document_id,
                old_processing_run_id=old.processing_run_id,
                old_asset_id=old.asset_id,
                content_hash=old.content_hash,
                payload_kind=old.payload_kind,
                old_order_index=old.order_index,
                old_heading_path=list(old.heading_path),
                occurred_at=occurred_at,
            )
        )
    for new in unit_diff.created:
        unit = new.unit
        events.append(
            outbox_events.document_unit_created(
                document_id=request.identity.document_id,
                processing_run_id=request.identity.processing_run_id,
                new_asset_id=new.asset_id,
                content_hash=unit.content_hash,
                payload_kind=unit.payload_kind,
                new_order_index=unit.unit_index,
                new_heading_path=list(unit.heading_path),
                occurred_at=occurred_at,
            )
        )
    for old, new, changed_fields in unit_diff.projection_changed:
        events.append(
            outbox_events.document_unit_projection_changed(
                document_id=request.identity.document_id,
                new_processing_run_id=request.identity.processing_run_id,
                old_asset_id=old.asset_id,
                new_asset_id=new.asset_id,
                content_hash=new.unit.content_hash,
                old_query_projection_hash=old.query_projection_hash,
                new_query_projection_hash=new.unit.query_projection_hash,
                changed_fields=list(changed_fields),
                occurred_at=occurred_at,
            )
        )
    projection = cast(
        dict[str, Any],
        strict_json_loads(request.processing_run_projection_json.encode("utf-8")),
    )
    previous_run = request.identity.expected_previous_processing_run_id
    change_kind = (
        "materialized"
        if previous_run is None or unit_diff.created or unit_diff.removed
        else "observed"
    )
    events.append(
        outbox_events.processing_run_published(
            document_id=request.identity.document_id,
            processing_run_id=request.identity.processing_run_id,
            change_kind=change_kind,
            previous_processing_run_id=previous_run,
            content_hash_aggregate=projection["content_hash_aggregate"],
            structure_hash=projection["structure_hash_aggregate"],
            unit_count=len(request.units),
            created_count=len(unit_diff.created),
            removed_count=len(unit_diff.removed),
            projection_changed_count=len(unit_diff.projection_changed),
            source_identity=request.upstream_evidence.source_pdf_sha256,
            source_page_count=request.source_page_count,
            publish_committed_at=occurred_at,
            occurred_at=occurred_at,
        )
    )
    return tuple(events)


def durable_publish_base_sha256_v4(
    *,
    request: AtomicPublicationRequestV4,
    unit_assets: tuple[UnitAssetWinnerV4, ...],
    previous_active_run_id: str | None,
    updated_count: int,
    deleted_count: int,
    outbox_commit: PublishedOutboxCommitReference,
    publish_precommit_at: datetime,
) -> str:
    """Bind the durable base row to this exact transaction winner basis."""

    return _digest(
        _canonical_json(
            {
                "attempt_id": request.identity.attempt_id,
                "deleted_count": deleted_count,
                "document_id": request.identity.document_id,
                "final_units_sha256": final_unit_rows_sha256_v4(unit_assets),
                "inserted_count": len(unit_assets),
                "lineage_sha256": lineage_rows_sha256_v4(unit_assets),
                "local_checkpoint_sha256": (
                    request.identity.expected_checkpoint_sha256
                ),
                "outbox_events_sha256": outbox_commit.events_sha256,
                "previous_active_units_sha256": (
                    request.previous_active_units_sha256
                ),
                "previous_active_run_id": previous_active_run_id,
                "processing_run_id": request.identity.processing_run_id,
                "processing_run_row_sha256": processing_run_row_sha256_v4(
                    request
                ),
                "publish_attempt_generation": (
                    request.identity.attempt_generation
                ),
                "publish_precommit_at": _canonical_datetime(
                    publish_precommit_at
                ),
                "request_sha256": request.request_sha256,
                "source_identity_sha256": (
                    request.upstream_evidence.source_pdf_sha256
                ),
                "source_page_count": request.source_page_count,
                "updated_count": updated_count,
                "upstream_evidence_sha256": (
                    request.upstream_evidence.evidence_sha256
                ),
            }
        )
    )


def final_unit_row_sha256_v4(
    *,
    request: AtomicPublicationRequestV4,
    unit_index: int,
    asset_id: str,
) -> str:
    unit = _request_unit(request, unit_index)
    _asset_id(asset_id)
    payload = strict_json_loads(unit.canonical_payload_json.encode("utf-8"))
    locator = strict_json_loads(
        unit.canonical_artifact_locator_json.encode("utf-8")
    )
    return _digest(
        _canonical_json(
            {
                "applicability": unit.applicability,
                "artifact_locator": locator,
                "asset_id": asset_id,
                "content_hash": unit.content_hash,
                "document_id": unit.document_id,
                "heading_path": list(unit.heading_path),
                "order_index": unit.unit_index,
                "page_no": unit.page_no,
                "payload": payload,
                "payload_kind": unit.payload_kind,
                "processing_run_id": unit.processing_run_id,
                "provider_document_id": unit.provider_document_id,
                "quality_status": unit.quality_status,
                "query_projection_hash": unit.query_projection_hash,
                "section_keys": (
                    None if unit.section_keys is None else list(unit.section_keys)
                ),
                "semantic_keys": (
                    None if unit.semantic_keys is None else list(unit.semantic_keys)
                ),
                "structure_hash": unit.structure_hash,
                "title": unit.title,
            }
        )
    )


def lineage_row_sha256_v4(
    *,
    request: AtomicPublicationRequestV4,
    unit_index: int,
    asset_id: str,
) -> str:
    unit = _request_unit(request, unit_index)
    _asset_id(asset_id)
    return _digest(
        _canonical_json(
            {
                "asset_id": asset_id,
                "attempt_generation": request.identity.attempt_generation,
                "attempt_id": request.identity.attempt_id,
                "document_id": request.identity.document_id,
                "fence_identity": request.identity.fence_identity,
                "page_numbers": list(unit.page_numbers),
                "parser_target_sha256": (
                    request.upstream_evidence.parser_target_sha256
                ),
                "processing_run_id": request.identity.processing_run_id,
                "provider_document_id": request.identity.provider_document_id,
                "provider_document_sha256": (
                    request.upstream_evidence.provider_document_sha256
                ),
                "source_pdf_sha256": request.upstream_evidence.source_pdf_sha256,
                "unit_index": unit.unit_index,
                "upstream_evidence_sha256": (
                    request.upstream_evidence.evidence_sha256
                ),
            }
        )
    )


def processing_run_row_sha256_v4(
    request: AtomicPublicationRequestV4,
) -> str:
    projection = strict_json_loads(
        request.processing_run_projection_json.encode("utf-8")
    )
    return _digest(_canonical_json(projection))


def final_unit_rows_sha256_v4(
    unit_assets: tuple[UnitAssetWinnerV4, ...],
) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(
            [
                {
                    "asset_id": item.asset_id,
                    "final_unit_row_sha256": item.final_unit_row_sha256,
                    "routed_draft_sha256": item.routed_draft_sha256,
                    "unit_index": item.unit_index,
                }
                for item in unit_assets
            ]
        )
    ).hexdigest()


def lineage_rows_sha256_v4(
    unit_assets: tuple[UnitAssetWinnerV4, ...],
) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(
            [
                {
                    "asset_id": item.asset_id,
                    "lineage_row_sha256": item.lineage_row_sha256,
                    "unit_index": item.unit_index,
                }
                for item in unit_assets
            ]
        )
    ).hexdigest()


def validate_atomic_publication_winner_v4(
    *,
    request: AtomicPublicationRequestV4,
    winner: AtomicPublicationWinnerV4,
) -> None:
    if (
        winner.attempt_id != request.identity.attempt_id
        or winner.fence_identity != request.identity.fence_identity
        or winner.document_id != request.identity.document_id
        or winner.processing_run_id != request.identity.processing_run_id
        or winner.publish_attempt_generation != request.identity.attempt_generation
        or winner.local_checkpoint_sha256
        != request.identity.expected_checkpoint_sha256
        or winner.lifecycle_version_before
        != request.identity.expected_lifecycle_version
        or winner.request_sha256 != request.request_sha256
        or winner.upstream_evidence_sha256
        != request.upstream_evidence.evidence_sha256
        or winner.previous_active_run_id
        != request.identity.expected_previous_processing_run_id
    ):
        raise ValueError("atomic publication winner drifted from its request")
    if len(winner.unit_assets) != len(request.units):
        raise ValueError("atomic publication winner Unit count drifted")
    expected_assets = seal_unit_asset_winners_v4(
        request=request,
        asset_ids=tuple(item.asset_id for item in winner.unit_assets),
    )
    if winner.unit_assets != expected_assets:
        raise ValueError("atomic publication winner row projection drifted")
    if (
        winner.final_units_sha256 != final_unit_rows_sha256_v4(expected_assets)
        or winner.lineage_sha256 != lineage_rows_sha256_v4(expected_assets)
        or winner.processing_run_row_sha256
        != processing_run_row_sha256_v4(request)
    ):
        raise ValueError("atomic publication winner aggregate projection drifted")
    _validate_published_outbox_projection_v4(
        request=request,
        winner=winner,
    )
    durable = winner.durable_base_commit
    if (
        durable.source_identity_sha256
        != request.upstream_evidence.source_pdf_sha256
        or durable.source_page_count != request.source_page_count
        or durable.publish_precommit_at != winner.publish_precommit_at
        or durable.durable_base_sha256
        != durable_publish_base_sha256_v4(
            request=request,
            unit_assets=expected_assets,
            previous_active_run_id=winner.previous_active_run_id,
            updated_count=winner.updated_count,
            deleted_count=winner.deleted_count,
            outbox_commit=winner.outbox_commit,
            publish_precommit_at=winner.publish_precommit_at,
        )
    ):
        raise ValueError("atomic publication durable-base projection drifted")


@dataclass(frozen=True, slots=True)
class _NewActiveUnitProjectionV4:
    unit: PreIdUnitPublicationV4
    asset_id: str
    query_projection: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _UnitDiffV4:
    created: tuple[_NewActiveUnitProjectionV4, ...]
    removed: tuple[PreviousActiveUnitV4, ...]
    projection_changed: tuple[
        tuple[
            PreviousActiveUnitV4,
            _NewActiveUnitProjectionV4,
            tuple[str, ...],
        ],
        ...,
    ]


def _derive_unit_diff_v4(
    *,
    request: AtomicPublicationRequestV4,
    unit_assets: tuple[UnitAssetWinnerV4, ...],
) -> _UnitDiffV4:
    """Replay the canonical publication diff from closed old/new projections."""

    old_asset_ids = {item.asset_id for item in request.previous_active_units}
    new_asset_ids = {item.asset_id for item in unit_assets}
    if old_asset_ids.intersection(new_asset_ids):
        raise ValueError("transaction-assigned Unit IDs are not fresh")
    old_by_key: dict[tuple[str, str], list[PreviousActiveUnitV4]] = {}
    for old in request.previous_active_units:
        old_by_key.setdefault((old.payload_kind, old.content_hash), []).append(old)
    new_by_key: dict[tuple[str, str], list[_NewActiveUnitProjectionV4]] = {}
    for asset in unit_assets:
        unit = _request_unit(request, asset.unit_index)
        new = _NewActiveUnitProjectionV4(
            unit=unit,
            asset_id=asset.asset_id,
            query_projection=_new_unit_query_projection_v4(unit),
        )
        new_by_key.setdefault((unit.payload_kind, unit.content_hash), []).append(new)

    created: list[_NewActiveUnitProjectionV4] = []
    removed: list[PreviousActiveUnitV4] = []
    changed: list[
        tuple[
            PreviousActiveUnitV4,
            _NewActiveUnitProjectionV4,
            tuple[str, ...],
        ]
    ] = []
    for key in sorted(set(old_by_key) | set(new_by_key)):
        old_group = sorted(
            old_by_key.get(key, []),
            key=lambda item: (item.order_index, item.asset_id),
        )
        new_group = sorted(
            new_by_key.get(key, []),
            key=lambda item: (item.unit.unit_index, item.asset_id),
        )
        new_by_projection: dict[str, list[_NewActiveUnitProjectionV4]] = {}
        for new in new_group:
            new_by_projection.setdefault(
                new.unit.query_projection_hash,
                [],
            ).append(new)
        exact: list[
            tuple[PreviousActiveUnitV4, _NewActiveUnitProjectionV4]
        ] = []
        old_remaining: list[PreviousActiveUnitV4] = []
        for old in old_group:
            candidates = new_by_projection.get(old.query_projection_hash)
            if candidates:
                exact.append((old, candidates.pop(0)))
            else:
                old_remaining.append(old)
        exact_new_asset_ids = {new.asset_id for _old, new in exact}
        new_remaining = [
            new for new in new_group if new.asset_id not in exact_new_asset_ids
        ]
        pair_count = min(len(old_remaining), len(new_remaining))
        for old, new in zip(
            old_remaining[:pair_count],
            new_remaining[:pair_count],
            strict=True,
        ):
            if old.query_projection_hash != new.unit.query_projection_hash:
                changed.append(
                    (
                        old,
                        new,
                        _changed_projection_fields_v4(old=old, new=new),
                    )
                )
        removed.extend(old_remaining[pair_count:])
        created.extend(new_remaining[pair_count:])

    return _UnitDiffV4(
        created=tuple(
            sorted(
                created,
                key=lambda item: (item.unit.unit_index, item.asset_id),
            )
        ),
        removed=tuple(
            sorted(removed, key=lambda item: (item.order_index, item.asset_id))
        ),
        projection_changed=tuple(
            sorted(
                changed,
                key=lambda item: (item[1].unit.unit_index, item[1].asset_id),
            )
        ),
    )


def _new_unit_query_projection_v4(
    unit: PreIdUnitPublicationV4,
) -> dict[str, Any]:
    payload = strict_json_loads(unit.canonical_payload_json.encode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("publication Unit payload is not an object")
    return query_projection(
        payload_kind=unit.payload_kind,
        title=unit.title,
        heading_path=list(unit.heading_path),
        semantic_keys=(
            None if unit.semantic_keys is None else list(unit.semantic_keys)
        ),
        section_keys=(
            None if unit.section_keys is None else list(unit.section_keys)
        ),
        quality_status=unit.quality_status,
        applicability=unit.applicability,
        payload=cast(dict[str, Any], payload),
    )


def _changed_projection_fields_v4(
    *,
    old: PreviousActiveUnitV4,
    new: _NewActiveUnitProjectionV4,
) -> tuple[str, ...]:
    old_projection = strict_json_loads(
        old.canonical_query_projection_json.encode("utf-8")
    )
    if not isinstance(old_projection, dict):
        raise ValueError("previous-active Unit projection is not an object")
    old_projection = cast(dict[str, Any], old_projection)
    field_order = dict.fromkeys(
        (
            "payload_kind",
            "title",
            "heading_path",
            "semantic_key",
            "quality_status",
            "applicability",
            "semantic_keys",
            "section_keys",
            "mixed_part_annotations",
            *old_projection,
            *new.query_projection,
        )
    )
    changed = tuple(
        field
        for field in field_order
        if field != "payload_kind"
        and old_projection.get(field) != new.query_projection.get(field)
    )
    if not changed:
        raise ValueError(
            "query projection hashes differ although the projection is unchanged"
        )
    return changed


def _validate_published_outbox_projection_v4(
    *,
    request: AtomicPublicationRequestV4,
    winner: AtomicPublicationWinnerV4,
) -> None:
    events = winner.outbox_commit.events
    if any(item.occurred_at != winner.publish_precommit_at for item in events):
        raise ValueError("atomic publication outbox time drifted")
    supported = {
        "document_unit_removed",
        "document_unit_created",
        "document_unit_projection_changed",
        "processing_run_published",
    }
    if any(item.event_kind not in supported for item in events):
        raise ValueError("atomic publication outbox kind is unsupported")
    groups = {
        kind: tuple(item for item in events if item.event_kind == kind)
        for kind in supported
    }
    expected_order = (
        *groups["document_unit_removed"],
        *groups["document_unit_created"],
        *groups["document_unit_projection_changed"],
        *groups["processing_run_published"],
    )
    if events != expected_order or len(groups["processing_run_published"]) != 1:
        raise ValueError("atomic publication outbox order or cardinality drifted")

    diff = _derive_unit_diff_v4(
        request=request,
        unit_assets=winner.unit_assets,
    )
    if (
        len(groups["document_unit_created"]) != len(diff.created)
        or len(groups["document_unit_removed"]) != len(diff.removed)
        or len(groups["document_unit_projection_changed"])
        != len(diff.projection_changed)
    ):
        raise ValueError("atomic publication outbox diff cardinality drifted")

    for event, new in zip(
        groups["document_unit_created"],
        diff.created,
        strict=True,
    ):
        unit = new.unit
        expected_payload = {
            "content_hash": unit.content_hash,
            "new_asset_id": new.asset_id,
            "new_heading_path": list(unit.heading_path),
            "new_order_index": unit.unit_index,
            "new_processing_run_id": request.identity.processing_run_id,
            "payload_kind": unit.payload_kind,
        }
        if (
            event.document_id != request.identity.document_id
            or event.processing_run_id != request.identity.processing_run_id
            or event.asset_id != new.asset_id
            or event.change_kind != "materialized"
            or event.subject_kind != "document_unit"
            or event.subject_ref != new.asset_id
            or _event_payload(event) != expected_payload
        ):
            raise ValueError("created outbox event projection drifted")

    previous_run = request.identity.expected_previous_processing_run_id
    for event, old in zip(
        groups["document_unit_removed"],
        diff.removed,
        strict=True,
    ):
        expected_payload = {
            "content_hash": old.content_hash,
            "old_asset_id": old.asset_id,
            "old_heading_path": list(old.heading_path),
            "old_order_index": old.order_index,
            "old_processing_run_id": old.processing_run_id,
            "payload_kind": old.payload_kind,
        }
        if (
            event.document_id != request.identity.document_id
            or event.processing_run_id != old.processing_run_id
            or event.asset_id != old.asset_id
            or event.change_kind != "materialized"
            or event.subject_kind != "document_unit"
            or event.subject_ref != old.asset_id
            or _event_payload(event) != expected_payload
        ):
            raise ValueError("removed outbox event projection drifted")

    for event, (old, new, changed_fields) in zip(
        groups["document_unit_projection_changed"],
        diff.projection_changed,
        strict=True,
    ):
        expected_payload = {
            "changed_fields": list(changed_fields),
            "content_hash": new.unit.content_hash,
            "new_asset_id": new.asset_id,
            "new_query_projection_hash": new.unit.query_projection_hash,
            "old_asset_id": old.asset_id,
            "old_query_projection_hash": old.query_projection_hash,
        }
        if (
            event.document_id != request.identity.document_id
            or event.processing_run_id != request.identity.processing_run_id
            or event.asset_id != new.asset_id
            or event.change_kind != "materialized"
            or event.subject_kind != "document_unit"
            or event.subject_ref != new.asset_id
            or _event_payload(event) != expected_payload
        ):
            raise ValueError("changed outbox event projection drifted")

    projection = cast(
        dict[str, Any],
        strict_json_loads(request.processing_run_projection_json.encode("utf-8")),
    )
    created_count = len(diff.created)
    removed_count = len(diff.removed)
    changed_count = len(diff.projection_changed)
    published = groups["processing_run_published"][0]
    expected_change_kind = (
        "materialized"
        if previous_run is None or created_count or removed_count
        else "observed"
    )
    expected_published_payload = {
        "content_hash_aggregate": projection["content_hash_aggregate"],
        "created_count": created_count,
        "previous_processing_run_id": previous_run,
        "projection_changed_count": changed_count,
        "publish_committed_at": winner.publish_precommit_at.isoformat(),
        "removed_count": removed_count,
        "source_identity": request.upstream_evidence.source_pdf_sha256,
        "source_page_count": request.source_page_count,
        "structure_hash": projection["structure_hash_aggregate"],
        "unit_count": len(request.units),
    }
    if (
        published.document_id != request.identity.document_id
        or published.processing_run_id != request.identity.processing_run_id
        or published.asset_id is not None
        or published.change_kind != expected_change_kind
        or published.subject_kind != "processing_run"
        or published.subject_ref != request.identity.processing_run_id
        or _event_payload(published) != expected_published_payload
        or winner.updated_count != changed_count
        or winner.deleted_count != removed_count
    ):
        raise ValueError("processing-run-published outbox projection drifted")


def decode_atomic_publication_winner_v4(
    exact_bytes: bytes,
) -> AtomicPublicationWinnerV4:
    if type(exact_bytes) is not bytes or not 1 <= len(exact_bytes) <= _MAX_BYTES:
        raise ValueError("atomic publication winner bytes are outside the envelope")
    payload = strict_json_loads(exact_bytes)
    if not isinstance(payload, dict):
        raise ValueError("atomic publication winner must be an object")
    root = cast(dict[str, Any], payload)
    current_fields = {item.name for item in fields(AtomicPublicationWinnerV4)}
    legacy_fields = current_fields - {"artifact_readiness"}
    if set(root) == legacy_fields and root.get("winner_row_version") == 1:
        root = {**root, "artifact_readiness": None}
    elif set(root) != current_fields:
        raise ValueError("AtomicPublicationWinnerV4 fields are not closed")
    outbox = _decode_outbox_commit(root["outbox_commit"])
    durable_payload = root["durable_base_commit"]
    if not isinstance(durable_payload, dict):
        raise ValueError("DurablePublishBaseCommitReference must be an object")
    _closed(durable_payload, DurablePublishBaseCommitReference)
    durable = DurablePublishBaseCommitReference(
        **{
            **durable_payload,
            "publish_precommit_at": _datetime(
                durable_payload["publish_precommit_at"]
            ),
        }
    )
    raw_assets = root["unit_assets"]
    if not isinstance(raw_assets, list):
        raise ValueError("winner Unit assets must be an array")
    assets = tuple(_nested(item, UnitAssetWinnerV4) for item in raw_assets)
    readiness_payload = root["artifact_readiness"]
    readiness = (
        None
        if readiness_payload is None
        else _nested(
            readiness_payload,
            AtomicPublicationReadinessReferenceV1,
        )
    )
    value = AtomicPublicationWinnerV4(
        **{
            **root,
            "outbox_commit": outbox,
            "durable_base_commit": durable,
            "unit_assets": assets,
            "artifact_readiness": readiness,
            "publish_precommit_at": _datetime(root["publish_precommit_at"]),
        }
    )
    if value.canonical_bytes != exact_bytes:
        raise ValueError("atomic publication winner JSON is not canonical")
    return value


def _winner_payload(value: AtomicPublicationWinnerV4) -> dict[str, Any]:
    payload = asdict(value)
    payload["publish_precommit_at"] = _canonical_datetime(
        value.publish_precommit_at
    )
    payload["outbox_commit"] = _published_outbox_commit_payload(
        value.outbox_commit
    )
    payload["durable_base_commit"] = {
        **asdict(value.durable_base_commit),
        "publish_precommit_at": _canonical_datetime(
            value.durable_base_commit.publish_precommit_at
        ),
    }
    payload["unit_assets"] = [asdict(item) for item in value.unit_assets]
    if value.winner_row_version == 1:
        payload.pop("artifact_readiness", None)
    return payload


def _decode_outbox_commit(value: object) -> PublishedOutboxCommitReference:
    if not isinstance(value, dict):
        raise ValueError("PublishedOutboxCommitReference must be an object")
    root = cast(dict[str, Any], value)
    _closed(root, PublishedOutboxCommitReference)
    raw_events = root["events"]
    if not isinstance(raw_events, list):
        raise ValueError("outbox commit events must be an array")
    events: list[PublishedOutboxEventV4] = []
    for item in raw_events:
        if not isinstance(item, dict):
            raise ValueError("PublishedOutboxEventV4 must be an object")
        _closed(item, PublishedOutboxEventV4)
        events.append(
            PublishedOutboxEventV4(
                **{
                    **item,
                    "occurred_at": _datetime(item["occurred_at"]),
                }
            )
        )
    return PublishedOutboxCommitReference(
        **{
            **root,
            "events": tuple(events),
        }
    )


def _published_outbox_event_payload(
    event: PublishedOutboxEventV4,
) -> dict[str, Any]:
    return {
        "asset_id": event.asset_id,
        "canonical_payload_json": event.canonical_payload_json,
        "change_kind": event.change_kind,
        "document_id": event.document_id,
        "event_id": event.event_id,
        "event_sequence": event.event_sequence,
        "event_kind": event.event_kind,
        "occurred_at": _canonical_datetime(event.occurred_at),
        "processing_run_id": event.processing_run_id,
        "subject_kind": event.subject_kind,
        "subject_ref": event.subject_ref,
    }


def _published_outbox_event_values_payload(
    values: dict[str, Any],
) -> dict[str, Any]:
    occurred_at = values["occurred_at"]
    if not isinstance(occurred_at, datetime):
        raise ValueError("outbox occurred time must be datetime")
    return {
        "asset_id": values["asset_id"],
        "canonical_payload_json": values["canonical_payload_json"],
        "change_kind": values["change_kind"],
        "document_id": values["document_id"],
        "event_id": values["event_id"],
        "event_sequence": values["event_sequence"],
        "event_kind": values["event_kind"],
        "occurred_at": _canonical_datetime(occurred_at),
        "processing_run_id": values["processing_run_id"],
        "subject_kind": values["subject_kind"],
        "subject_ref": values["subject_ref"],
    }


def _published_outbox_commit_payload(
    reference: PublishedOutboxCommitReference,
) -> dict[str, Any]:
    return {
        "document_id": reference.document_id,
        "event_count": reference.event_count,
        "events": [
            {
                **_published_outbox_event_payload(item),
                "event_row_sha256": item.event_row_sha256,
            }
            for item in reference.events
        ],
        "events_sha256": reference.events_sha256,
        "first_event_id": reference.first_event_id,
        "last_event_id": reference.last_event_id,
        "processing_run_id": reference.processing_run_id,
        "processing_run_published_event_id": (
            reference.processing_run_published_event_id
        ),
        "processing_run_published_event_sha256": (
            reference.processing_run_published_event_sha256
        ),
    }


def _event_payload(event: PublishedOutboxEventV4) -> dict[str, Any]:
    payload = strict_json_loads(event.canonical_payload_json.encode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("outbox payload must be an object")
    return cast(dict[str, Any], payload)


def _request_unit(
    request: AtomicPublicationRequestV4,
    unit_index: int,
) -> PreIdUnitPublicationV4:
    _positive(unit_index, "winner unit index")
    if unit_index > len(request.units):
        raise ValueError("winner unit index is outside the publication request")
    unit = request.units[unit_index - 1]
    if unit.unit_index != unit_index:
        raise ValueError("publication request Unit order is not canonical")
    return unit


def _nested(value: object, item_type: type[Any]) -> Any:
    if not isinstance(value, dict):
        raise ValueError(f"{item_type.__name__} must be an object")
    root = cast(dict[str, Any], value)
    _closed(root, item_type)
    return item_type(**root)


def _closed(value: dict[str, Any], item_type: type[Any]) -> None:
    if set(value) != {item.name for item in fields(item_type)}:
        raise ValueError(f"{item_type.__name__} fields are not closed")


def _canonical_json(value: object) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if not 1 <= len(encoded) <= _MAX_BYTES:
        raise ValueError("atomic publication winner bytes are outside the envelope")
    return encoded


def _canonical_json_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be canonical JSON text")
    exact = value.encode("utf-8")
    decoded = strict_json_loads(exact)
    if _canonical_json(decoded) != exact:
        raise ValueError(f"{label} is not canonical JSON")


def _canonical_datetime(value: datetime) -> str:
    _utc(value, "canonical time")
    return value.isoformat().replace("+00:00", "Z")


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and item for item in value
    )


def _datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("publish precommit time is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("publish precommit time is invalid") from exc
    _utc(parsed, "publish precommit time")
    return parsed


def _identity(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"{label} identity is invalid")


def _sha(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{label} hash is not canonical")


def _asset_id(value: str) -> None:
    if not isinstance(value, str) or _ASSET_ID.fullmatch(value) is None:
        raise ValueError("winner asset is not a canonical Unit ID")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _positive(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be positive")


def _nonnegative(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be non-negative")


def _publication_lifecycle_version(value: int, label: str) -> None:
    if type(value) is not int or not 0 <= value < _MAX_INT:
        raise ValueError(f"{label} cannot admit an exact signed-BIGINT successor")


def _utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be timezone aware")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{label} must be UTC")


__all__ = [
    "ATOMIC_PUBLICATION_WINNER_V4_CONTRACT",
    "AtomicPublicationCommitResponseLost",
    "AtomicPublicationUniqueConflict",
    "AtomicPublicationWinnerV4",
    "AtomicPublicationWinnerReaderV4Port",
    "AtomicWholeDocumentPublisherV4Port",
    "build_atomic_publication_outbox_events_v4",
    "DurablePublishBaseCommitReference",
    "PublishedOutboxCommitReference",
    "PublishedOutboxEventV4",
    "UnitAssetWinnerV4",
    "decode_atomic_publication_winner_v4",
    "durable_publish_base_sha256_v4",
    "final_unit_row_sha256_v4",
    "final_unit_rows_sha256_v4",
    "lineage_row_sha256_v4",
    "lineage_rows_sha256_v4",
    "processing_run_row_sha256_v4",
    "seal_atomic_publication_winner_v4",
    "seal_published_outbox_commit_reference_v4",
    "seal_published_outbox_event_v4",
    "seal_unit_asset_winners_v4",
    "published_outbox_event_row_sha256_v4",
    "published_outbox_events_sha256_v4",
    "validate_atomic_publication_winner_v4",
    "validate_atomic_publication_artifacts_ready_v4",
    "validate_atomic_publication_claim_v4",
]
