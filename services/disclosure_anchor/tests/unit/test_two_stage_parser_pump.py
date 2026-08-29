from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from disclosure_anchor.application.ports.staged_provider_parser import (
    RemoteArtifactReceipt,
)
from disclosure_anchor.application.services.two_stage_parser_pump import (
    BoundedTwoStageParserPump,
    TwoStageParseWork,
)


def _receipt(sequence: int, *, byte_count: int = 1) -> RemoteArtifactReceipt:
    return RemoteArtifactReceipt(
        attempt_identity=f"attempt-{sequence}",
        fence_identity=f"fence-{sequence}",
        artifact_owner_identity=f"artifact-{sequence}",
        artifact_byte_count=byte_count,
    )


class TwoStageParserPumpTests(unittest.TestCase):
    def test_remote_failure_is_acked_only_after_durable_failure_checkpoint(self) -> None:
        events: list[str] = []

        def remote_failure() -> RemoteArtifactReceipt:
            raise RuntimeError("remote failed")

        outcome = BoundedTwoStageParserPump[str](
            remote_workers=1,
            local_workers=1,
            max_terminal_receipts=1,
            max_local_items=1,
            max_local_bytes=1,
        ).run(
            (
                TwoStageParseWork(
                    sequence=0,
                    item_identity="doc-0",
                    wait_remote_terminal=remote_failure,
                    persist_local=lambda _value: "unused",
                    checkpoint_remote_terminal=lambda _value: None,
                    checkpoint_remote_failure=lambda _error: events.append(
                        "failure_committed"
                    ),
                    acknowledge_remote_failure=lambda: events.append("acked"),
                ),
            )
        )[0]
        self.assertEqual(outcome.status, "remote_failed")
        self.assertEqual(events, ["failure_committed", "acked"])

    def test_remote_failure_checkpoint_error_never_acks(self) -> None:
        acked = threading.Event()
        outcome = BoundedTwoStageParserPump[str](
            remote_workers=1,
            local_workers=1,
            max_terminal_receipts=1,
            max_local_items=1,
            max_local_bytes=1,
        ).run(
            (
                TwoStageParseWork(
                    sequence=0,
                    item_identity="doc-0",
                    wait_remote_terminal=lambda: (_ for _ in ()).throw(
                        RuntimeError("remote failed")
                    ),
                    persist_local=lambda _value: "unused",
                    checkpoint_remote_terminal=lambda _value: None,
                    checkpoint_remote_failure=lambda _error: (_ for _ in ()).throw(
                        OSError("checkpoint failed")
                    ),
                    acknowledge_remote_failure=acked.set,
                ),
            )
        )[0]
        self.assertEqual(outcome.status, "remote_failed")
        self.assertRegex(str(outcome.error), "checkpoint failed")
        self.assertFalse(acked.is_set())

    def test_remote_refills_while_first_local_persist_is_blocked(self) -> None:
        first_local_started = threading.Event()
        release_first_local = threading.Event()
        second_remote_started = threading.Event()
        checkpoints: list[str] = []

        def first_local(receipt: RemoteArtifactReceipt) -> str:
            first_local_started.set()
            self.assertTrue(release_first_local.wait(timeout=2))
            return receipt.artifact_owner_identity

        work = (
            TwoStageParseWork(
                sequence=0,
                item_identity="doc-0",
                wait_remote_terminal=lambda: _receipt(0),
                persist_local=first_local,
                checkpoint_remote_terminal=lambda value: checkpoints.append(
                    value.artifact_owner_identity
                ),
            ),
            TwoStageParseWork(
                sequence=1,
                item_identity="doc-1",
                wait_remote_terminal=lambda: (
                    second_remote_started.set() or _receipt(1)
                ),
                persist_local=lambda value: value.artifact_owner_identity,
                checkpoint_remote_terminal=lambda value: checkpoints.append(
                    value.artifact_owner_identity
                ),
            ),
        )
        pump = BoundedTwoStageParserPump[str](
            remote_workers=1,
            local_workers=1,
            max_terminal_receipts=1,
            max_local_items=1,
            max_local_bytes=2,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(pump.run, work)
            self.assertTrue(first_local_started.wait(timeout=2))
            self.assertTrue(second_remote_started.wait(timeout=2))
            self.assertFalse(running.done())
            release_first_local.set()
            outcomes = running.result(timeout=2)

        self.assertEqual([item.status for item in outcomes], ["succeeded"] * 2)
        self.assertEqual(
            [item.result for item in outcomes], ["artifact-0", "artifact-1"]
        )
        self.assertEqual(checkpoints, ["artifact-0", "artifact-1"])

    def test_local_failure_stays_visible_and_does_not_consume_remote_slot(self) -> None:
        second_remote_started = threading.Event()
        work = (
            TwoStageParseWork[str](
                sequence=0,
                item_identity="doc-0",
                wait_remote_terminal=lambda: _receipt(0),
                persist_local=lambda _value: (_ for _ in ()).throw(OSError("disk")),
                checkpoint_remote_terminal=lambda _value: None,
            ),
            TwoStageParseWork(
                sequence=1,
                item_identity="doc-1",
                wait_remote_terminal=lambda: (
                    second_remote_started.set() or _receipt(1)
                ),
                persist_local=lambda value: value.artifact_owner_identity,
                checkpoint_remote_terminal=lambda _value: None,
            ),
        )
        outcomes = BoundedTwoStageParserPump[str](
            remote_workers=1,
            local_workers=1,
            max_terminal_receipts=1,
            max_local_items=1,
            max_local_bytes=1,
        ).run(work)

        self.assertTrue(second_remote_started.is_set())
        self.assertEqual(outcomes[0].status, "local_failed")
        self.assertIsInstance(outcomes[0].error, OSError)
        self.assertEqual(outcomes[1].status, "succeeded")

    def test_local_byte_credit_serializes_materialization(self) -> None:
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()

        def first_local(_receipt: RemoteArtifactReceipt) -> str:
            first_started.set()
            self.assertTrue(release_first.wait(timeout=2))
            return "first"

        def second_local(_receipt: RemoteArtifactReceipt) -> str:
            second_started.set()
            return "second"

        pump = BoundedTwoStageParserPump[str](
            remote_workers=2,
            local_workers=2,
            max_terminal_receipts=2,
            max_local_items=2,
            max_local_bytes=10,
        )
        work = (
            TwoStageParseWork(
                sequence=0,
                item_identity="doc-0",
                wait_remote_terminal=lambda: _receipt(0, byte_count=7),
                persist_local=first_local,
                checkpoint_remote_terminal=lambda _value: None,
            ),
            TwoStageParseWork(
                sequence=1,
                item_identity="doc-1",
                wait_remote_terminal=lambda: _receipt(1, byte_count=7),
                persist_local=second_local,
                checkpoint_remote_terminal=lambda _value: None,
            ),
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(pump.run, work)
            self.assertTrue(first_started.wait(timeout=2))
            time.sleep(0.05)
            self.assertFalse(second_started.is_set())
            release_first.set()
            outcomes = running.result(timeout=2)

        self.assertTrue(second_started.is_set())
        self.assertEqual([item.status for item in outcomes], ["succeeded"] * 2)

    def test_cancel_drains_remote_and_does_not_start_local(self) -> None:
        stop = threading.Event()
        remote_started = threading.Event()
        drained = threading.Event()
        local_started = threading.Event()

        def remote() -> RemoteArtifactReceipt:
            remote_started.set()
            self.assertTrue(drained.wait(timeout=2))
            return _receipt(0)

        work = (
            TwoStageParseWork(
                sequence=0,
                item_identity="doc-0",
                wait_remote_terminal=remote,
                persist_local=lambda _value: local_started.set() or "bad",
                checkpoint_remote_terminal=lambda _value: None,
                cancel_and_drain=drained.set,
            ),
        )
        pump = BoundedTwoStageParserPump[str](
            remote_workers=1,
            local_workers=1,
            max_terminal_receipts=1,
            max_local_items=1,
            max_local_bytes=1,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(pump.run, work, stop_requested=stop.is_set)
            self.assertTrue(remote_started.wait(timeout=2))
            stop.set()
            outcomes = running.result(timeout=2)

        self.assertTrue(drained.is_set())
        self.assertFalse(local_started.is_set())
        self.assertEqual(outcomes[0].status, "cancelled")

    def test_cancel_issues_exactly_one_drain_and_preserves_drain_error(self) -> None:
        stop = threading.Event()
        remote_started = threading.Event()
        release_remote = threading.Event()
        drain_calls = 0
        drain_error = RuntimeError("drain proof failed")

        def remote() -> RemoteArtifactReceipt:
            remote_started.set()
            self.assertTrue(release_remote.wait(timeout=2))
            time.sleep(0.1)
            return _receipt(0)

        def drain() -> None:
            nonlocal drain_calls
            drain_calls += 1
            release_remote.set()
            raise drain_error

        work = (
            TwoStageParseWork(
                sequence=0,
                item_identity="doc-0",
                wait_remote_terminal=remote,
                persist_local=lambda _value: "bad",
                checkpoint_remote_terminal=lambda _value: None,
                cancel_and_drain=drain,
            ),
        )
        pump = BoundedTwoStageParserPump[str](
            remote_workers=1,
            local_workers=1,
            max_terminal_receipts=1,
            max_local_items=1,
            max_local_bytes=1,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(pump.run, work, stop_requested=stop.is_set)
            self.assertTrue(remote_started.wait(timeout=2))
            stop.set()
            outcome = running.result(timeout=2)[0]

        self.assertEqual(drain_calls, 1)
        self.assertEqual(outcome.status, "cancelled")
        self.assertIs(outcome.error, drain_error)

    def test_recovered_terminal_skips_remote_and_resumes_local(self) -> None:
        checkpointed: list[RemoteArtifactReceipt] = []
        receipt = _receipt(0, byte_count=3)
        outcome = BoundedTwoStageParserPump[str](
            remote_workers=1,
            local_workers=1,
            max_terminal_receipts=1,
            max_local_items=1,
            max_local_bytes=3,
        ).run(
            (
                TwoStageParseWork(
                    sequence=0,
                    item_identity="doc-0",
                    wait_remote_terminal=None,
                    recovered_terminal=receipt,
                    persist_local=lambda value: value.artifact_owner_identity,
                    checkpoint_remote_terminal=checkpointed.append,
                ),
            )
        )[0]

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.result, "artifact-0")
        self.assertEqual(checkpointed, [receipt])

    def test_artifact_larger_than_byte_limit_fails_before_local(self) -> None:
        local_started = threading.Event()
        outcome = BoundedTwoStageParserPump[str](
            remote_workers=1,
            local_workers=1,
            max_terminal_receipts=1,
            max_local_items=1,
            max_local_bytes=2,
        ).run(
            (
                TwoStageParseWork(
                    sequence=0,
                    item_identity="doc-0",
                    wait_remote_terminal=lambda: _receipt(0, byte_count=3),
                    persist_local=lambda _value: local_started.set() or "bad",
                    checkpoint_remote_terminal=lambda _value: None,
                ),
            )
        )[0]

        self.assertEqual(outcome.status, "local_failed")
        self.assertFalse(local_started.is_set())


if __name__ == "__main__":
    unittest.main()
