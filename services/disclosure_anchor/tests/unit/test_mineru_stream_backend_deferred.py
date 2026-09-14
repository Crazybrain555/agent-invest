"""The durable backend preserves a proved-absent intent during stream pause."""

from __future__ import annotations

import unittest
from unittest import mock

from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
)
from disclosure_anchor.application.ports.mineru_stream_pressure import (
    StreamSubmissionDeferred,
)
from disclosure_anchor.application.ports.remote_provider_v4 import (
    RemoteProviderV4Port,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    RetryStage,
    StageWaiting,
)
from tests.unit import test_staged_coordinator_backend_v4 as backend_fixtures


class MinerUStreamBackendDeferredTests(unittest.TestCase):
    def test_deferred_submission_waits_without_advancing_durable_intent(self) -> None:
        authority = backend_fixtures._authority("reconciling")
        work = backend_fixtures._work(authority)
        checkpoint = authority.checkpoint
        evidence = authority.evidence
        deferred = StreamSubmissionDeferred("latest pressure sample is unknown")
        remote = mock.Mock(spec=RemoteProviderV4Port)
        remote.reconcile_or_submit.side_effect = deferred
        backend, persistence, inputs, _ = backend_fixtures._backend(
            authority, remote=remote,
        )
        inputs.submission_command.return_value = mock.sentinel.submission_command

        with self.assertRaises(StageWaiting) as raised:
            backend.run_remote(
                work,
                credit_allowance=ResourceCreditVector(provider_tasks=1, ack_items=1),
                stage_guard=backend_fixtures._guard(),
            )

        # StageWaiting selects the non-error-budget branch in the coordinator;
        # this boundary must never turn the pause into RetryStage or failure.
        self.assertIs(type(raised.exception), StageWaiting)
        self.assertNotIsInstance(raised.exception, RetryStage)
        self.assertIs(raised.exception.__cause__, deferred)
        self.assertEqual(raised.exception.retry_after_seconds, 0.25)
        remote.reconcile_or_submit.assert_called_once_with(
            mock.sentinel.submission_command,
        )
        self.assertEqual(persistence.appends, [])
        self.assertIs(persistence.authority, authority)
        self.assertEqual(persistence.authority.state, "reconciling")
        self.assertEqual(persistence.authority.checkpoint, checkpoint)
        self.assertEqual(persistence.authority.evidence, evidence)
        self.assertEqual(persistence.reload_claim(work), work)
        self.assertEqual(
            tuple(item.kind for item in evidence),
            ("preparation_intent", "snapshot_receipt", "submission_intent"),
        )
        self.assertEqual(checkpoint.held_resource_credit.provider_tasks, 0)
        self.assertEqual(checkpoint.held_resource_credit.ack_items, 0)


if __name__ == "__main__":
    unittest.main()
