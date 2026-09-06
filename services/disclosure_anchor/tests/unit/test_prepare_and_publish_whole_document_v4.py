from __future__ import annotations

from types import SimpleNamespace
from threading import Event
from typing import Any, cast
import unittest

from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactsReadyV4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    RemoteParseCheckpointV4,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationCommitResponseLost,
    AtomicPublicationWinnerV4,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
    V4ClaimGuard,
    V4ClaimWitness,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.use_cases.prepare_and_publish_whole_document_v4 import (
    PrepareAndPublishWholeDocumentV4,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    StageLeaseGuard,
    StageLeaseLost,
)


class _Uow:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __enter__(self) -> _Uow:
        self._events.append("lease-enter")
        return self

    def __exit__(self, *args: object) -> None:
        self._events.append("lease-exit")


class _Readiness:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.reference = object()
        self.ready = cast(AtomicPublicationArtifactsReadyV4, object())

    def prepare_or_replay(self, **kwargs: object) -> object:
        self.events.append("prepare")
        return self.reference

    def verify_ready(self, **kwargs: object) -> AtomicPublicationArtifactsReadyV4:
        self.events.append(
            "verify-post" if kwargs.get("expected_winner") is not None else "verify-pre"
        )
        return self.ready


class _RequestFactory:
    def __init__(self, events: list[str], request: AtomicPublicationRequestV4) -> None:
        self.events = events
        self.request = request

    def build_or_reopen(self, **_: object) -> AtomicPublicationRequestV4:
        self.events.append("build-or-reopen")
        return self.request


class _StageGuard:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def checkpoint(self) -> None:
        self.events.append("stage-checkpoint")

    def remaining_seconds(self) -> float:
        return 1.0


class _Publisher:
    def __init__(
        self,
        events: list[str],
        *,
        commits: list[object],
        reloads: list[object | None] | None = None,
    ) -> None:
        self.events = events
        self.commits = commits
        self.reloads = [] if reloads is None else reloads

    def commit_whole_document(self, *args: object, **kwargs: object) -> object:
        self.events.append("commit")
        result = self.commits.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def reload_commit_winner(self, **kwargs: object) -> object | None:
        self.events.append("reload")
        return self.reloads.pop(0)


