"""PostgreSQL implementation of the durable remote-parse V4 authority."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, NoReturn, cast

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.application.contracts.provider_secret_envelope_v4 import (
    ProviderSecretBindingV4,
    SealedProviderSecretV4,
    validate_provider_secret_revision_history_v4,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    EncodedRemoteParseEvidenceV4,
    LocalCleanupPlanV4,
    LocalCleanupReceiptV4,
    PreparationIntentV4,
    ProviderAckReceiptV4,
    SnapshotReceiptV4,
    SubmissionIntentV4,
    SupersessionReceiptV4,
    decode_remote_parse_evidence_v4,
    encode_remote_parse_evidence_v4,
    validate_durable_remote_parse_evidence_bundle_v4,
    validate_superseding_checkpoint_seed_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    RemoteParseCheckpointV4,
    ResourceReservationV4,
    decode_remote_parse_checkpoint_v4,
    decode_resource_reservation_v4,
    validate_remote_parse_checkpoint_successor_v4,
)
from disclosure_anchor.application.contracts.staged_credit import (
    DatabaseLeaseSnapshot,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationWinnerV4,
    decode_atomic_publication_winner_v4,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    LegacyCurrentRemoteParseAuthority,
    RecoveryCandidate,
    RemoteParseV4Authority,
    RemoteParseV4AuthorityViolation,
    V4AttemptFinal,
    V4ClaimGenerationExhausted,
    V4ClaimHeldByOther,
    V4ClaimLost,
    V4DifferentSuccessorCommitted,
    V4DocumentCurrentConflict,
    V4GenerationConflict,
    V4HeadExpectation,
    V4HeadNotFound,
    V4HeadStale,
    V4PreparedCreation,
    V4ResourceFreeFailureCreation,
    V4ResourceFreeSupersessionCreation,
    V4SecretRevisionConflict,
    V4SecretRewrap,
    V4SuccessorAppend,
    V4SuccessorNotCommitted,
    V4SuccessorReconciliation,
    V4SupersessionLinkAuthority,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    V4ClaimWitness,
)
from disclosure_anchor.application.worker.locks import acquire_document_xact_lock

_MAX_INT = (1 << 63) - 1
_CURRENT_STATES = {
    "prepared",
    "reconciling",
    "submitted",
    "remote_terminal",
    "materializing",
    "local_materialized",
    "publish_committed",
    "cleanup_pending",
    "ack_pending",
}
_FINAL_STATES = {
    "acked",
    "remote_failed",
    "local_failed",
    "pre_submission_failed",
    "preparation_failed",
    "superseded",
}
_EVIDENCE_FIELDS = (
    "preparation_intent_sha256",
    "snapshot_receipt_sha256",
    "submission_intent_sha256",
    "accepted_submission_sha256",
    "terminal_receipt_sha256",
    "materialization_intent_sha256",
    "local_materialization_receipt_sha256",
    "failure_receipt_sha256",
    "supersession_receipt_sha256",
    "cleanup_plan_sha256",
    "cleanup_receipt_sha256",
    "ack_receipt_sha256",
)
_HELD_CREDIT_FIELDS = (
    "documents",
    "snapshot_items",
    "snapshot_bytes",
    "remote_waits",
    "provider_tasks",
    "provider_result_bytes",
    "materialization_items",
    "compressed_bytes",
    "decoded_bytes",
    "temp_disk_bytes",
    "output_items",
    "output_bytes",
    "output_pages",
    "ack_items",
)
_V4_DEFERRED_CONSTRAINTS = (
    "fk_remote_parse_attempt_v4_current_checkpoint",
    "fk_remote_parse_v4_evidence_parent",
    "fk_remote_parse_v4_checkpoint_parent",
    "fk_remote_parse_v4_checkpoint_predecessor",
    *(
        f"fk_remote_parse_v4_checkpoint_{name[:-7]}"
        for name in _EVIDENCE_FIELDS
    ),
    "fk_remote_parse_v4_checkpoint_publication_winner",
    "fk_atomic_publication_winner_v4_parent",
    "fk_atomic_publication_winner_v4_checkpoint",
    "fk_remote_parse_v4_supersession_link_source",
    "fk_remote_parse_v4_supersession_link_receipt",
    "fk_remote_parse_v4_supersession_link_target",
    "fk_remote_parse_v4_supersession_link_checkpoint",
    "ck_remote_parse_v4_evidence_v4_parent",
    "ck_remote_parse_v4_checkpoint_v4_parent",
    "ck_atomic_publication_winner_v4_v4_parent",
    "ck_remote_parse_v4_secret_v4_parent",
    "ck_remote_parse_v4_checkpoint_chain",
    "ck_remote_parse_v4_checkpoint_references",
    "ck_remote_parse_v4_evidence_referenced",
    "ck_atomic_publication_winner_v4_referenced",
    "ck_remote_parse_v4_head",
    "ck_remote_parse_v4_secret_lifecycle_head",
    "ck_remote_parse_v4_secret_lifecycle_row",
    "ck_remote_parse_v4_secret_history",
    "ck_remote_parse_v4_secret_delete",
    "ck_remote_parse_v4_supersession_link_relations",
    "ck_remote_parse_v4_supersession_head_relations",
)


class RemoteParseV4Repository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def load(self, attempt_id: str) -> RemoteParseV4Authority:
        _identity(attempt_id, "attempt")
        try:
            head = self._lock_authority_heads(attempt_id)
            return self._strict_authority(head)
        except V4HeadNotFound:
            raise
        except RemoteParseV4AuthorityViolation:
            raise
        except (LookupError, TypeError, ValueError) as exc:
            raise RemoteParseV4AuthorityViolation(
                "persisted v4 authority cannot be reconstructed"
            ) from exc

    def load_current_for_document(
        self,
        document_id: str,
    ) -> RemoteParseV4Authority | LegacyCurrentRemoteParseAuthority | None:
        _identity(document_id, "document")
        table = models.RemoteParseAttempt.__table__
        row = self._session.execute(
            sa.select(table)
            .where(
                table.c.document_id == document_id,
                table.c.is_current.is_(True),
            )
            .with_for_update(read=True)
        ).mappings().one_or_none()
        if row is None:
            return None
        if row["checkpoint_contract_version"] != 4:
            return LegacyCurrentRemoteParseAuthority(
                attempt_id=row["attempt_id"],
                document_id=row["document_id"],
                checkpoint_contract_version=row["checkpoint_contract_version"],
                state=row["state"],
            )
        try:
            return self._strict_authority(row)
        except RemoteParseV4AuthorityViolation:
            raise
        except (LookupError, TypeError, ValueError) as exc:
            raise RemoteParseV4AuthorityViolation(
                "current v4 authority cannot be reconstructed"
            ) from exc

    def list_recoverable_heads(
        self,
        *,
        after_attempt_id: str | None,
        limit: int,
    ) -> tuple[RecoveryCandidate, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("v4 recovery page limit is outside 1..1000")
        if after_attempt_id is not None:
            _identity(after_attempt_id, "recovery cursor")

        table = models.RemoteParseAttempt.__table__
        clock = self._database_clock()
        ordered_attempt_id = sa.collate(table.c.attempt_id, "C")
        predicates = [
            table.c.checkpoint_contract_version == 4,
            table.c.is_current.is_(True),
        ]
        if after_attempt_id is not None:
            predicates.append(ordered_attempt_id > after_attempt_id)
        statement = (
            sa.select(
                table.c.attempt_id,
                table.c.checkpoint_contract_version,
                table.c.state,
                table.c.is_current,
                table.c.row_version,
                table.c.claim_generation,
                table.c.claim_owner_identity,
                table.c.claim_lease_until,
                clock.c.database_observed_at,
            )
            .select_from(table.join(clock, sa.true()))
            .where(*predicates)
            .order_by(ordered_attempt_id)
            .limit(limit)
        )
        try:
            rows = self._session.execute(statement).mappings().all()
        except DBAPIError as exc:
            self._raise_authority_dbapi(
                exc,
                "v4 recovery backlog could not be read",
            )

        observed_at: datetime | None = None
        candidates: list[RecoveryCandidate] = []
        for row in rows:
            raw_observed_at = row["database_observed_at"]
            if not isinstance(raw_observed_at, datetime):
                raise RemoteParseV4AuthorityViolation(
                    "v4 recovery database clock is invalid"
                )
            row_observed_at = _utc(raw_observed_at)
            if observed_at is None:
                observed_at = row_observed_at
            elif row_observed_at != observed_at:
                raise RemoteParseV4AuthorityViolation(
                    "v4 recovery page used more than one database clock"
                )
            candidates.append(
                recovery_candidate_from_head_row(
                    row,
                    database_observed_at=observed_at,
                )
            )

        result = tuple(candidates)
        identities = tuple(item.attempt_id for item in result)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(
            identities
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 recovery page is not strictly byte ordered"
            )
        if after_attempt_id is not None and any(
            identity <= after_attempt_id for identity in identities
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 recovery page crossed its keyset cursor"
            )
        return result

    def list_unclaimed_prepared_heads(
        self,
        *,
        after_attempt_id: str | None,
        limit: int,
    ) -> tuple[RecoveryCandidate, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("v4 admission page limit is outside 1..1000")
        if after_attempt_id is not None:
            _identity(after_attempt_id, "admission cursor")

        table = models.RemoteParseAttempt.__table__
        clock = self._database_clock()
        ordered_attempt_id = sa.collate(table.c.attempt_id, "C")
        predicates = [
            table.c.checkpoint_contract_version == 4,
            table.c.is_current.is_(True),
            table.c.state == "prepared",
            table.c.row_version == 0,
            table.c.claim_generation == 0,
            table.c.claim_owner_identity.is_(None),
            table.c.claim_lease_until.is_(None),
        ]
        if after_attempt_id is not None:
            predicates.append(ordered_attempt_id > after_attempt_id)
        statement = (
            sa.select(
                table.c.attempt_id,
                table.c.checkpoint_contract_version,
                table.c.state,
                table.c.is_current,
                table.c.row_version,
                table.c.claim_generation,
                table.c.claim_owner_identity,
                table.c.claim_lease_until,
                clock.c.database_observed_at,
            )
            .select_from(table.join(clock, sa.true()))
            .where(*predicates)
            .order_by(ordered_attempt_id)
            .limit(limit)
        )
        try:
            rows = self._session.execute(statement).mappings().all()
        except DBAPIError as exc:
            self._raise_authority_dbapi(
                exc,
                "v4 admission backlog could not be read",
            )

        observed_at: datetime | None = None
        candidates: list[RecoveryCandidate] = []
        for row in rows:
            raw_observed_at = row["database_observed_at"]
            if not isinstance(raw_observed_at, datetime):
                raise RemoteParseV4AuthorityViolation(
                    "v4 admission database clock is invalid"
                )
            row_observed_at = _utc(raw_observed_at)
            if observed_at is None:
                observed_at = row_observed_at
            elif row_observed_at != observed_at:
                raise RemoteParseV4AuthorityViolation(
                    "v4 admission page used more than one database clock"
                )
            candidates.append(
                recovery_candidate_from_head_row(
                    row,
                    database_observed_at=observed_at,
                )
            )

        result = tuple(candidates)
        identities = tuple(item.attempt_id for item in result)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(
            identities
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 admission page is not strictly byte ordered"
            )
        if after_attempt_id is not None and any(
            identity <= after_attempt_id for identity in identities
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 admission page crossed its keyset cursor"
            )
        return result

    def create_prepared(
        self,
        creation: V4PreparedCreation,
    ) -> RemoteParseV4Authority:
        if type(creation) is not V4PreparedCreation:
            raise ValueError("v4 prepared creation must be exact")
        checkpoint = creation.checkpoint
        rows = self._lock_creation_chain(checkpoint.document_id)
        existing = self._creation_row(rows, checkpoint.attempt_id)
        if existing is not None:
            authority = self._strict_authority(existing)
            self._require_prepared_replay(authority, creation)
            return authority
        self._guard_creation_chain(
            rows=rows,
            generations=(checkpoint.attempt_generation,),
            allow_existing_current=False,
        )
        try:
            with self._session.begin_nested():
                self._insert_prepared_creation(creation, is_current=True)
                self._force_v4_constraints()
                authority = self._strict_authority(
                    self._select_head(checkpoint.attempt_id, lock="update")
                )
        except IntegrityError as exc:
            self._raise_creation_integrity(exc)
        except DBAPIError as exc:
            self._raise_authority_dbapi(
                exc,
                "v4 prepared creation violated durable authority",
            )
        return authority

    def create_resource_free_failure(
        self,
        creation: V4ResourceFreeFailureCreation,
    ) -> RemoteParseV4Authority:
        if type(creation) is not V4ResourceFreeFailureCreation:
            raise ValueError("v4 resource-free failure creation must be exact")
        checkpoint = creation.checkpoint
        rows = self._lock_creation_chain(checkpoint.document_id)
        existing = self._creation_row(rows, checkpoint.attempt_id)
        if existing is not None:
            authority = self._strict_authority(existing)
            self._require_failure_replay(authority, creation)
            return authority
        self._guard_creation_chain(
            rows=rows,
            generations=(checkpoint.attempt_generation,),
            allow_existing_current=True,
            allow_existing_staged=True,
        )
        try:
            with self._session.begin_nested():
                self._insert_head(
                    checkpoint=checkpoint,
                    parser_target_sha256=creation.parser_target_sha256,
                    client_submit_key=creation.client_submit_key,
                    is_current=False,
                )
                self._insert_evidence(
                    encode_remote_parse_evidence_v4(creation.failure_receipt)
                )
                self._insert_checkpoint(checkpoint, reservation=None)
                self._force_v4_constraints()
                authority = self._strict_authority(
                    self._select_head(checkpoint.attempt_id, lock="update")
                )
        except IntegrityError as exc:
            self._raise_creation_integrity(exc)
        except DBAPIError as exc:
            self._raise_authority_dbapi(
                exc,
                "v4 resource-free failure creation violated durable authority",
            )
        return authority

    def create_resource_free_supersession(
        self,
        creation: V4ResourceFreeSupersessionCreation,
    ) -> tuple[RemoteParseV4Authority, RemoteParseV4Authority]:
        if type(creation) is not V4ResourceFreeSupersessionCreation:
            raise ValueError("v4 resource-free supersession creation must be exact")
        source = creation.source_checkpoint
        target = creation.superseding.checkpoint
        rows = self._lock_creation_chain(source.document_id)
        source_existing = self._creation_row(rows, source.attempt_id)
        target_existing = self._creation_row(rows, target.attempt_id)
        if source_existing is not None or target_existing is not None:
            if source_existing is None or target_existing is None:
                raise V4GenerationConflict(
                    "v4 resource-free supersession replay is partial"
                )
            source_authority = self._strict_authority(source_existing)
            target_authority = self._strict_authority(target_existing)
            self._require_resource_free_supersession_replay(
                source_authority,
                target_authority,
                creation,
            )
            return source_authority, target_authority
        self._guard_creation_chain(
            rows=rows,
            generations=(
                source.attempt_generation,
                target.attempt_generation,
            ),
            allow_existing_current=False,
            allow_existing_staged=False,
        )
        try:
            with self._session.begin_nested():
                self._insert_head(
                    checkpoint=source,
                    parser_target_sha256=creation.source_parser_target_sha256,
                    client_submit_key=creation.source_client_submit_key,
                    is_current=False,
                )
                self._insert_evidence(
                    encode_remote_parse_evidence_v4(
                        creation.supersession_receipt
                    )
                )
                self._insert_checkpoint(source, reservation=None)
                self._insert_prepared_creation(
                    creation.superseding,
                    is_current=True,
                )
                self._session.execute(
                    sa.insert(models.RemoteParseV4SupersessionLink).values(
                        source_attempt_id=source.attempt_id,
                        source_fence_identity=source.fence_identity,
                        source_evidence_kind="supersession_receipt",
                        source_supersession_receipt_sha256=(
                            creation.supersession_receipt.sha256
                        ),
                        superseding_attempt_id=target.attempt_id,
                        superseding_fence_identity=target.fence_identity,
                        superseding_lifecycle_version=0,
                        superseding_checkpoint_sha256=target.sha256,
                    )
                )
                self._force_v4_constraints()
                source_authority = self._strict_authority(
                    self._select_head(source.attempt_id, lock="update")
                )
                target_authority = self._strict_authority(
                    self._select_head(target.attempt_id, lock="update")
                )
        except IntegrityError as exc:
            self._raise_creation_integrity(exc)
        except DBAPIError as exc:
            self._raise_authority_dbapi(
                exc,
                "v4 resource-free supersession creation violated durable authority",
            )
        return source_authority, target_authority

    def claim(
        self,
        expectation: V4HeadExpectation,
        *,
        owner_identity: str,
        lease_seconds: int,
    ) -> RemoteParseV4Authority:
        try:
            with self._session.begin_nested():
                authority = self._claim_once(
                    expectation,
                    owner_identity=owner_identity,
                    lease_seconds=lease_seconds,
                )
                self._force_v4_constraints()
        except DBAPIError as exc:
            self._raise_authority_dbapi(
                exc,
                "v4 claim violated durable authority",
            )
        return authority

    def _claim_once(
        self,
        expectation: V4HeadExpectation,
        *,
        owner_identity: str,
        lease_seconds: int,
    ) -> RemoteParseV4Authority:
        self._validate_claim_request(
            expectation=expectation,
            owner_identity=owner_identity,
            lease_seconds=lease_seconds,
        )
        table = models.RemoteParseAttempt.__table__
        locked = self._session.execute(
            sa.select(table)
            .where(table.c.attempt_id == expectation.attempt_id)
            .with_for_update()
        ).mappings().one_or_none()
        if locked is None:
            raise V4HeadNotFound("v4 claim head is absent")
        self._require_expected_head(locked, expectation)
        self._strict_authority(locked, observe_database_clock=False)
        observed_owner = locked["claim_owner_identity"]
        observed_generation = locked["claim_generation"]
        observed_lease = locked["claim_lease_until"]

        clock = self._database_clock()
        live_same_owner = sa.and_(
            table.c.claim_owner_identity == owner_identity,
            table.c.claim_lease_until > clock.c.database_observed_at,
        )
        acquirable = sa.and_(
            sa.or_(
                table.c.claim_owner_identity.is_(None),
                table.c.claim_lease_until <= clock.c.database_observed_at,
            ),
            table.c.claim_generation < _MAX_INT,
        )
        candidate_lease = clock.c.database_observed_at + sa.func.make_interval(
            0, 0, 0, 0, 0, 0, lease_seconds
        )
        statement = (
            sa.update(models.RemoteParseAttempt)
            .where(
                table.c.attempt_id == expectation.attempt_id,
                table.c.fence_identity == expectation.fence_identity,
                table.c.checkpoint_contract_version == 4,
                table.c.is_current.is_(True),
                table.c.state == expectation.state,
                table.c.row_version == expectation.lifecycle_version,
                table.c.current_checkpoint_sha256
                == expectation.checkpoint_sha256,
                table.c.claim_generation == observed_generation,
                table.c.claim_owner_identity.is_not_distinct_from(observed_owner),
                table.c.claim_lease_until.is_not_distinct_from(observed_lease),
                sa.or_(live_same_owner, acquirable),
            )
            .values(
                claim_generation=sa.case(
                    (live_same_owner, table.c.claim_generation),
                    else_=table.c.claim_generation + 1,
                ),
                claim_owner_identity=owner_identity,
                claim_lease_until=sa.case(
                    (
                        live_same_owner,
                        sa.func.greatest(table.c.claim_lease_until, candidate_lease),
                    ),
                    else_=candidate_lease,
                ),
                updated_at=clock.c.database_observed_at,
            )
            .returning(
                table.c.attempt_id,
                clock.c.database_observed_at,
            )
        )
        result = self._session.execute(statement).one_or_none()
        if result is None:
            observed_at = self._observe_database_time()
            if (
                observed_owner is not None
                and observed_lease is not None
                and _utc(observed_lease) > observed_at
                and observed_owner != owner_identity
            ):
                raise V4ClaimHeldByOther("v4 head has a live foreign claim")
            if observed_generation >= _MAX_INT:
                raise V4ClaimGenerationExhausted(
                    "v4 claim generation is exhausted"
                )
            raise V4HeadStale("v4 claim lost exact head CAS")
        return self._strict_authority(
            self._select_head(expectation.attempt_id, lock="update")
        )

    def renew(
        self,
        claim: V4ClaimWitness,
        *,
        lease_seconds: int,
    ) -> RemoteParseV4Authority:
        try:
            with self._session.begin_nested():
                authority = self._renew_once(
                    claim,
                    lease_seconds=lease_seconds,
                )
                self._force_v4_constraints()
        except DBAPIError as exc:
            self._raise_authority_dbapi(
                exc,
                "v4 claim renewal violated durable authority",
            )
        return authority

    def _renew_once(
        self,
        claim: V4ClaimWitness,
        *,
        lease_seconds: int,
    ) -> RemoteParseV4Authority:
        if type(claim) is not V4ClaimWitness:
            raise ValueError("v4 renew requires an exact claim witness")
        expectation = V4HeadExpectation(
            attempt_id=claim.attempt_id,
            fence_identity=claim.fence_identity,
            state=claim.state,
            lifecycle_version=claim.lifecycle_version,
            checkpoint_sha256=claim.checkpoint_sha256,
        )
        self._validate_claim_request(
            expectation=expectation,
            owner_identity=claim.claim_owner_identity,
            lease_seconds=lease_seconds,
        )
        locked = self._select_head(claim.attempt_id, lock="update")
        self._require_expected_head(locked, expectation)
        self._strict_authority(locked, observe_database_clock=False)
        table = models.RemoteParseAttempt.__table__
        clock = self._database_clock()
        candidate_lease = clock.c.database_observed_at + sa.func.make_interval(
            0, 0, 0, 0, 0, 0, lease_seconds
        )
        result = self._session.execute(
            sa.update(models.RemoteParseAttempt)
            .where(
                table.c.attempt_id == claim.attempt_id,
                table.c.fence_identity == claim.fence_identity,
                table.c.checkpoint_contract_version == 4,
                table.c.is_current.is_(True),
                table.c.state == claim.state,
                table.c.row_version == claim.lifecycle_version,
                table.c.current_checkpoint_sha256 == claim.checkpoint_sha256,
                table.c.claim_owner_identity == claim.claim_owner_identity,
                table.c.claim_generation == claim.claim_generation,
                table.c.claim_lease_until > clock.c.database_observed_at,
            )
            .values(
                claim_lease_until=sa.func.greatest(
                    table.c.claim_lease_until,
                    candidate_lease,
                ),
                updated_at=clock.c.database_observed_at,
            )
            .returning(table.c.attempt_id)
        ).one_or_none()
        if result is None:
            raise V4ClaimLost("v4 renewal lost exact live claim")
        return self._strict_authority(
            self._select_head(claim.attempt_id, lock="update")
        )

    def reload_claimed(
        self,
        claim: V4ClaimWitness,
        *,
        lock_for_transition: bool = False,
    ) -> RemoteParseV4Authority:
        if type(claim) is not V4ClaimWitness or type(lock_for_transition) is not bool:
            raise ValueError("v4 claimed reload request is invalid")
        head = (
            self._lock_append_heads(claim.attempt_id)
            if lock_for_transition
            else self._select_head(claim.attempt_id, lock="share")
        )
        if head["checkpoint_contract_version"] != 4:
            raise V4HeadStale("v4 claimed reload crossed contract version")
        if head["state"] in _FINAL_STATES:
            raise V4AttemptFinal("v4 claimed attempt is final")
        if (
            not head["is_current"]
            or head["fence_identity"] != claim.fence_identity
            or head["state"] != claim.state
            or head["row_version"] != claim.lifecycle_version
            or head["current_checkpoint_sha256"] != claim.checkpoint_sha256
        ):
            raise V4HeadStale("v4 claimed reload lost exact head")
        if (
            head["claim_owner_identity"] != claim.claim_owner_identity
            or head["claim_generation"] != claim.claim_generation
        ):
            raise V4ClaimLost("v4 claimed reload lost owner or generation")
        authority = self._strict_authority(head)
        if (
            authority.database_lease is None
            or authority.database_lease.remaining_microseconds <= 0
        ):
            raise V4ClaimLost("v4 claimed reload lease expired")
        return authority

    def append_successor(
        self,
        append: V4SuccessorAppend,
    ) -> RemoteParseV4Authority:
        if type(append) is not V4SuccessorAppend:
            raise ValueError("v4 successor append must be exact")
        acquire_document_xact_lock(self._session, append.successor.document_id)
        try:
            with self._session.begin_nested():
                head = self._lock_append_heads(append.claim.attempt_id)
                authority = self._strict_authority(
                    head,
                    observe_database_clock=False,
                )
                previous = authority.checkpoint
                self._require_append_predecessor(authority, append)
                try:
                    validate_remote_parse_checkpoint_successor_v4(
                        previous,
                        append.successor,
                    )
                except ValueError as exc:
                    raise V4HeadStale(
                        "v4 proposed successor is not valid for the locked head"
                    ) from exc
                self._require_new_evidence_exact(previous, append)
                self._require_append_companions(authority, append)
                self._cas_successor_head(append)
                for evidence in append.new_evidence:
                    self._insert_evidence(evidence)
                if append.sealed_secret is not None:
                    accepted_sha256 = append.successor.accepted_submission_sha256
                    assert accepted_sha256 is not None
                    self._insert_secret(
                        append.sealed_secret,
                        accepted_submission_sha256=accepted_sha256,
                    )
                if append.publication_winner is not None:
                    self._insert_winner(append.publication_winner)
                self._insert_checkpoint(append.successor, reservation=None)
                if append.staged_superseder is not None:
                    self._insert_resourceful_superseder(
                        append,
                        authority=authority,
                    )
                if append.successor.state == "superseded":
                    self._activate_staged_superseder(authority, append)
                if append.successor.state in _FINAL_STATES:
                    self._purge_final_secrets(
                        append=append,
                        expected_revision_max=len(authority.secret_history),
                    )
                self._force_v4_constraints()
                result = self._strict_authority(
                    self._select_head(append.claim.attempt_id, lock="update")
                )
        except IntegrityError as exc:
            self._raise_creation_integrity(exc)
        except DBAPIError as exc:
            self._raise_authority_dbapi(
                exc,
                "v4 successor append violated durable authority",
            )
        return result

    def reconcile_successor(
        self,
        append: V4SuccessorAppend,
    ) -> V4SuccessorReconciliation:
        if type(append) is not V4SuccessorAppend:
            raise ValueError("v4 successor reconciliation must be exact")
        head = self._lock_authority_heads(append.claim.attempt_id)
        authority = self._strict_authority(head)
        if authority.lifecycle_version == append.claim.lifecycle_version:
            if authority.checkpoint_sha256 != append.claim.checkpoint_sha256:
                raise V4DifferentSuccessorCommitted(
                    "v4 predecessor version has a different checkpoint"
                )
            if self._claim_is_live(authority, append.claim):
                raise V4SuccessorNotCommitted(
                    "v4 successor is not durably committed"
                )
            raise V4ClaimLost(
                "v4 successor is absent and predecessor authorization was lost"
            )
        if authority.lifecycle_version == append.successor.lifecycle_version:
            if authority.checkpoint_sha256 != append.successor.sha256:
                raise V4DifferentSuccessorCommitted(
                    "a different v4 successor is durably committed"
                )
            self._require_committed_successor_exact(authority, append)
            return V4SuccessorReconciliation(
                authority=authority,
                authorization_still_live=self._claim_is_live(
                    authority,
                    append.claim,
                ),
            )
        raise V4HeadStale(
            "v4 authority advanced beyond the proposed successor"
        )

    def rewrap_secret(
        self,
        rewrap: V4SecretRewrap,
    ) -> tuple[SealedProviderSecretV4, ...]:
        if type(rewrap) is not V4SecretRewrap:
            raise ValueError("v4 secret rewrap must be exact")
        try:
            with self._session.begin_nested():
                head = self._select_head(rewrap.attempt_id, lock="update")
                authority = self._strict_authority(
                    head,
                    observe_database_clock=False,
                )
                if authority.fence_identity != rewrap.fence_identity:
                    raise V4HeadStale("v4 secret rewrap fence is stale")
                if not authority.is_current or authority.state in _FINAL_STATES:
                    raise V4AttemptFinal(
                        "v4 secret rewrap requires current non-final authority"
                    )
                history = authority.secret_history
                if not history:
                    raise V4SecretRevisionConflict(
                        "v4 secret rewrap lacks revision one"
                    )
                proposed = rewrap.rewrapped
                current = history[-1]
                if proposed.encryption_revision == current.encryption_revision:
                    if proposed != current:
                        raise V4SecretRevisionConflict(
                            "v4 secret rewrap replay differs from current revision"
                        )
                    return history
                if proposed.encryption_revision != current.encryption_revision + 1:
                    raise V4SecretRevisionConflict(
                        "v4 secret rewrap is not the next revision"
                    )
                try:
                    validate_provider_secret_revision_history_v4(
                        (*history, proposed)
                    )
                except ValueError as exc:
                    raise V4SecretRevisionConflict(
                        "v4 secret rewrap changed immutable data-layer fields"
                    ) from exc
                accepted_sha256 = authority.checkpoint.accepted_submission_sha256
                if accepted_sha256 is None:
                    raise RemoteParseV4AuthorityViolation(
                        "v4 secret history lacks accepted checkpoint evidence"
                    )
                self._insert_secret(
                    proposed,
                    accepted_submission_sha256=accepted_sha256,
                )
                self._force_v4_constraints()
                result = self._strict_authority(
                    self._select_head(rewrap.attempt_id, lock="update"),
                    observe_database_clock=False,
                ).secret_history
        except DBAPIError as exc:
            self._raise_authority_dbapi(
                exc,
                "v4 secret rewrap violated durable authority",
            )
        return result

    def _lock_append_heads(self, attempt_id: str) -> Mapping[Any, Any]:
        table = models.RemoteParseAttempt.__table__
        link = models.RemoteParseV4SupersessionLink.__table__
        target_id = self._session.execute(
            sa.select(link.c.superseding_attempt_id).where(
                link.c.source_attempt_id == attempt_id
            )
        ).scalar_one_or_none()
        attempt_ids = (attempt_id,) if target_id is None else (attempt_id, target_id)
        rows = self._session.execute(
            sa.select(table)
            .where(table.c.attempt_id.in_(attempt_ids))
            .order_by(table.c.attempt_generation, table.c.attempt_id)
            .with_for_update()
        ).mappings().all()
        by_id = {row["attempt_id"]: row for row in rows}
        head = by_id.get(attempt_id)
        if head is None:
            raise V4HeadNotFound("v4 successor head is absent")
        if target_id is not None and target_id not in by_id:
            raise RemoteParseV4AuthorityViolation(
                "v4 linked superseding head is absent"
            )
        return head

    @staticmethod
    def _require_append_predecessor(
        authority: RemoteParseV4Authority,
        append: V4SuccessorAppend,
    ) -> None:
        if authority.state in _FINAL_STATES:
            raise V4AttemptFinal("v4 successor predecessor is final")
        if (
            not authority.is_current
            or authority.attempt_id != append.claim.attempt_id
            or authority.fence_identity != append.claim.fence_identity
            or authority.state != append.claim.state
            or authority.lifecycle_version != append.claim.lifecycle_version
            or authority.checkpoint_sha256 != append.claim.checkpoint_sha256
        ):
            raise V4HeadStale("v4 successor predecessor head is stale")
        if (
            authority.claim_owner_identity != append.claim.claim_owner_identity
            or authority.claim_generation != append.claim.claim_generation
        ):
            raise V4ClaimLost("v4 successor predecessor claim was lost")

    @staticmethod
    def _require_new_evidence_exact(
        previous: RemoteParseCheckpointV4,
        append: V4SuccessorAppend,
    ) -> None:
        introduced = {
            name[:-7]
            for name in _EVIDENCE_FIELDS
            if getattr(previous, name) is None
            and getattr(append.successor, name) is not None
        }
        provided = {item.kind for item in append.new_evidence}
        if introduced != provided:
            raise ValueError(
                "v4 successor evidence delta is not exact"
            )
        for item in append.new_evidence:
            if getattr(append.successor, f"{item.kind}_sha256") != item.sha256:
                raise ValueError(
                    "v4 successor evidence hash drifted"
                )

    def _require_append_companions(
        self,
        authority: RemoteParseV4Authority,
        append: V4SuccessorAppend,
    ) -> None:
        previous = authority.checkpoint
        accepted_introduced = (
            previous.accepted_submission_sha256 is None
            and append.successor.accepted_submission_sha256 is not None
        )
        if accepted_introduced != (append.sealed_secret is not None):
            raise ValueError(
                "v4 accepted successor secret presence is not exact"
            )
        winner_introduced = (
            previous.publication_winner_sha256 is None
            and append.successor.publication_winner_sha256 is not None
        )
        if winner_introduced != (append.publication_winner is not None):
            raise ValueError(
                "v4 publication winner presence is not exact"
            )
        supersession_introduced = (
            previous.supersession_receipt_sha256 is None
            and append.successor.supersession_receipt_sha256 is not None
        )
        if supersession_introduced != (append.staged_superseder is not None):
            raise ValueError(
                "v4 resourceful superseder presence is not exact"
            )
        if append.staged_superseder is not None:
            if authority.source_supersession_link is not None:
                raise ValueError(
                    "v4 source already has a supersession link"
                )
            staged = append.staged_superseder
            target = staged.checkpoint
            if staged.snapshot_receipt is None:
                raise V4GenerationConflict(
                    "v4 staged superseder lacks a durable snapshot seed"
                )
            try:
                validate_superseding_checkpoint_seed_evidence_v4(
                    checkpoint=target,
                    reservation=staged.reservation,
                    preparation_intent=staged.preparation_intent,
                    snapshot_receipt=staged.snapshot_receipt,
                )
            except ValueError as exc:
                raise V4GenerationConflict(
                    "v4 staged superseder seed is invalid"
                ) from exc
            max_generation = self._session.execute(
                sa.select(sa.func.max(models.RemoteParseAttempt.attempt_generation))
                .where(
                    models.RemoteParseAttempt.document_id == authority.document_id
                )
            ).scalar_one()
            if (
                max_generation is None
                or target.attempt_generation != max_generation + 1
                or target.document_id != authority.document_id
            ):
                raise V4GenerationConflict(
                    "v4 staged superseder is not the next document generation"
                )
        if append.successor.state == "superseded":
            if authority.source_supersession_link is None:
                raise V4HeadStale(
                    "v4 final supersession lacks staged authority"
                )
        elif authority.source_supersession_link is not None and (
            append.successor.state not in {"cleanup_pending", "ack_pending"}
        ):
            raise V4HeadStale(
                "v4 linked supersession took an invalid successor"
            )

    def _cas_successor_head(self, append: V4SuccessorAppend) -> None:
        table = models.RemoteParseAttempt.__table__
        clock = self._database_clock()
        final = append.successor.state in _FINAL_STATES
        result = self._session.execute(
            sa.update(models.RemoteParseAttempt)
            .where(
                table.c.attempt_id == append.claim.attempt_id,
                table.c.fence_identity == append.claim.fence_identity,
                table.c.checkpoint_contract_version == 4,
                table.c.is_current.is_(True),
                table.c.state == append.claim.state,
                table.c.row_version == append.claim.lifecycle_version,
                table.c.current_checkpoint_sha256 == append.claim.checkpoint_sha256,
                table.c.claim_owner_identity == append.claim.claim_owner_identity,
                table.c.claim_generation == append.claim.claim_generation,
                table.c.claim_lease_until > clock.c.database_observed_at,
            )
            .values(
                state=append.successor.state,
                is_current=not final,
                row_version=append.successor.lifecycle_version,
                current_checkpoint_sha256=append.successor.sha256,
                claim_owner_identity=(
                    None if final else append.claim.claim_owner_identity
                ),
                claim_lease_until=(
                    None if final else table.c.claim_lease_until
                ),
                updated_at=clock.c.database_observed_at,
            )
            .returning(table.c.attempt_id)
        ).one_or_none()
        if result is None:
            raise V4ClaimLost("v4 successor lost exact live claim CAS")

    def _insert_resourceful_superseder(
        self,
        append: V4SuccessorAppend,
        *,
        authority: RemoteParseV4Authority,
    ) -> None:
        creation = append.staged_superseder
        assert creation is not None
        supersession = next(
            (
                item
                for item in append.new_evidence
                if item.kind == "supersession_receipt"
            ),
            None,
        )
        if supersession is None or type(supersession.value) is not SupersessionReceiptV4:
            raise RemoteParseV4AuthorityViolation(
                "v4 staged superseder lacks exact source receipt"
            )
        receipt = cast(SupersessionReceiptV4, supersession.value)
        if (
            receipt.source_checkpoint_sha256 != authority.checkpoint_sha256
            or receipt.source_lifecycle_version != authority.lifecycle_version
            or receipt.superseding_checkpoint_sha256 != creation.checkpoint.sha256
            or receipt.superseding_attempt_id != creation.checkpoint.attempt_id
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 staged superseder receipt drifted"
            )
        self._insert_prepared_creation(creation, is_current=False)
        self._session.execute(
            sa.insert(models.RemoteParseV4SupersessionLink).values(
                source_attempt_id=authority.attempt_id,
                source_fence_identity=authority.fence_identity,
                source_evidence_kind="supersession_receipt",
                source_supersession_receipt_sha256=supersession.sha256,
                superseding_attempt_id=creation.checkpoint.attempt_id,
                superseding_fence_identity=creation.checkpoint.fence_identity,
                superseding_lifecycle_version=0,
                superseding_checkpoint_sha256=creation.checkpoint.sha256,
            )
        )

    def _activate_staged_superseder(
        self,
        authority: RemoteParseV4Authority,
        append: V4SuccessorAppend,
    ) -> None:
        link = authority.source_supersession_link
        if link is None:
            raise RemoteParseV4AuthorityViolation(
                "v4 final supersession lacks immutable link"
        )
        table = models.RemoteParseAttempt.__table__
        activated = self._session.execute(
            sa.update(models.RemoteParseAttempt)
            .where(
                table.c.attempt_id == link.superseding_attempt_id,
                table.c.fence_identity == link.superseding_fence_identity,
                table.c.checkpoint_contract_version == 4,
                table.c.document_id == authority.document_id,
                table.c.state == "prepared",
                table.c.is_current.is_(False),
                table.c.row_version == 0,
                table.c.current_checkpoint_sha256
                == link.superseding_checkpoint_sha256,
                table.c.claim_generation == 0,
                table.c.claim_owner_identity.is_(None),
                table.c.claim_lease_until.is_(None),
            )
            .values(
                is_current=True,
                updated_at=sa.func.clock_timestamp(),
            )
            .returning(table.c.attempt_id)
        ).one_or_none()
        if activated is None:
            raise V4HeadStale(
                "v4 staged superseder activation lost exact H0 CAS"
            )

    def _purge_final_secrets(
        self,
        *,
        append: V4SuccessorAppend,
        expected_revision_max: int,
    ) -> None:
        deleted = self._session.execute(
            sa.text(
                "SELECT disclosure_ops.purge_remote_parse_v4_secrets_final("
                ":attempt_id, :fence_identity, :lifecycle_version, "
                ":checkpoint_sha256, :expected_revision_max)"
            ),
            {
                "attempt_id": append.claim.attempt_id,
                "fence_identity": append.claim.fence_identity,
                "lifecycle_version": append.successor.lifecycle_version,
                "checkpoint_sha256": append.successor.sha256,
                "expected_revision_max": expected_revision_max,
            },
        ).scalar_one()
        if deleted != expected_revision_max:
            raise RemoteParseV4AuthorityViolation(
                "v4 final secret purge count drifted"
            )

    @staticmethod
    def _insert_winner_values(winner: AtomicPublicationWinnerV4) -> dict[str, object]:
        return {
            "attempt_id": winner.attempt_id,
            "fence_identity": winner.fence_identity,
            "document_id": winner.document_id,
            "processing_run_id": winner.processing_run_id,
            "publish_attempt_generation": winner.publish_attempt_generation,
            "local_checkpoint_sha256": winner.local_checkpoint_sha256,
            "lifecycle_version_before": winner.lifecycle_version_before,
            "lifecycle_version_after": winner.lifecycle_version_after,
            "request_sha256": winner.request_sha256,
            "upstream_evidence_sha256": winner.upstream_evidence_sha256,
            "final_units_sha256": winner.final_units_sha256,
            "lineage_sha256": winner.lineage_sha256,
            "processing_run_row_sha256": winner.processing_run_row_sha256,
            "previous_active_run_id": winner.previous_active_run_id,
            "inserted_count": winner.inserted_count,
            "updated_count": winner.updated_count,
            "deleted_count": winner.deleted_count,
            "publish_precommit_at": winner.publish_precommit_at,
            "winner_sha256": winner.sha256,
            "winner_bytes": winner.canonical_bytes,
            "winner_byte_count": len(winner.canonical_bytes),
        }

    def _insert_winner(self, winner: AtomicPublicationWinnerV4) -> None:
        self._session.execute(
            sa.insert(models.AtomicPublicationWinnerV4).values(
                **self._insert_winner_values(winner)
            )
        )

    def _insert_secret(
        self,
        secret: SealedProviderSecretV4,
        *,
        accepted_submission_sha256: str,
    ) -> None:
        binding = secret.binding
        self._session.execute(
            sa.insert(models.RemoteParseV4Secret).values(
                attempt_id=binding.attempt_id,
                fence_identity=binding.fence_identity,
                accepted_submission_sha256=accepted_submission_sha256,
                secret_kind=binding.secret_kind,
                provider_secret_version=binding.provider_secret_version,
                token_sha256=binding.token_sha256,
                token_byte_count=binding.token_byte_count,
                encryption_revision=secret.encryption_revision,
                kek_id=secret.kek_id,
                wrap_nonce=secret.wrap_nonce,
                wrapped_dek=secret.wrapped_dek,
                data_nonce=secret.data_nonce,
                token_ciphertext=secret.token_ciphertext,
            )
        )

    def _force_v4_constraints(self) -> None:
        names = ", ".join(
            f"disclosure_ops.{name}" for name in _V4_DEFERRED_CONSTRAINTS
        )
        self._session.execute(sa.text(f"SET CONSTRAINTS {names} IMMEDIATE"))
        self._session.execute(sa.text(f"SET CONSTRAINTS {names} DEFERRED"))

    def _require_committed_successor_exact(
        self,
        authority: RemoteParseV4Authority,
        append: V4SuccessorAppend,
    ) -> None:
        committed = authority.checkpoint_history[append.successor.lifecycle_version]
        if committed != append.successor or not _evidence_subset_exact(
            authority.evidence,
            append.new_evidence,
        ):
            raise V4DifferentSuccessorCommitted(
                "v4 committed successor bytes differ from proposal"
            )
        if append.sealed_secret is not None and (
            not authority.secret_history
            or authority.secret_history[0] != append.sealed_secret
        ):
            raise V4DifferentSuccessorCommitted(
                "v4 committed initial secret differs from proposal"
            )
        if (
            append.publication_winner is not None
            and authority.publication_winner != append.publication_winner
        ):
            raise V4DifferentSuccessorCommitted(
                "v4 committed publication winner differs from proposal"
            )
        if append.staged_superseder is not None:
            link = authority.source_supersession_link
            if link is None:
                raise V4DifferentSuccessorCommitted(
                    "v4 committed successor lacks staged superseder"
                )
            target = self._strict_authority(
                self._select_head(link.superseding_attempt_id, lock="share")
            )
            try:
                self._require_prepared_replay(target, append.staged_superseder)
            except V4GenerationConflict as exc:
                raise V4DifferentSuccessorCommitted(
                    "v4 committed staged superseder differs from proposal"
                ) from exc

    @staticmethod
    def _claim_is_live(
        authority: RemoteParseV4Authority,
        claim: V4ClaimWitness,
    ) -> bool:
        return (
            authority.is_current
            and authority.claim_owner_identity == claim.claim_owner_identity
            and authority.claim_generation == claim.claim_generation
            and authority.database_lease is not None
            and authority.database_lease.remaining_microseconds > 0
        )

    def _lock_authority_heads(self, attempt_id: str) -> Mapping[Any, Any]:
        table = models.RemoteParseAttempt.__table__
        seed = self._session.execute(
            sa.select(table.c.attempt_id).where(table.c.attempt_id == attempt_id)
        ).scalar_one_or_none()
        if seed is None:
            raise V4HeadNotFound("v4 authority head is absent")
        link = models.RemoteParseV4SupersessionLink.__table__
        linked_ids = {
            attempt_id,
            *self._session.execute(
                sa.select(link.c.source_attempt_id).where(
                    link.c.superseding_attempt_id == attempt_id
                )
            ).scalars(),
            *self._session.execute(
                sa.select(link.c.superseding_attempt_id).where(
                    link.c.source_attempt_id == attempt_id
                )
            ).scalars(),
        }
        rows = self._session.execute(
            sa.select(table)
            .where(table.c.attempt_id.in_(linked_ids))
            .order_by(table.c.attempt_generation, table.c.attempt_id)
            .with_for_update(read=True)
        ).mappings().all()
        by_id = {row["attempt_id"]: row for row in rows}
        head = by_id.get(attempt_id)
        if head is None:
            raise V4HeadNotFound("v4 authority head disappeared before lock")
        return head

    def _lock_creation_chain(
        self,
        document_id: str,
    ) -> tuple[Mapping[Any, Any], ...]:
        _identity(document_id, "document")
        acquire_document_xact_lock(self._session, document_id)
        table = models.RemoteParseAttempt.__table__
        return tuple(
            self._session.execute(
                sa.select(table)
                .where(table.c.document_id == document_id)
                .order_by(table.c.attempt_generation, table.c.attempt_id)
                .with_for_update()
            ).mappings().all()
        )

    @staticmethod
    def _creation_row(
        rows: tuple[Mapping[Any, Any], ...],
        attempt_id: str,
    ) -> Mapping[Any, Any] | None:
        return next(
            (row for row in rows if row["attempt_id"] == attempt_id),
            None,
        )

    @staticmethod
    def _guard_creation_chain(
        *,
        rows: tuple[Mapping[Any, Any], ...],
        generations: tuple[int, ...],
        allow_existing_current: bool,
        allow_existing_staged: bool = False,
    ) -> None:
        if (
            not generations
            or any(
                type(value) is not int or not 1 <= value <= _MAX_INT
                for value in generations
            )
            or generations
            != tuple(range(generations[0], generations[0] + len(generations)))
        ):
            raise ValueError("v4 creation generations are invalid")
        if not allow_existing_current and any(row["is_current"] for row in rows):
            raise V4DocumentCurrentConflict(
                "document already has a current remote-parse attempt"
            )
        if not allow_existing_staged and any(
            row["checkpoint_contract_version"] == 4
            and row["state"] == "prepared"
            and not row["is_current"]
            for row in rows
        ):
            raise V4DocumentCurrentConflict(
                "document already has a staged remote-parse attempt"
            )
        expected_first = (
            1
            if not rows
            else max(cast(int, row["attempt_generation"]) for row in rows) + 1
        )
        if generations[0] != expected_first:
            raise V4GenerationConflict(
                "v4 creation generation is not the next document generation"
            )

    @staticmethod
    def _require_prepared_replay(
        authority: RemoteParseV4Authority,
        creation: V4PreparedCreation,
    ) -> None:
        expected = [encode_remote_parse_evidence_v4(creation.preparation_intent)]
        if creation.snapshot_receipt is not None:
            expected.append(encode_remote_parse_evidence_v4(creation.snapshot_receipt))
        if (
            authority.checkpoint_history[0] != creation.checkpoint
            or authority.reservation != creation.reservation
            or authority.parser_target_sha256 != creation.parser_target_sha256
            or authority.client_submit_key != creation.client_submit_key
            or not _evidence_subset_exact(authority.evidence, tuple(expected))
        ):
            raise V4GenerationConflict(
                "v4 prepared response-loss replay differs from H0 authority"
            )

    @staticmethod
    def _require_failure_replay(
        authority: RemoteParseV4Authority,
        creation: V4ResourceFreeFailureCreation,
    ) -> None:
        expected = encode_remote_parse_evidence_v4(creation.failure_receipt)
        if (
            authority.checkpoint_history != (creation.checkpoint,)
            or authority.reservation is not None
            or authority.parser_target_sha256 != creation.parser_target_sha256
            or authority.client_submit_key != creation.client_submit_key
            or authority.evidence != (expected,)
        ):
            raise V4GenerationConflict(
                "v4 failure response-loss replay differs from authority"
            )

    @staticmethod
    def _require_resource_free_supersession_replay(
        source: RemoteParseV4Authority,
        target: RemoteParseV4Authority,
        creation: V4ResourceFreeSupersessionCreation,
    ) -> None:
        expected_link = V4SupersessionLinkAuthority(
            source_attempt_id=creation.source_checkpoint.attempt_id,
            source_fence_identity=creation.source_checkpoint.fence_identity,
            source_supersession_receipt_sha256=creation.supersession_receipt.sha256,
            superseding_attempt_id=creation.superseding.checkpoint.attempt_id,
            superseding_fence_identity=creation.superseding.checkpoint.fence_identity,
            superseding_checkpoint_sha256=creation.superseding.checkpoint.sha256,
        )
        RemoteParseV4Repository._require_failure_free_supersession_source(
            source,
            creation,
        )
        RemoteParseV4Repository._require_prepared_replay(
            target,
            creation.superseding,
        )
        if (
            source.source_supersession_link != expected_link
            or target.staged_by_link != expected_link
        ):
            raise V4GenerationConflict(
                "v4 supersession replay differs from immutable link"
            )

    @staticmethod
    def _require_failure_free_supersession_source(
        authority: RemoteParseV4Authority,
        creation: V4ResourceFreeSupersessionCreation,
    ) -> None:
        expected = encode_remote_parse_evidence_v4(creation.supersession_receipt)
        if (
            authority.checkpoint_history != (creation.source_checkpoint,)
            or authority.reservation is not None
            or authority.parser_target_sha256
            != creation.source_parser_target_sha256
            or authority.client_submit_key != creation.source_client_submit_key
            or authority.evidence != (expected,)
        ):
            raise V4GenerationConflict(
                "v4 supersession replay differs from source authority"
            )

    def _insert_prepared_creation(
        self,
        creation: V4PreparedCreation,
        *,
        is_current: bool,
    ) -> None:
        checkpoint = creation.checkpoint
        self._insert_head(
            checkpoint=checkpoint,
            parser_target_sha256=creation.parser_target_sha256,
            client_submit_key=creation.client_submit_key,
            is_current=is_current,
        )
        self._insert_evidence(
            encode_remote_parse_evidence_v4(creation.preparation_intent)
        )
        if creation.snapshot_receipt is not None:
            self._insert_evidence(
                encode_remote_parse_evidence_v4(creation.snapshot_receipt)
            )
        self._insert_checkpoint(checkpoint, reservation=creation.reservation)

    def _insert_head(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        parser_target_sha256: str,
        client_submit_key: str,
        is_current: bool,
    ) -> None:
        self._session.execute(
            sa.insert(models.RemoteParseAttempt).values(
                attempt_id=checkpoint.attempt_id,
                processing_run_id=checkpoint.processing_run_id,
                document_id=checkpoint.document_id,
                attempt_generation=checkpoint.attempt_generation,
                fence_identity=checkpoint.fence_identity,
                source_pdf_sha256=checkpoint.source_pdf_sha256,
                parser_target_sha256=parser_target_sha256,
                request_sha256=checkpoint.request_sha256,
                runtime_epoch_sha256=checkpoint.runtime_epoch_sha256,
                client_submit_key=client_submit_key,
                checkpoint_contract_version=4,
                state=checkpoint.state,
                is_current=is_current,
                row_version=checkpoint.lifecycle_version,
                current_checkpoint_sha256=checkpoint.sha256,
                claim_generation=0,
                claim_owner_identity=None,
                claim_lease_until=None,
            )
        )

    def _insert_evidence(
        self,
        evidence: EncodedRemoteParseEvidenceV4,
    ) -> None:
        self._session.execute(
            sa.insert(models.RemoteParseV4Evidence).values(
                attempt_id=evidence.value.attempt_id,
                fence_identity=evidence.value.fence_identity,
                evidence_kind=evidence.kind,
                evidence_sha256=evidence.sha256,
                evidence_bytes=evidence.exact_bytes,
                evidence_byte_count=evidence.byte_count,
            )
        )

    def _insert_checkpoint(
        self,
        checkpoint: RemoteParseCheckpointV4,
        *,
        reservation: ResourceReservationV4 | None,
    ) -> None:
        self._session.execute(
            sa.insert(models.RemoteParseV4Checkpoint).values(
                attempt_id=checkpoint.attempt_id,
                fence_identity=checkpoint.fence_identity,
                state=checkpoint.state,
                lifecycle_version=checkpoint.lifecycle_version,
                previous_checkpoint_sha256=(
                    checkpoint.previous_checkpoint_sha256
                ),
                checkpoint_sha256=checkpoint.sha256,
                checkpoint_bytes=checkpoint.canonical_bytes,
                checkpoint_byte_count=len(checkpoint.canonical_bytes),
                resource_reservation_sha256=(
                    None if reservation is None else reservation.sha256
                ),
                resource_reservation_bytes=(
                    None if reservation is None else reservation.canonical_bytes
                ),
                resource_reservation_byte_count=(
                    None
                    if reservation is None
                    else len(reservation.canonical_bytes)
                ),
                source_byte_count=checkpoint.source_byte_count,
                source_page_count=checkpoint.source_page_count,
                **{
                    f"held_{name}": getattr(
                        checkpoint.held_resource_credit,
                        name,
                    )
                    for name in _HELD_CREDIT_FIELDS
                },
                **{
                    name: getattr(checkpoint, name)
                    for name in _EVIDENCE_FIELDS
                },
                publication_winner_sha256=(
                    checkpoint.publication_winner_sha256
                ),
            )
        )

    @staticmethod
    def _raise_creation_integrity(exc: IntegrityError) -> None:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint in {
            "uq_remote_parse_attempt_current_document",
            "uq_remote_parse_v4_staged_document",
        }:
            raise V4DocumentCurrentConflict(
                "document remote-parse currentness changed during creation"
            ) from exc
        if constraint in {
            "remote_parse_attempt_pkey",
            "remote_parse_attempt_client_submit_key_key",
            "uq_remote_parse_attempt_document_generation",
        }:
            raise V4GenerationConflict(
                "v4 attempt identity or generation already exists"
            ) from exc
        raise RemoteParseV4AuthorityViolation(
            "v4 creation was rejected by durable authority"
        ) from exc

    @staticmethod
    def _raise_authority_dbapi(exc: DBAPIError, message: str) -> NoReturn:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate is None:
            sqlstate = getattr(exc.orig, "pgcode", None)
        if isinstance(exc, IntegrityError) or sqlstate == "P0001":
            raise RemoteParseV4AuthorityViolation(message) from exc
        raise exc

    def _select_head(
        self,
        attempt_id: str,
        *,
        lock: str,
    ) -> Mapping[Any, Any]:
        table = models.RemoteParseAttempt.__table__
        statement = sa.select(table).where(table.c.attempt_id == attempt_id)
        statement = statement.with_for_update(read=lock == "share")
        row = self._session.execute(statement).mappings().one_or_none()
        if row is None:
            raise V4HeadNotFound("v4 authority head is absent")
        return row

    def _strict_authority(
        self,
        head: Mapping[Any, Any],
        *,
        observe_database_clock: bool = True,
    ) -> RemoteParseV4Authority:
        try:
            return self._strict_authority_unchecked(
                head,
                observe_database_clock=observe_database_clock,
            )
        except RemoteParseV4AuthorityViolation:
            raise
        except (LookupError, TypeError, ValueError) as exc:
            raise RemoteParseV4AuthorityViolation(
                "persisted v4 authority cannot be reconstructed"
            ) from exc

    def _strict_authority_unchecked(
        self,
        head: Mapping[Any, Any],
        *,
        observe_database_clock: bool = True,
    ) -> RemoteParseV4Authority:
        if head["checkpoint_contract_version"] != 4:
            raise RemoteParseV4AuthorityViolation(
                "remote-parse head is not contract version 4"
            )
        attempt_id = head["attempt_id"]
        checkpoint_table = models.RemoteParseV4Checkpoint.__table__
        checkpoint_rows = self._session.execute(
            sa.select(checkpoint_table)
            .where(checkpoint_table.c.attempt_id == attempt_id)
            .order_by(checkpoint_table.c.lifecycle_version)
        ).mappings().all()
        if not checkpoint_rows:
            raise RemoteParseV4AuthorityViolation(
                "v4 head has no checkpoint history"
            )
        checkpoints: list[RemoteParseCheckpointV4] = []
        reservation: ResourceReservationV4 | None = None
        for row in checkpoint_rows:
            checkpoint, row_reservation = self._decode_checkpoint_row(row)
            checkpoints.append(checkpoint)
            if row_reservation is not None:
                if reservation is not None:
                    raise RemoteParseV4AuthorityViolation(
                        "v4 authority repeats its H0 reservation"
                    )
                reservation = row_reservation
        history = tuple(checkpoints)
        current = history[-1]
        self._validate_head_projection(head, current)

        evidence_table = models.RemoteParseV4Evidence.__table__
        evidence_rows = self._session.execute(
            sa.select(evidence_table)
            .where(evidence_table.c.attempt_id == attempt_id)
            .order_by(evidence_table.c.evidence_kind)
        ).mappings().all()
        evidence = tuple(self._decode_evidence_row(row) for row in evidence_rows)
        evidence_by_kind: dict[str, EncodedRemoteParseEvidenceV4] = {
            item.kind: item for item in evidence
        }
        if len(evidence_by_kind) != len(evidence):
            raise RemoteParseV4AuthorityViolation(
                "v4 authority repeats an evidence kind"
            )
        preparation = _evidence_value(
            evidence_by_kind,
            "preparation_intent",
            PreparationIntentV4,
        )
        submission = _evidence_value(
            evidence_by_kind,
            "submission_intent",
            SubmissionIntentV4,
        )
        if preparation is not None and (
            preparation.parser_target_sha256 != head["parser_target_sha256"]
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 parser target drifted from preparation evidence"
            )
        if submission is not None and (
            submission.client_submit_key != head["client_submit_key"]
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 client submit key drifted from submission evidence"
            )

        winner = self._load_winner(
            attempt_id=attempt_id,
            expected_sha256=current.publication_winner_sha256,
        )
        if winner is not None:
            self._validate_winner_authority(
                head=head,
                history=history,
                winner=winner,
            )
        secrets = self._load_secret_history(
            head=head,
            accepted=evidence_by_kind.get("accepted_submission"),
        )
        accepted_evidence = evidence_by_kind.get("accepted_submission")
        if current.state in _FINAL_STATES and secrets:
            raise RemoteParseV4AuthorityViolation(
                "v4 final authority retained provider secrets"
            )
        if (
            accepted_evidence is not None
            and current.state not in _FINAL_STATES
            and not secrets
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 accepted authority lacks provider secret history"
            )
        source_link, staged_by_link = self._load_links(attempt_id)
        if staged_by_link is not None:
            self._validate_staged_by_link(
                head=head,
                checkpoint=history[0],
                link=staged_by_link,
            )

        cleanup_plan = _evidence_value(
            evidence_by_kind,
            "cleanup_plan",
            LocalCleanupPlanV4,
        )
        cleanup_receipt = _evidence_value(
            evidence_by_kind,
            "cleanup_receipt",
            LocalCleanupReceiptV4,
        )
        ack = _evidence_value(
            evidence_by_kind,
            "ack_receipt",
            ProviderAckReceiptV4,
        )
        cleanup_source = (
            _checkpoint_by_identity(
                history,
                cleanup_plan.source_lifecycle_version,
                cleanup_plan.source_checkpoint_sha256,
            )
            if cleanup_plan is not None
            else None
        )
        cleanup_pending = (
            _checkpoint_by_identity(
                history,
                cleanup_receipt.cleanup_pending_lifecycle_version,
                cleanup_receipt.cleanup_pending_checkpoint_sha256,
            )
            if cleanup_receipt is not None
            else None
        )
        ack_pending = (
            _checkpoint_by_identity(
                history,
                ack.ack_pending_lifecycle_version,
                ack.ack_pending_checkpoint_sha256,
            )
            if ack is not None
            else None
        )
        resourceful_history = None
        if reservation is not None:
            target = cleanup_source or current
            resourceful_history = history[: target.lifecycle_version + 1]

        superseding_checkpoint = None
        superseding_reservation = None
        superseding_preparation = None
        superseding_snapshot = None
        if source_link is not None:
            supersession = _evidence_value(
                evidence_by_kind,
                "supersession_receipt",
                SupersessionReceiptV4,
            )
            if supersession is None:
                raise RemoteParseV4AuthorityViolation(
                    "v4 supersession link lacks its receipt"
                )
            (
                superseding_checkpoint,
                superseding_reservation,
                superseding_preparation,
                superseding_snapshot,
            ) = self._load_superseding_seed(source_link, supersession)

        try:
            validate_durable_remote_parse_evidence_bundle_v4(
                checkpoint=current,
                evidence=evidence,
                reservation=reservation,
                cleanup_source_checkpoint=cleanup_source,
                resourceful_checkpoint_history=resourceful_history,
                cleanup_pending_checkpoint=cleanup_pending,
                ack_pending_checkpoint=ack_pending,
                superseding_checkpoint=superseding_checkpoint,
                superseding_reservation=superseding_reservation,
                superseding_preparation_intent=superseding_preparation,
                superseding_snapshot_receipt=superseding_snapshot,
            )
        except ValueError as exc:
            raise RemoteParseV4AuthorityViolation(
                "v4 durable evidence replay failed"
            ) from exc

        database_lease = None
        if head["claim_lease_until"] is not None and observe_database_clock:
            observed_at = self._observe_database_time()
            lease_until = _utc(head["claim_lease_until"])
            delta = lease_until - observed_at
            database_lease = DatabaseLeaseSnapshot(
                database_observed_at_utc=observed_at,
                lease_until_utc=lease_until,
                remaining_microseconds=(
                    delta.days * 86_400_000_000
                    + delta.seconds * 1_000_000
                    + delta.microseconds
                ),
            )
        return RemoteParseV4Authority(
            attempt_id=attempt_id,
            processing_run_id=head["processing_run_id"],
            document_id=head["document_id"],
            attempt_generation=head["attempt_generation"],
            fence_identity=head["fence_identity"],
            source_pdf_sha256=head["source_pdf_sha256"],
            parser_target_sha256=head["parser_target_sha256"],
            request_sha256=head["request_sha256"],
            runtime_epoch_sha256=head["runtime_epoch_sha256"],
            client_submit_key=head["client_submit_key"],
            state=head["state"],
            is_current=head["is_current"],
            lifecycle_version=head["row_version"],
            checkpoint_sha256=head["current_checkpoint_sha256"],
            claim_generation=head["claim_generation"],
            claim_owner_identity=head["claim_owner_identity"],
            claim_lease_until=(
                None
                if head["claim_lease_until"] is None
                else _utc(head["claim_lease_until"])
            ),
            checkpoint_history=history,
            reservation=reservation,
            evidence=evidence,
            publication_winner=winner,
            secret_history=secrets,
            source_supersession_link=source_link,
            staged_by_link=staged_by_link,
            database_lease=database_lease,
        )

    @staticmethod
    def _decode_checkpoint_row(
        row: Mapping[Any, Any],
    ) -> tuple[RemoteParseCheckpointV4, ResourceReservationV4 | None]:
        exact_bytes = _exact_bytes(row["checkpoint_bytes"], "checkpoint")
        checkpoint = decode_remote_parse_checkpoint_v4(exact_bytes)
        expected: dict[str, object] = {
            "attempt_id": checkpoint.attempt_id,
            "fence_identity": checkpoint.fence_identity,
            "state": checkpoint.state,
            "lifecycle_version": checkpoint.lifecycle_version,
            "previous_checkpoint_sha256": checkpoint.previous_checkpoint_sha256,
            "checkpoint_sha256": checkpoint.sha256,
            "checkpoint_byte_count": len(exact_bytes),
            "source_byte_count": checkpoint.source_byte_count,
            "source_page_count": checkpoint.source_page_count,
            "publication_winner_sha256": checkpoint.publication_winner_sha256,
            **{
                name: getattr(checkpoint, name)
                for name in _EVIDENCE_FIELDS
            },
            **{
                f"held_{name}": getattr(checkpoint.held_resource_credit, name)
                for name in _HELD_CREDIT_FIELDS
            },
        }
        _require_projection(row, expected, "v4 checkpoint")
        reservation_bytes = row["resource_reservation_bytes"]
        if reservation_bytes is None:
            if (
                row["resource_reservation_sha256"] is not None
                or row["resource_reservation_byte_count"] is not None
            ):
                raise ValueError("v4 reservation columns are partial")
            return checkpoint, None
        reservation_exact = _exact_bytes(reservation_bytes, "reservation")
        reservation = decode_resource_reservation_v4(reservation_exact)
        if (
            row["resource_reservation_sha256"] != reservation.sha256
            or row["resource_reservation_byte_count"] != len(reservation_exact)
        ):
            raise ValueError("v4 reservation row does not close")
        return checkpoint, reservation

    @staticmethod
    def _decode_evidence_row(
        row: Mapping[Any, Any],
    ) -> EncodedRemoteParseEvidenceV4:
        exact = _exact_bytes(row["evidence_bytes"], "evidence")
        encoded = decode_remote_parse_evidence_v4(
            cast(Any, row["evidence_kind"]),
            exact,
        )
        if (
            encoded.sha256 != row["evidence_sha256"]
            or encoded.byte_count != row["evidence_byte_count"]
            or encoded.value.attempt_id != row["attempt_id"]
            or encoded.value.fence_identity != row["fence_identity"]
        ):
            raise ValueError("v4 evidence row does not close")
        return encoded

    def _load_winner(
        self,
        *,
        attempt_id: str,
        expected_sha256: str | None,
    ) -> AtomicPublicationWinnerV4 | None:
        table = models.AtomicPublicationWinnerV4.__table__
        rows = self._session.execute(
            sa.select(table).where(table.c.attempt_id == attempt_id)
        ).mappings().all()
        if expected_sha256 is None:
            if rows:
                raise RemoteParseV4AuthorityViolation(
                    "v4 authority has an unreferenced publication winner"
                )
            return None
        if len(rows) != 1:
            raise RemoteParseV4AuthorityViolation(
                "v4 checkpoint lacks its exact publication winner"
            )
        row = rows[0]
        exact = _exact_bytes(row["winner_bytes"], "publication winner")
        winner = decode_atomic_publication_winner_v4(exact)
        expected = {
            "attempt_id": winner.attempt_id,
            "fence_identity": winner.fence_identity,
            "document_id": winner.document_id,
            "processing_run_id": winner.processing_run_id,
            "publish_attempt_generation": winner.publish_attempt_generation,
            "local_checkpoint_sha256": winner.local_checkpoint_sha256,
            "lifecycle_version_before": winner.lifecycle_version_before,
            "lifecycle_version_after": winner.lifecycle_version_after,
            "request_sha256": winner.request_sha256,
            "upstream_evidence_sha256": winner.upstream_evidence_sha256,
            "final_units_sha256": winner.final_units_sha256,
            "lineage_sha256": winner.lineage_sha256,
            "processing_run_row_sha256": winner.processing_run_row_sha256,
            "previous_active_run_id": winner.previous_active_run_id,
            "inserted_count": winner.inserted_count,
            "updated_count": winner.updated_count,
            "deleted_count": winner.deleted_count,
            "publish_precommit_at": winner.publish_precommit_at,
            "winner_sha256": winner.sha256,
            "winner_byte_count": len(exact),
        }
        _require_projection(row, expected, "v4 publication winner")
        if winner.sha256 != expected_sha256:
            raise RemoteParseV4AuthorityViolation(
                "v4 publication winner hash drifted from checkpoint"
            )
        return winner

    def _load_secret_history(
        self,
        *,
        head: Mapping[Any, Any],
        accepted: EncodedRemoteParseEvidenceV4 | None,
    ) -> tuple[SealedProviderSecretV4, ...]:
        table = models.RemoteParseV4Secret.__table__
        rows = self._session.execute(
            sa.select(table)
            .where(table.c.attempt_id == head["attempt_id"])
            .order_by(table.c.encryption_revision)
        ).mappings().all()
        if not rows:
            return ()
        if accepted is None or type(accepted.value) is not AcceptedSubmissionReceiptV4:
            raise RemoteParseV4AuthorityViolation(
                "v4 secret history lacks accepted submission evidence"
            )
        accepted_value = cast(AcceptedSubmissionReceiptV4, accepted.value)
        history: list[SealedProviderSecretV4] = []
        for row in rows:
            if (
                row["fence_identity"] != head["fence_identity"]
                or row["accepted_submission_sha256"] != accepted.sha256
            ):
                raise RemoteParseV4AuthorityViolation(
                    "v4 secret row drifted from accepted evidence"
                )
            binding = ProviderSecretBindingV4(
                attempt_id=row["attempt_id"],
                fence_identity=row["fence_identity"],
                secret_kind=row["secret_kind"],
                provider_secret_version=row["provider_secret_version"],
                token_sha256=row["token_sha256"],
                token_byte_count=row["token_byte_count"],
            )
            if (
                binding.secret_kind != accepted_value.secret_kind
                or binding.provider_secret_version != accepted_value.secret_version
                or binding.token_sha256 != accepted_value.token_sha256
                or binding.token_byte_count != accepted_value.token_byte_count
            ):
                raise RemoteParseV4AuthorityViolation(
                    "v4 sealed secret binding drifted from accepted evidence"
                )
            history.append(
                SealedProviderSecretV4(
                    binding=binding,
                    encryption_revision=row["encryption_revision"],
                    kek_id=row["kek_id"],
                    wrap_nonce=_exact_bytes(row["wrap_nonce"], "wrap nonce"),
                    wrapped_dek=_exact_bytes(row["wrapped_dek"], "wrapped DEK"),
                    data_nonce=_exact_bytes(row["data_nonce"], "data nonce"),
                    token_ciphertext=_exact_bytes(
                        row["token_ciphertext"],
                        "token ciphertext",
                    ),
                )
            )
        result = tuple(history)
        try:
            validate_provider_secret_revision_history_v4(result)
        except ValueError as exc:
            raise RemoteParseV4AuthorityViolation(
                "v4 sealed secret history is invalid"
            ) from exc
        return result

    def _load_links(
        self,
        attempt_id: str,
    ) -> tuple[
        V4SupersessionLinkAuthority | None,
        V4SupersessionLinkAuthority | None,
    ]:
        table = models.RemoteParseV4SupersessionLink.__table__
        source_row = self._session.execute(
            sa.select(table).where(table.c.source_attempt_id == attempt_id)
        ).mappings().one_or_none()
        target_row = self._session.execute(
            sa.select(table).where(table.c.superseding_attempt_id == attempt_id)
        ).mappings().one_or_none()
        return (
            None if source_row is None else _link_authority(source_row),
            None if target_row is None else _link_authority(target_row),
        )

    def _validate_staged_by_link(
        self,
        *,
        head: Mapping[Any, Any],
        checkpoint: RemoteParseCheckpointV4,
        link: V4SupersessionLinkAuthority,
    ) -> None:
        if (
            checkpoint.lifecycle_version != 0
            or checkpoint.state != "prepared"
            or link.superseding_attempt_id != checkpoint.attempt_id
            or link.superseding_fence_identity != checkpoint.fence_identity
            or link.superseding_checkpoint_sha256 != checkpoint.sha256
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession target drifted from immutable link"
            )
        source_head = self._session.execute(
            sa.select(models.RemoteParseAttempt.__table__).where(
                models.RemoteParseAttempt.__table__.c.attempt_id
                == link.source_attempt_id
            )
        ).mappings().one_or_none()
        if source_head is None or (
            source_head["checkpoint_contract_version"] != 4
            or source_head["fence_identity"] != link.source_fence_identity
            or source_head["document_id"] != head["document_id"]
            or source_head["attempt_generation"] >= head["attempt_generation"]
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession source authority is absent or drifted"
            )
        evidence_row = self._session.execute(
            sa.select(models.RemoteParseV4Evidence.__table__).where(
                models.RemoteParseV4Evidence.__table__.c.attempt_id
                == link.source_attempt_id,
                models.RemoteParseV4Evidence.__table__.c.evidence_kind
                == "supersession_receipt",
                models.RemoteParseV4Evidence.__table__.c.evidence_sha256
                == link.source_supersession_receipt_sha256,
            )
        ).mappings().one_or_none()
        if evidence_row is None:
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession target lacks source receipt"
            )
        encoded = self._decode_evidence_row(evidence_row)
        if type(encoded.value) is not SupersessionReceiptV4:
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession source receipt has the wrong type"
            )
        receipt = cast(SupersessionReceiptV4, encoded.value)
        if (
            receipt.attempt_id != source_head["attempt_id"]
            or receipt.fence_identity != source_head["fence_identity"]
            or receipt.source_document_id != source_head["document_id"]
            or receipt.source_attempt_generation
            != source_head["attempt_generation"]
            or receipt.superseding_attempt_id != head["attempt_id"]
            or receipt.superseding_attempt_generation
            != head["attempt_generation"]
            or receipt.superseding_document_id != head["document_id"]
            or receipt.superseding_checkpoint_sha256 != checkpoint.sha256
            or receipt.sha256 != link.source_supersession_receipt_sha256
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession receipt drifted from linked heads"
            )
        self._validate_supersession_source_witness(
            source_head=source_head,
            receipt=receipt,
        )

    def _validate_supersession_source_witness(
        self,
        *,
        source_head: Mapping[Any, Any],
        receipt: SupersessionReceiptV4,
    ) -> None:
        checkpoint = None
        cleanup_pending = None
        cleanup_plan = None
        resource_free = (
            source_head["state"] == "superseded"
            and source_head["row_version"] == 0
        )
        if not resource_free and receipt.source_checkpoint_sha256 is not None:
            checkpoint_row = self._session.execute(
                sa.select(models.RemoteParseV4Checkpoint.__table__).where(
                    models.RemoteParseV4Checkpoint.__table__.c.attempt_id
                    == source_head["attempt_id"],
                    models.RemoteParseV4Checkpoint.__table__.c.lifecycle_version
                    == receipt.source_lifecycle_version,
                    models.RemoteParseV4Checkpoint.__table__.c.checkpoint_sha256
                    == receipt.source_checkpoint_sha256,
                )
            ).mappings().one_or_none()
            if checkpoint_row is not None:
                checkpoint, _reservation = self._decode_checkpoint_row(
                    checkpoint_row
                )
            cleanup_pending_row = self._session.execute(
                sa.select(models.RemoteParseV4Checkpoint.__table__).where(
                    models.RemoteParseV4Checkpoint.__table__.c.attempt_id
                    == source_head["attempt_id"],
                    models.RemoteParseV4Checkpoint.__table__.c.lifecycle_version
                    == receipt.source_lifecycle_version + 1,
                )
            ).mappings().one_or_none()
            if cleanup_pending_row is not None:
                cleanup_pending, _reservation = self._decode_checkpoint_row(
                    cleanup_pending_row
                )
                if cleanup_pending.cleanup_plan_sha256 is not None:
                    cleanup_plan_row = self._session.execute(
                        sa.select(
                            models.RemoteParseV4Evidence.__table__
                        ).where(
                            models.RemoteParseV4Evidence.__table__.c.attempt_id
                            == source_head["attempt_id"],
                            models.RemoteParseV4Evidence.__table__.c.evidence_kind
                            == "cleanup_plan",
                            models.RemoteParseV4Evidence.__table__.c.evidence_sha256
                            == cleanup_pending.cleanup_plan_sha256,
                        )
                    ).mappings().one_or_none()
                    if cleanup_plan_row is not None:
                        encoded_plan = self._decode_evidence_row(
                            cleanup_plan_row
                        )
                        if type(encoded_plan.value) is LocalCleanupPlanV4:
                            cleanup_plan = cast(
                                LocalCleanupPlanV4,
                                encoded_plan.value,
                            )
        self._validate_supersession_source_witness_projection(
            source_head=source_head,
            receipt=receipt,
            checkpoint=checkpoint,
            cleanup_pending=cleanup_pending,
            cleanup_plan=cleanup_plan,
        )

    @staticmethod
    def _validate_supersession_source_witness_projection(
        *,
        source_head: Mapping[Any, Any],
        receipt: SupersessionReceiptV4,
        checkpoint: RemoteParseCheckpointV4 | None,
        cleanup_pending: RemoteParseCheckpointV4 | None = None,
        cleanup_plan: LocalCleanupPlanV4 | None = None,
    ) -> None:
        if (
            source_head["state"] == "superseded"
            and source_head["row_version"] == 0
        ):
            if (
                receipt.source_state != "not_prepared"
                or receipt.source_lifecycle_version != 0
                or receipt.source_checkpoint_sha256 is not None
                or checkpoint is not None
                or cleanup_pending is not None
                or cleanup_plan is not None
            ):
                raise RemoteParseV4AuthorityViolation(
                    "v4 supersession source lifecycle witness drifted"
                )
            return
        if (
            checkpoint is None
            or receipt.source_checkpoint_sha256 is None
            or checkpoint.state != receipt.source_state
            or checkpoint.lifecycle_version != receipt.source_lifecycle_version
            or checkpoint.sha256 != receipt.source_checkpoint_sha256
            or receipt.source_lifecycle_version >= source_head["row_version"]
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession source lifecycle witness drifted"
            )
        try:
            RemoteParseV4Repository._validate_immutable_head_projection(
                source_head,
                checkpoint,
            )
            if cleanup_pending is not None:
                RemoteParseV4Repository._validate_immutable_head_projection(
                    source_head,
                    cleanup_pending,
                )
        except ValueError as exc:
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession source head drifted from lifecycle witness"
            ) from exc
        if (
            cleanup_pending is None
            or cleanup_plan is None
            or cleanup_pending.state != "cleanup_pending"
            or cleanup_pending.lifecycle_version
            != receipt.source_lifecycle_version + 1
            or cleanup_pending.previous_checkpoint_sha256 != checkpoint.sha256
            or cleanup_pending.supersession_receipt_sha256 != receipt.sha256
            or cleanup_pending.cleanup_plan_sha256 != cleanup_plan.sha256
            or cleanup_plan.outcome != "superseded"
            or cleanup_plan.attempt_id != source_head["attempt_id"]
            or cleanup_plan.fence_identity != source_head["fence_identity"]
            or cleanup_plan.document_id != source_head["document_id"]
            or cleanup_plan.processing_run_id
            != source_head["processing_run_id"]
            or cleanup_plan.supersession_receipt_sha256 != receipt.sha256
            or cleanup_plan.source_state != receipt.source_state
            or cleanup_plan.source_lifecycle_version
            != receipt.source_lifecycle_version
            or cleanup_plan.source_checkpoint_sha256 != checkpoint.sha256
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession source cleanup transition drifted"
            )

    @staticmethod
    def _validate_winner_authority(
        *,
        head: Mapping[Any, Any],
        history: tuple[RemoteParseCheckpointV4, ...],
        winner: AtomicPublicationWinnerV4,
    ) -> None:
        before = _checkpoint_by_identity(
            history,
            winner.lifecycle_version_before,
            winner.local_checkpoint_sha256,
        )
        if not 0 <= winner.lifecycle_version_after < len(history):
            raise RemoteParseV4AuthorityViolation(
                "v4 publication winner successor is absent"
            )
        after = history[winner.lifecycle_version_after]
        if (
            winner.attempt_id != head["attempt_id"]
            or winner.fence_identity != head["fence_identity"]
            or winner.document_id != head["document_id"]
            or winner.processing_run_id != head["processing_run_id"]
            or winner.publish_attempt_generation != head["attempt_generation"]
            or before.state != "local_materialized"
            or winner.lifecycle_version_after
            != winner.lifecycle_version_before + 1
            or after.state != "publish_committed"
            or after.publication_winner_sha256 != winner.sha256
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 publication winner drifted from durable authority"
            )

    def _load_superseding_seed(
        self,
        link: V4SupersessionLinkAuthority,
        receipt: SupersessionReceiptV4,
    ) -> tuple[
        RemoteParseCheckpointV4,
        ResourceReservationV4,
        PreparationIntentV4,
        SnapshotReceiptV4,
    ]:
        if (
            receipt.superseding_attempt_id != link.superseding_attempt_id
            or receipt.superseding_checkpoint_sha256
            != link.superseding_checkpoint_sha256
            or receipt.sha256 != link.source_supersession_receipt_sha256
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession receipt drifted from immutable link"
            )
        head = self._session.execute(
            sa.select(models.RemoteParseAttempt.__table__).where(
                models.RemoteParseAttempt.__table__.c.attempt_id
                == link.superseding_attempt_id
            )
        ).mappings().one_or_none()
        if head is None or (
            head["checkpoint_contract_version"] != 4
            or head["fence_identity"] != link.superseding_fence_identity
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession target head is absent or drifted"
            )
        checkpoint_row = self._session.execute(
            sa.select(models.RemoteParseV4Checkpoint.__table__).where(
                models.RemoteParseV4Checkpoint.__table__.c.attempt_id
                == link.superseding_attempt_id,
                models.RemoteParseV4Checkpoint.__table__.c.lifecycle_version == 0,
                models.RemoteParseV4Checkpoint.__table__.c.checkpoint_sha256
                == link.superseding_checkpoint_sha256,
            )
        ).mappings().one_or_none()
        if checkpoint_row is None:
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession target H0 is absent"
            )
        checkpoint, reservation = self._decode_checkpoint_row(checkpoint_row)
        if reservation is None:
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession target H0 lacks reservation"
            )
        evidence_rows = self._session.execute(
            sa.select(models.RemoteParseV4Evidence.__table__).where(
                models.RemoteParseV4Evidence.__table__.c.attempt_id
                == link.superseding_attempt_id,
                models.RemoteParseV4Evidence.__table__.c.evidence_kind.in_(
                    ("preparation_intent", "snapshot_receipt")
                ),
            )
        ).mappings().all()
        encoded: dict[str, EncodedRemoteParseEvidenceV4] = {
            item.kind: item
            for item in (self._decode_evidence_row(row) for row in evidence_rows)
        }
        preparation = _evidence_value(
            encoded,
            "preparation_intent",
            PreparationIntentV4,
        )
        snapshot = _evidence_value(
            encoded,
            "snapshot_receipt",
            SnapshotReceiptV4,
        )
        if preparation is None or snapshot is None:
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession target seed evidence is incomplete"
            )
        try:
            validate_superseding_checkpoint_seed_evidence_v4(
                checkpoint=checkpoint,
                reservation=reservation,
                preparation_intent=preparation,
                snapshot_receipt=snapshot,
            )
        except ValueError as exc:
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession target seed is invalid"
            ) from exc
        try:
            self._validate_superseding_target_head_projection(
                head,
                checkpoint,
                preparation,
            )
        except ValueError as exc:
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession target head drifted from H0"
            ) from exc
        if (
            checkpoint.attempt_generation
            != receipt.superseding_attempt_generation
            or checkpoint.document_id != receipt.superseding_document_id
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 supersession target identity drifted from receipt"
            )
        return checkpoint, reservation, preparation, snapshot

    @staticmethod
    def _validate_head_projection(
        head: Mapping[Any, Any],
        checkpoint: RemoteParseCheckpointV4,
    ) -> None:
        RemoteParseV4Repository._validate_immutable_head_projection(
            head,
            checkpoint,
        )
        expected = {
            "state": checkpoint.state,
            "row_version": checkpoint.lifecycle_version,
            "current_checkpoint_sha256": checkpoint.sha256,
        }
        _require_projection(head, expected, "v4 head")
        is_current = head["is_current"]
        claim_generation = head["claim_generation"]
        claim_owner = head["claim_owner_identity"]
        claim_lease = head["claim_lease_until"]
        if type(is_current) is not bool:
            raise ValueError("v4 head currentness is invalid")
        if (
            type(claim_generation) is not int
            or not 0 <= claim_generation <= _MAX_INT
        ):
            raise ValueError("v4 head claim generation is invalid")
        if (claim_owner is None) != (claim_lease is None):
            raise ValueError("v4 head claim columns are partial")
        if claim_owner is not None and (
            type(claim_owner) is not str
            or not claim_owner.strip()
            or len(claim_owner.encode("utf-8")) > 128
            or type(claim_lease) is not datetime
        ):
            raise ValueError("v4 head live claim is invalid")
        unclaimed = (
            claim_generation == 0
            and claim_owner is None
            and claim_lease is None
        )
        claimed = (
            1 <= claim_generation <= _MAX_INT
            and claim_owner is not None
            and claim_lease is not None
        )
        if checkpoint.state in _CURRENT_STATES:
            if checkpoint.state == "prepared" and not is_current:
                if checkpoint.lifecycle_version != 0 or not unclaimed:
                    raise ValueError("v4 staged H0 claim shape drifted")
            elif not is_current:
                raise ValueError("v4 nonfinal head is not current")
            elif checkpoint.state == "prepared":
                if not (unclaimed or claimed):
                    raise ValueError("v4 prepared claim shape drifted")
            elif not claimed:
                raise ValueError("v4 non-prepared current head is not claimed")
        elif checkpoint.state in _FINAL_STATES:
            if is_current:
                raise ValueError("v4 final head remains current")
            if checkpoint.lifecycle_version == 0:
                if (
                    checkpoint.state not in {"preparation_failed", "superseded"}
                    or not unclaimed
                ):
                    raise ValueError("v4 resource-free final claim shape drifted")
            elif not (
                1 <= claim_generation <= _MAX_INT
                and claim_owner is None
                and claim_lease is None
            ):
                raise ValueError("v4 resourceful final claim shape drifted")
        else:
            raise ValueError("v4 head state is unsupported")

    @staticmethod
    def _validate_immutable_head_projection(
        head: Mapping[Any, Any],
        checkpoint: RemoteParseCheckpointV4,
    ) -> None:
        expected = {
            "attempt_id": checkpoint.attempt_id,
            "processing_run_id": checkpoint.processing_run_id,
            "document_id": checkpoint.document_id,
            "attempt_generation": checkpoint.attempt_generation,
            "fence_identity": checkpoint.fence_identity,
            "source_pdf_sha256": checkpoint.source_pdf_sha256,
            "request_sha256": checkpoint.request_sha256,
            "runtime_epoch_sha256": checkpoint.runtime_epoch_sha256,
        }
        _require_projection(head, expected, "v4 immutable head")

    @staticmethod
    def _validate_superseding_target_head_projection(
        head: Mapping[Any, Any],
        checkpoint: RemoteParseCheckpointV4,
        preparation: PreparationIntentV4,
    ) -> None:
        RemoteParseV4Repository._validate_immutable_head_projection(
            head,
            checkpoint,
        )
        if head["parser_target_sha256"] != preparation.parser_target_sha256:
            raise ValueError(
                "v4 superseding target parser target drifted from preparation"
            )

    @staticmethod
    def _validate_claim_request(
        *,
        expectation: V4HeadExpectation,
        owner_identity: str,
        lease_seconds: int,
    ) -> None:
        if (
            type(expectation) is not V4HeadExpectation
            or type(owner_identity) is not str
            or not owner_identity.strip()
            or len(owner_identity.encode("utf-8")) > 128
            or type(lease_seconds) is not int
            or not 1 <= lease_seconds <= 300
        ):
            raise ValueError("v4 claim request is invalid")

    @staticmethod
    def _require_expected_head(
        head: Mapping[Any, Any],
        expectation: V4HeadExpectation,
    ) -> None:
        if head["checkpoint_contract_version"] != 4:
            raise V4HeadStale("v4 head crossed contract version")
        if head["state"] in _FINAL_STATES:
            raise V4AttemptFinal("v4 attempt is final")
        if (
            not head["is_current"]
            or head["fence_identity"] != expectation.fence_identity
            or head["state"] != expectation.state
            or head["row_version"] != expectation.lifecycle_version
            or head["current_checkpoint_sha256"]
            != expectation.checkpoint_sha256
        ):
            raise V4HeadStale("v4 exact head expectation is stale")

    @staticmethod
    def _database_clock() -> Any:
        return sa.select(
            sa.cast(
                sa.func.clock_timestamp(),
                sa.DateTime(timezone=True),
            ).label("database_observed_at")
        ).cte("database_clock").prefix_with("MATERIALIZED")

    def _observe_database_time(self) -> datetime:
        value = self._session.execute(
            sa.select(
                sa.cast(
                    sa.func.clock_timestamp(),
                    sa.DateTime(timezone=True),
                )
            )
        ).scalar_one()
        return _utc(value)


def recovery_candidate_from_head_row(
    row: Mapping[Any, Any],
    *,
    database_observed_at: datetime,
) -> RecoveryCandidate:
    """Validate and project one V4 recovery head without loading its history."""

    try:
        contract_version = row["checkpoint_contract_version"]
        is_current = row["is_current"]
        state = row["state"]
        lifecycle_version = row["row_version"]
        claim_generation = row["claim_generation"]
        claim_owner_identity = row["claim_owner_identity"]
        claim_lease_until = row["claim_lease_until"]
        attempt_id = row["attempt_id"]
    except KeyError as exc:
        raise RemoteParseV4AuthorityViolation(
            "v4 recovery head projection is incomplete"
        ) from exc

    if (
        type(contract_version) is not int
        or contract_version != 4
        or type(is_current) is not bool
        or not is_current
        or type(state) is not str
        or state not in _CURRENT_STATES
        or isinstance(lifecycle_version, bool)
        or not isinstance(lifecycle_version, int)
        or not 0 <= lifecycle_version <= _MAX_INT
        or isinstance(claim_generation, bool)
        or not isinstance(claim_generation, int)
        or not 0 <= claim_generation <= _MAX_INT
        or not isinstance(database_observed_at, datetime)
    ):
        raise RemoteParseV4AuthorityViolation(
            "v4 recovery head projection violates durable authority"
        )

    lease_remaining_seconds: float | None
    if claim_owner_identity is None:
        if (
            claim_generation != 0
            or claim_lease_until is not None
            or state != "prepared"
            or lifecycle_version != 0
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 unclaimed recovery head has an invalid durable shape"
            )
        lease_remaining_seconds = None
    else:
        if (
            type(claim_owner_identity) is not str
            or not claim_owner_identity.strip()
            or claim_generation < 1
            or not isinstance(claim_lease_until, datetime)
        ):
            raise RemoteParseV4AuthorityViolation(
                "v4 claimed recovery head has an invalid durable shape"
            )
        remaining = _utc(claim_lease_until) - _utc(database_observed_at)
        remaining_microseconds = (
            remaining.days * 86_400_000_000
            + remaining.seconds * 1_000_000
            + remaining.microseconds
        )
        lease_remaining_seconds = remaining_microseconds / 1_000_000

    try:
        return RecoveryCandidate(
            attempt_id=attempt_id,
            state=state,
            lifecycle_version=lifecycle_version,
            claim_generation=claim_generation,
            claim_owner_identity=claim_owner_identity,
            lease_remaining_seconds=lease_remaining_seconds,
        )
    except (TypeError, ValueError) as exc:
        raise RemoteParseV4AuthorityViolation(
            "v4 recovery head cannot be represented canonically"
        ) from exc


def _link_authority(row: Mapping[Any, Any]) -> V4SupersessionLinkAuthority:
    if (
        row["source_evidence_kind"] != "supersession_receipt"
        or row["superseding_lifecycle_version"] != 0
    ):
        raise RemoteParseV4AuthorityViolation(
            "v4 supersession link projection is invalid"
        )
    return V4SupersessionLinkAuthority(
        source_attempt_id=row["source_attempt_id"],
        source_fence_identity=row["source_fence_identity"],
        source_supersession_receipt_sha256=row[
            "source_supersession_receipt_sha256"
        ],
        superseding_attempt_id=row["superseding_attempt_id"],
        superseding_fence_identity=row["superseding_fence_identity"],
        superseding_checkpoint_sha256=row["superseding_checkpoint_sha256"],
    )


def _evidence_value(
    evidence_by_kind: Mapping[str, EncodedRemoteParseEvidenceV4],
    kind: str,
    expected_type: type[Any],
) -> Any | None:
    encoded = evidence_by_kind.get(kind)
    if encoded is None:
        return None
    if type(encoded.value) is not expected_type:
        raise RemoteParseV4AuthorityViolation(
            f"v4 {kind} evidence has the wrong type"
        )
    return encoded.value


def _checkpoint_by_identity(
    history: tuple[RemoteParseCheckpointV4, ...],
    lifecycle_version: int,
    checkpoint_sha256: str,
) -> RemoteParseCheckpointV4:
    if not 0 <= lifecycle_version < len(history):
        raise RemoteParseV4AuthorityViolation(
            "v4 referenced checkpoint version is absent"
        )
    checkpoint = history[lifecycle_version]
    if checkpoint.sha256 != checkpoint_sha256:
        raise RemoteParseV4AuthorityViolation(
            "v4 referenced checkpoint hash drifted"
        )
    return checkpoint


def _evidence_subset_exact(
    persisted: tuple[EncodedRemoteParseEvidenceV4, ...],
    expected: tuple[EncodedRemoteParseEvidenceV4, ...],
) -> bool:
    by_kind = {item.kind: item for item in persisted}
    return all(by_kind.get(item.kind) == item for item in expected)


def _require_projection(
    row: Mapping[Any, Any],
    expected: Mapping[str, object],
    label: str,
) -> None:
    for name, value in expected.items():
        observed = row[name]
        if isinstance(value, datetime) and isinstance(observed, datetime):
            if _utc(observed) != _utc(value):
                raise ValueError(f"{label} column {name} drifted")
        elif observed != value:
            raise ValueError(f"{label} column {name} drifted")


def _exact_bytes(value: object, label: str) -> bytes:
    try:
        exact = bytes(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"v4 {label} bytes are unavailable") from exc
    if not exact:
        raise ValueError(f"v4 {label} bytes are empty")
    return exact


def _identity(value: object, label: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"v4 {label} identity is invalid")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["RemoteParseV4Repository", "recovery_candidate_from_head_row"]
