"""Direct tests for the seven-lane durable V4 backend."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
import unittest
from unittest import mock

from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    FailureReceiptV4,
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    LocalCleanupPlanV4,
    RemoteParseCheckpointV4,
    build_initial_remote_parse_checkpoint_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Authority,
    V4SuccessorAppend,
)
from disclosure_anchor.application.ports.remote_provider_v4 import (
    RemoteProviderFailedV4,
    RemoteProviderUnavailableV4,
    RemoteProviderWaitingV4,
    RemoteSubmissionAmbiguousV4,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
)
from disclosure_anchor.application.services.staged_coordinator_backend_v4 import (
    DurableStagedCoordinatorBackendV4,
    ExpectedV4AttemptFailure,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorWork,
    RetryStage,
    StageLeaseGuard,
)
from tests.unit.test_remote_parse_evidence_v4 import (
    _exact_materialization_reservation_and_allowance,
    _typed_happy_bundle,
)


class _Persistence:
    def __init__(self, authority: RemoteParseV4Authority) -> None:
        self.authority = authority
        self.appends: list[V4SuccessorAppend] = []

    def load_owned_authority(self, _work: CoordinatorWork) -> RemoteParseV4Authority:
        return self.authority

    def append_successor(
        self,
        _work: CoordinatorWork,
        append: V4SuccessorAppend,
    ) -> CoordinatorWork:
        self.appends.append(append)
        if append.successor.state in {
            "acked",
            "remote_failed",
            "local_failed",
            "pre_submission_failed",
            "preparation_failed",
            "superseded",
        }:
            return CoordinatorWork(
                attempt_id=append.successor.attempt_id,
                state=append.successor.state,
                lifecycle_version=append.successor.lifecycle_version,
                claim_generation=1,
                claim_owner_identity=None,
                lease_expires_monotonic=None,
                credit_reservation=ResourceCreditVector(),
                credits=ResourceCreditVector(),
            )
        return _work_for(append.successor, self.authority.reservation.reserved_credit)

    def reload_claim(self, _work: CoordinatorWork) -> CoordinatorWork:
        return _work


def _fixture():
    (
        _final,
        reservation,
        values,
        _published,
        _manifest,
        _provider_envelope,
        cleanup_pending,
        ack_pending,
        resourceful_history,
    ) = _typed_happy_bundle()
    return reservation, values, cleanup_pending, ack_pending, resourceful_history


def _authority(state: str) -> RemoteParseV4Authority:
    reservation, values, cleanup_pending, ack_pending, history = _fixture()
    preparation, snapshot, submission = values[:3]
    state_history = {
        "prepared": history[:1],
        "reconciling": history[:2],
        "submitted": history[:3],
        "remote_terminal": history[:4],
        "materializing": history[:5],
        "local_materialized": history[:6],
        "publish_committed": history[:7],
        "cleanup_pending": (*history, cleanup_pending),
        "ack_pending": (*history, cleanup_pending, ack_pending),
    }[state]
    evidence_count = {
        "prepared": 2,
        "reconciling": 3,
        "submitted": 4,
        "remote_terminal": 5,
        "materializing": 6,
        "local_materialized": 7,
        "publish_committed": 7,
        "cleanup_pending": 8,
        "ack_pending": 9,
    }[state]
    checkpoint = state_history[-1]
    return RemoteParseV4Authority(
        attempt_id=checkpoint.attempt_id,
        processing_run_id=checkpoint.processing_run_id,
        document_id=checkpoint.document_id,
        attempt_generation=checkpoint.attempt_generation,
        fence_identity=checkpoint.fence_identity,
        source_pdf_sha256=checkpoint.source_pdf_sha256,
        parser_target_sha256=preparation.parser_target_sha256,
        request_sha256=reservation.request_sha256,
        runtime_epoch_sha256=reservation.runtime_epoch_sha256,
        client_submit_key=submission.client_submit_key,
        state=checkpoint.state,
        is_current=True,
        lifecycle_version=checkpoint.lifecycle_version,
        checkpoint_sha256=checkpoint.sha256,
        claim_generation=1,
        claim_owner_identity="worker-1",
        claim_lease_until=datetime(2030, 1, 1, tzinfo=UTC),
        checkpoint_history=state_history,
        reservation=reservation,
        evidence=tuple(
            encode_remote_parse_evidence_v4(value)
            for value in values[:evidence_count]
        ),
        publication_winner=None,
        secret_history=(),
        source_supersession_link=None,
        staged_by_link=None,
        database_lease=None,
    )


def _delayed_prepared_authority() -> RemoteParseV4Authority:
    authority = _authority("prepared")
    assert authority.reservation is not None
    preparation = authority.evidence[0].value
    prepared = build_initial_remote_parse_checkpoint_v4(
        reservation=authority.reservation,
        preparation_intent_sha256=preparation.sha256,
        held_resource_credit=authority.checkpoint.held_resource_credit,
    )
    return replace(
        authority,
        checkpoint_sha256=prepared.sha256,
        checkpoint_history=(prepared,),
        evidence=(authority.evidence[0],),
    )


def _work_for(
    checkpoint: RemoteParseCheckpointV4,
    reservation: ResourceCreditVector,
) -> CoordinatorWork:
    return CoordinatorWork(
        attempt_id=checkpoint.attempt_id,
        state=checkpoint.state,
        lifecycle_version=checkpoint.lifecycle_version,
        claim_generation=1,
        claim_owner_identity="worker-1",
        lease_expires_monotonic=90.0,
        credit_reservation=reservation,
        credits=checkpoint.held_resource_credit,
    )


def _work(authority: RemoteParseV4Authority) -> CoordinatorWork:
    assert authority.reservation is not None
    return _work_for(authority.checkpoint, authority.reservation.reserved_credit)


def _guard() -> StageLeaseGuard:
    return StageLeaseGuard(
        deadline_monotonic=100.0,
        _revoked=Event(),
        _monotonic=lambda: 1.0,
    )


def _backend(
    authority: RemoteParseV4Authority,
    *,
    inputs: mock.Mock | None = None,
    remote: mock.Mock | None = None,
    materialization: mock.Mock | None = None,
    wall_clock=lambda: 2.0,
) -> tuple[DurableStagedCoordinatorBackendV4, _Persistence, mock.Mock, mock.Mock]:
    persistence = _Persistence(authority)
    inputs = mock.Mock() if inputs is None else inputs
    remote = mock.Mock() if remote is None else remote
    materialization = mock.Mock() if materialization is None else materialization
    backend = DurableStagedCoordinatorBackendV4(
        persistence=persistence,  # type: ignore[arg-type]
        inputs=inputs,
        remote=remote,
        materialization=materialization,
        secret_cipher=mock.Mock(),
        claim_guard=mock.Mock(),
        publication_requests=mock.Mock(),
        publisher=mock.Mock(),
        poll_seconds=0.25,
        wall_clock=wall_clock,
    )
    return backend, persistence, inputs, materialization


class DurableStagedCoordinatorBackendV4Tests(unittest.TestCase):
    def test_credit_check_uses_positive_transition_delta(self) -> None:
        authority = _authority("prepared")
        reconciling = _authority("reconciling").checkpoint
        DurableStagedCoordinatorBackendV4._require_credit_transition(
            authority.checkpoint,
            reconciling,
            ResourceCreditVector(remote_waits=1),
        )
        with self.assertRaises(RetryStage):
            DurableStagedCoordinatorBackendV4._require_credit_transition(
                authority.checkpoint,
                reconciling,
                ResourceCreditVector(),
            )
        DurableStagedCoordinatorBackendV4._require_credit_transition(
            _authority("local_materialized").checkpoint,
            _authority("local_materialized").checkpoint,
            ResourceCreditVector(),
        )

    def test_preflight_appends_exact_reconciling_successor(self) -> None:
        authority = _authority("prepared")
        _, values, _, _, _ = _fixture()
        inputs = mock.Mock()
        inputs.submission_intent.return_value = values[2]
        backend, persistence, _, materialization = _backend(
            authority,
            inputs=inputs,
        )

        updated = backend.prepare_remote_io(
            _work(authority),
            credit_allowance=ResourceCreditVector(remote_waits=1),
            stage_guard=_guard(),
        )

        self.assertEqual(updated.state, "reconciling")
        self.assertEqual(len(persistence.appends), 1)
        self.assertEqual(
            tuple(item.kind for item in persistence.appends[0].new_evidence),
            ("submission_intent",),
        )
        materialization.create_or_reconcile_snapshot_v4.assert_not_called()

    def test_preflight_creates_missing_snapshot_and_appends_both_facts(self) -> None:
        authority = _delayed_prepared_authority()
        _, values, _, _, _ = _fixture()
        inputs = mock.Mock()
        inputs.source_pdf.return_value = mock.sentinel.source_pdf
        inputs.submission_intent.return_value = values[2]
        materialization = mock.Mock()
        materialization.create_or_reconcile_snapshot_v4.return_value = values[1]
        backend, persistence, _, _ = _backend(
            authority,
            inputs=inputs,
            materialization=materialization,
        )

        updated = backend.prepare_remote_io(
            _work(authority),
            credit_allowance=ResourceCreditVector(remote_waits=1),
            stage_guard=_guard(),
        )

        self.assertEqual(updated.state, "reconciling")
        self.assertEqual(
            tuple(item.kind for item in persistence.appends[0].new_evidence),
            ("snapshot_receipt", "submission_intent"),
        )
        materialization.create_or_reconcile_snapshot_v4.assert_called_once()

    def test_ambiguous_submission_retries_without_append(self) -> None:
        authority = _authority("reconciling")
        inputs = mock.Mock()
        inputs.submission_command.return_value = mock.sentinel.command
        remote = mock.Mock()
        remote.reconcile_or_submit.side_effect = RemoteSubmissionAmbiguousV4(
            "response unknown"
        )
        materialization = mock.Mock()
        materialization.submission_snapshot_source_v4.return_value = (
            mock.sentinel.snapshot_source
        )
        backend, persistence, _, _ = _backend(
            authority,
            inputs=inputs,
            remote=remote,
            materialization=materialization,
        )

        with self.assertRaises(RetryStage):
            backend.run_remote(
                _work(authority),
                credit_allowance=ResourceCreditVector(
                    provider_tasks=1,
                    ack_items=1,
                ),
                stage_guard=_guard(),
            )
        self.assertEqual(persistence.appends, [])

    def test_waiting_past_durable_runaway_becomes_remote_failure(self) -> None:
        authority = _authority("submitted")
        inputs = mock.Mock()
        inputs.poll_command.return_value = mock.sentinel.poll
        inputs.remote_runaway_seconds.return_value = 10
        remote = mock.Mock()
        remote.poll_once.return_value = RemoteProviderWaitingV4(
            remote_task_identity="task-1",
            status="processing",
            response_sha256="sha256:" + "1" * 64,
            response_byte_count=2,
        )
        backend, persistence, _, _ = _backend(
            authority,
            inputs=inputs,
            remote=remote,
            wall_clock=lambda: 12.0,
        )

        with mock.patch.object(
            backend,
            "_capability",
            return_value=mock.sentinel.capability,
        ):
            updated = backend.run_remote(
                _work(authority),
                credit_allowance=ResourceCreditVector(provider_result_bytes=20),
                stage_guard=_guard(),
            )

        self.assertEqual(updated.state, "cleanup_pending")
        failure, plan = tuple(
            item.value for item in persistence.appends[0].new_evidence
        )
        self.assertIsInstance(failure, FailureReceiptV4)
        self.assertEqual(failure.error_code, "remote_parse_runaway")
        self.assertIsInstance(plan, LocalCleanupPlanV4)
        self.assertEqual(plan.outcome, "remote_failure")

    def test_full_provider_failure_message_is_item_local(self) -> None:
        authority = _authority("submitted")
        inputs = mock.Mock()
        inputs.poll_command.return_value = mock.sentinel.poll
        remote = mock.Mock()
        remote.poll_once.return_value = RemoteProviderFailedV4(
            remote_task_identity="task-1",
            provider_error="x" * 4096,
            response_sha256="sha256:" + "1" * 64,
            response_byte_count=2,
        )
        backend, persistence, _, _ = _backend(
            authority,
            inputs=inputs,
            remote=remote,
        )

        with mock.patch.object(
            backend,
            "_capability",
            return_value=mock.sentinel.capability,
        ):
            updated = backend.run_remote(
                _work(authority),
                credit_allowance=ResourceCreditVector(),
                stage_guard=_guard(),
            )

        self.assertEqual(updated.state, "cleanup_pending")
        failure = persistence.appends[0].new_evidence[0].value
        self.assertIsInstance(failure, FailureReceiptV4)
        self.assertEqual(failure.message, "x" * 4096)

    def test_local_prepare_known_failure_drains_to_cleanup(self) -> None:
        authority = _authority("remote_terminal")
        inputs = mock.Mock()
        inputs.materialization_intent.side_effect = ExpectedV4AttemptFailure(
            error_code="unsupported_provider_result",
            message="provider result is unsupported",
        )
        backend, persistence, _, _ = _backend(authority, inputs=inputs)

        with mock.patch.object(
            backend,
            "_capability",
            return_value=mock.sentinel.capability,
        ):
            updated = backend.prepare_local_io(
                _work(authority),
                credit_allowance=ResourceCreditVector(
                    materialization_items=1,
                    compressed_bytes=20,
                    decoded_bytes=30,
                    temp_disk_bytes=8192,
                ),
                stage_guard=_guard(),
            )

        self.assertEqual(updated.state, "cleanup_pending")
        failure = persistence.appends[0].new_evidence[0].value
        self.assertIsInstance(failure, FailureReceiptV4)
        self.assertEqual(failure.outcome, "local_failure")
        self.assertIsNone(failure.materialization_intent_sha256)

    def test_commit_reopens_both_restart_inputs_before_publication(self) -> None:
        authority = _authority("local_materialized")
        (
            _,
            _,
            values,
            _,
            manifest,
            envelope,
            _,
            _,
            _,
        ) = _typed_happy_bundle()
        materialized = MaterializedProviderDocumentV4(
            receipt=values[6],
            intent=values[5],
            provider_envelope=envelope,
            manifest=manifest,
        )
        materialization = mock.Mock()
        materialization.reopen_materialized_v4.return_value = materialized
        backend, persistence, _, _ = _backend(
            authority,
            materialization=materialization,
        )
        request = mock.sentinel.request
        backend._publication_requests.build_or_reopen.return_value = request

        updated = backend.commit(
            _work(authority),
            credit_allowance=ResourceCreditVector(),
            stage_guard=_guard(),
        )

        self.assertEqual(updated, _work(authority))
        materialization.reopen_materialized_v4.assert_called_once()
        backend._publication_requests.build_or_reopen.assert_called_once_with(
            checkpoint=authority.checkpoint,
            materialized=materialized,
            stage_guard=mock.ANY,
        )
        backend._publisher.execute.assert_called_once_with(
            request=request,
            checkpoint=authority.checkpoint,
            materialized=materialized,
            claim=authority.claim_witness,
            claim_guard=backend._claim_guard,
        )
        self.assertEqual(persistence.appends, [])

    def test_result_download_and_ack_unavailability_are_retriable(self) -> None:
        materializing = _authority("materializing")
        local_materialization = mock.Mock()
        local_materialization.materialize_v4.side_effect = (
            RemoteProviderUnavailableV4("result GET unavailable")
        )
        local_inputs = mock.Mock()
        _, allowance = _exact_materialization_reservation_and_allowance()
        local_inputs.materialization_allowance.return_value = allowance
        local_inputs.result_lease_seconds.return_value = 300
        local_backend, local_persistence, _, _ = _backend(
            materializing,
            inputs=local_inputs,
            materialization=local_materialization,
        )
        reservation = materializing.reservation
        assert reservation is not None
        output_grant = ResourceCreditVector(
            output_items=reservation.reserved_credit.output_items,
            output_bytes=reservation.reserved_credit.output_bytes,
            output_pages=reservation.reserved_credit.output_pages,
        )
        with (
            mock.patch.object(
                local_backend,
                "_capability",
                return_value=mock.sentinel.capability,
            ),
            self.assertRaises(RetryStage),
        ):
            local_backend.run_local(
                _work(materializing),
                credit_allowance=output_grant,
                stage_guard=_guard(),
            )
        self.assertEqual(local_persistence.appends, [])

        ack_pending = _authority("ack_pending")
        ack_materialization = mock.Mock()
        (
            _,
            _,
            ack_values,
            _,
            ack_manifest,
            ack_envelope,
            _,
            _,
            _,
        ) = _typed_happy_bundle()
        ack_materialization.reopen_materialized_v4.return_value = (
            MaterializedProviderDocumentV4(
                receipt=ack_values[6],
                intent=ack_values[5],
                provider_envelope=ack_envelope,
                manifest=ack_manifest,
            )
        )
        ack_materialization.acknowledge_v4.side_effect = (
            RemoteProviderUnavailableV4("ACK unavailable")
        )
        ack_backend, ack_persistence, _, _ = _backend(
            ack_pending,
            materialization=ack_materialization,
        )
        with (
            mock.patch.object(
                ack_backend,
                "_capability",
                return_value=mock.sentinel.capability,
            ),
            self.assertRaises(RetryStage),
        ):
            ack_backend.acknowledge(_work(ack_pending), stage_guard=_guard())
        self.assertEqual(ack_persistence.appends, [])

    def test_cleanup_response_loss_and_ack_do_not_reopen_removed_output(
        self,
    ) -> None:
        cleanup_pending = _authority("cleanup_pending")
        _, values, _, _, _ = _fixture()

        for _ in range(2):
            cleanup_materialization = mock.Mock()
            cleanup_materialization.cleanup_v4.return_value = values[8]
            cleanup_backend, _, _, _ = _backend(
                cleanup_pending,
                materialization=cleanup_materialization,
            )

            updated = cleanup_backend.cleanup(
                _work(cleanup_pending),
                credit_allowance=ResourceCreditVector(),
                stage_guard=_guard(),
            )

            self.assertEqual(updated.state, "ack_pending")
            cleanup_materialization.cleanup_v4.assert_called_once()
            cleanup_materialization.reopen_materialized_v4.assert_not_called()

        ack_pending = _authority("ack_pending")
        ack_materialization = mock.Mock()
        ack_materialization.acknowledge_v4.return_value = values[9]
        ack_backend, _, _, _ = _backend(
            ack_pending,
            materialization=ack_materialization,
        )
        with mock.patch.object(
            ack_backend,
            "_capability",
            return_value=mock.sentinel.capability,
        ):
            updated = ack_backend.acknowledge(
                _work(ack_pending),
                stage_guard=_guard(),
            )

        self.assertEqual(updated.state, "acked")
        ack_materialization.acknowledge_v4.assert_called_once()
        ack_materialization.reopen_materialized_v4.assert_not_called()


if __name__ == "__main__":
    unittest.main()