class PrepareAndPublishWholeDocumentV4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[str] = []
        self.request = cast(
            AtomicPublicationRequestV4,
            SimpleNamespace(
                identity=SimpleNamespace(
                    document_id="doc_1",
                    processing_run_id="run_1",
                    attempt_id="attempt_1",
                )
            ),
        )
        self.winner = cast(AtomicPublicationWinnerV4, object())
        self.checkpoint = cast(
            RemoteParseCheckpointV4,
            SimpleNamespace(document_id="doc_1"),
        )
        self.materialized = cast(MaterializedProviderDocumentV4, object())
        self.claim = cast(V4ClaimWitness, object())
        self.guard = cast(V4ClaimGuard, object())

    def _use_case(self, publisher: _Publisher) -> PrepareAndPublishWholeDocumentV4:
        readiness = _Readiness(self.events)
        return PrepareAndPublishWholeDocumentV4(
            uow_factory=cast(
                Any,
                lambda: cast(UnitOfWork, _Uow(self.events)),
            ),
            publication_requests=cast(
                Any,
                _RequestFactory(self.events, self.request),
            ),
            readiness=cast(Any, readiness),
            publisher=cast(Any, publisher),
        )

    def _execute(self, publisher: _Publisher) -> AtomicPublicationWinnerV4:
        return self._use_case(publisher).execute(
            checkpoint=self.checkpoint,
            materialized=self.materialized,
            claim=self.claim,
            claim_guard=self.guard,
            stage_guard=cast(Any, _StageGuard(self.events)),
        )

    def test_holds_producer_lease_through_postcommit_readiness_verification(self) -> None:
        publisher = _Publisher(self.events, commits=[self.winner])

        self.assertIs(self._execute(publisher), self.winner)
        self.assertEqual(
            self.events,
            [
                "lease-enter",
                "build-or-reopen",
                "stage-checkpoint",
                "prepare",
                "verify-pre",
                "stage-checkpoint",
                "commit",
                "verify-post",
                "lease-exit",
            ],
        )

    def test_lost_response_uses_durable_winner_without_second_commit(self) -> None:
        publisher = _Publisher(
            self.events,
            commits=[AtomicPublicationCommitResponseLost("lost")],
            reloads=[self.winner],
        )

        self.assertIs(self._execute(publisher), self.winner)
        self.assertEqual(self.events.count("commit"), 1)
        self.assertEqual(self.events.count("reload"), 1)
        self.assertEqual(self.events[-2:], ["verify-post", "lease-exit"])

    def test_absent_winner_allows_one_exact_retry(self) -> None:
        publisher = _Publisher(
            self.events,
            commits=[AtomicPublicationCommitResponseLost("lost"), self.winner],
            reloads=[None],
        )

        self.assertIs(self._execute(publisher), self.winner)
        self.assertEqual(self.events.count("commit"), 2)
        self.assertEqual(self.events.count("reload"), 1)
        self.assertEqual(self.events[-2:], ["verify-post", "lease-exit"])

    def test_two_unresolved_response_losses_fail_closed(self) -> None:
        publisher = _Publisher(
            self.events,
            commits=[
                AtomicPublicationCommitResponseLost("lost-one"),
                AtomicPublicationCommitResponseLost("lost-two"),
            ],
            reloads=[None, None],
        )

        with self.assertRaisesRegex(AtomicPublicationCommitResponseLost, "lost-two"):
            self._execute(publisher)
        self.assertEqual(self.events.count("commit"), 2)
        self.assertEqual(self.events.count("reload"), 2)
        self.assertNotIn("verify-post", self.events)
        self.assertEqual(self.events[-1], "lease-exit")

    def test_revocation_blocks_new_commit_but_preserves_winner_reconciliation(self) -> None:
        for phase in ("readiness", "retry-absent", "retry-committed"):
            with self.subTest(phase=phase):
                events: list[str] = []
                stage = StageLeaseGuard(
                    deadline_monotonic=60.0, _revoked=Event(), _monotonic=lambda: 0.0,
                )

                class Readiness(_Readiness):
                    def prepare_or_replay(self, **kwargs: object) -> object:
                        result = super().prepare_or_replay(**kwargs)
                        if phase == "readiness":
                            stage.revoke()
                        return result

                class Publisher(_Publisher):
                    def commit_whole_document(self, *args: object, **kwargs: object) -> object:
                        if phase.startswith("retry"):
                            stage.revoke()
                        return super().commit_whole_document(*args, **kwargs)

                publisher = Publisher(
                    events,
                    commits=[AtomicPublicationCommitResponseLost("lost"), self.winner],
                    reloads=[self.winner if phase == "retry-committed" else None],
                )
                use_case = PrepareAndPublishWholeDocumentV4(
                    uow_factory=cast(Any, lambda: _Uow(events)),
                    publication_requests=cast(Any, _RequestFactory(events, self.request)),
                    readiness=cast(Any, Readiness(events)), publisher=cast(Any, publisher),
                )

                def execute() -> AtomicPublicationWinnerV4:
                    return use_case.execute(
                        checkpoint=self.checkpoint, materialized=self.materialized,
                        claim=self.claim, claim_guard=self.guard, stage_guard=stage,
                    )

                if phase == "retry-committed":
                    self.assertIs(execute(), self.winner)
                    self.assertIn("verify-post", events)
                else:
                    with self.assertRaises(StageLeaseLost):
                        execute()
                    self.assertNotIn("verify-post", events)
                self.assertEqual(events.count("commit"), 0 if phase == "readiness" else 1)
                self.assertEqual(events.count("reload"), 0 if phase == "readiness" else 1)
                self.assertEqual(events[-1], "lease-exit")


if __name__ == "__main__":
    unittest.main()
