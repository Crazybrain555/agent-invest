"""Independently authored M6 reducer tests (WP-B pure accounting).

Every expected total is derived by hand from the scenario and written next to
the journal; the reducer output is never used as its own oracle. Synthetic
journals only: they validate accounting invariants and certify no PDF,
PostgreSQL row, Windows ownership, hour or throughput.

Tick plan: T0 is the synthetic origin, `at(s)` is `s` seconds after T0 at
10 MHz, the admission deadline is `at(60)`, the close bound `at(90)`, and the
stop-propagation budget is 5 seconds.
"""

from __future__ import annotations

import json
from itertools import permutations
import unittest

from disclosure_anchor.application.contracts.m6_run import M6PublicationMetrics, M6ServiceMetrics

from tests import m6_support as m6


def e2e(entries: dict[str, tuple[int, str]] | None = None, **kwargs: object) -> m6.RunFixture:
    return m6.make_fixture("e2e_publication", entries or {"a": (7, "fresh")}, **kwargs)


def service(entries: dict[str, tuple[int, str]] | None = None, **kwargs: object) -> m6.RunFixture:
    return m6.make_fixture("service_diagnostic", entries or {"s": (4, "fresh")}, **kwargs)


def opened(fixture: m6.RunFixture, *, open_at: float = 1.0, **journal_kwargs: object) -> m6.Journal:
    journal = m6.Journal(fixture, **journal_kwargs)  # type: ignore[arg-type]
    journal.start()
    journal.opened(journal.at(open_at))
    return journal


class GoldenAndWindowTests(unittest.TestCase):
    def test_golden_full_source_whole_run_and_deterministic_replay(self) -> None:
        # a: 7 fresh pages, everything before the deadline -> window credit.
        # b: 5 fresh pages, committed/confirmed early, qualified at 61 s -> whole-run only.
        # c: 3 replay pages, fully published -> labelled replay, zero credit.
        fixture = e2e({"a": (7, "fresh"), "b": (5, "fresh"), "c": (3, "replay")})
        a, b, c = fixture.entries["a"], fixture.entries["b"], fixture.entries["c"]
        j = opened(fixture)
        admit_a = j.admit(a, "att-a", j.at(2))
        admit_b = j.admit(b, "att-b", j.at(3))
        admit_c = j.admit(c, "att-c", j.at(4))
        j.accept("att-a", j.at(5))
        j.accept("att-b", j.at(6))
        j.accept("att-c", j.at(7))
        j.commit(admit_a, j.at(10), ledger_seq=101)
        j.confirm(admit_a, j.at(12), ledger_seq=101)
        proof_a = m6.qualification_for(a, "e2e_publication", "att-a")
        j.qualify("att-a", proof_a, j.at(13))
        j.commit(admit_b, j.at(20), ledger_seq=102)
        j.confirm(admit_b, j.at(22), ledger_seq=102)
        j.commit(admit_c, j.at(25), ledger_seq=103)
        j.confirm(admit_c, j.at(26), ledger_seq=103)
        proof_c = m6.qualification_for(c, "e2e_publication", "att-c")
        j.qualify("att-c", proof_c, j.at(27))
        j.final("att-a", j.at(30))
        j.final("att-c", j.at(31))
        j.stop_requested(j.at(60))
        proof_b = m6.qualification_for(b, "e2e_publication", "att-b")
        j.qualify("att-b", proof_b, j.at(61))
        j.final("att-b", j.at(62))
        j.stop_effective(j.at(63))
        j.drained(j.at(64))
        j.resources_closed(j.at(65))
        j.closed(j.at(66))
        history = (
            m6.history_fact(a, attempt_id="att-a", ledger_seq=101),
            m6.history_fact(b, attempt_id="att-b", ledger_seq=102),
            m6.history_fact(c, attempt_id="att-c", ledger_seq=103),
        )
        proofs = (proof_a, proof_b, proof_c)

        receipt = m6.reduce(fixture, j, history=history, qualifications=proofs)

        self.assertEqual(receipt.status, "complete", receipt)
        self.assertEqual(receipt.metrics, M6PublicationMetrics(window_pages=7, whole_run_pages=12, carry_in_pages=0))
        self.assertEqual(m6.outcomes(receipt), {
            "att-a": "credited_window", "att-b": "credited_whole_run_only", "att-c": "replay",
        })
        by_attempt = {line.attempt_id: line for line in receipt.sources}
        self.assertEqual(by_attempt["att-a"].ready_received_ticks, j.at(13), "credit time is the latest receipt")
        self.assertEqual(by_attempt["att-b"].ready_received_ticks, j.at(61))
        self.assertEqual(by_attempt["att-a"].admission_received_ticks, j.at(2))
        self.assertEqual({line.attempt_id: line.page_count for line in receipt.sources},
                         {"att-a": 7, "att-b": 5, "att-c": 3})
        self.assertEqual(receipt.tclose_ticks, j.at(66))
        self.assertEqual(receipt.elapsed_ticks, 66 * m6.QPC_HZ)
        self.assertEqual((receipt.t0_ticks, receipt.deadline_ticks), (m6.T0, fixture.deadline))
        self.assertEqual((receipt.stop_requested_ticks, receipt.stop_effective_ticks), (j.at(60), j.at(63)))
        self.assertEqual(receipt.close_reason, "deadline_drained")
        self.assertEqual(receipt.events_total, len(j.records))
        self.assertEqual(receipt.duplicate_events, 0)
        self.assertEqual(receipt.journal_prefix_sha256, m6.sha256_of(b"".join(j.lines())))
        self.assertEqual(receipt.journal_bytes_consumed, sum(len(line) for line in j.lines()))
        self.assertEqual(receipt.spec_sha256, fixture.spec.canonical_sha256())
        self.assertEqual((receipt.mode, receipt.phase, receipt.qpc_frequency_hz),
                         ("e2e_publication", "short_batch", m6.QPC_HZ))

        replayed = m6.reduce(fixture, tuple(j.lines()), history=history[::-1], qualifications=proofs[::-1])
        self.assertEqual(replayed, receipt, "identical evidence replays to identical bytes")
        self.assertEqual(replayed.canonical_bytes(), receipt.canonical_bytes())

    def test_half_open_window_and_late_quality_cannot_backfill(self) -> None:
        # d: confirmation lands exactly on the deadline tick -> outside [T0, deadline).
        # e: last fact at 59 s -> window. f: early confirmation, commit observed at 61 s -> whole-run only.
        fixture = e2e({"d": (4, "fresh"), "e": (6, "fresh"), "f": (2, "fresh")})
        d, e_, f = fixture.entries["d"], fixture.entries["e"], fixture.entries["f"]
        j = opened(fixture)
        admit_d = j.admit(d, "att-d", j.at(2))
        j.accept("att-d", j.at(3))
        j.commit(admit_d, j.at(4), ledger_seq=1)
        proof_d = m6.qualification_for(d, "e2e_publication", "att-d")
        j.qualify("att-d", proof_d, j.at(5))
        admit_e = j.admit(e_, "att-e", j.at(6))
        j.accept("att-e", j.at(7))
        j.commit(admit_e, j.at(8), ledger_seq=2)
        j.confirm(admit_e, j.at(9), ledger_seq=2)
        admit_f = j.admit(f, "att-f", j.at(10))
        j.accept("att-f", j.at(11))
        j.confirm(admit_f, j.at(12), ledger_seq=3)
        proof_f = m6.qualification_for(f, "e2e_publication", "att-f")
        j.qualify("att-f", proof_f, j.at(13))
        proof_e = m6.qualification_for(e_, "e2e_publication", "att-e")
        j.qualify("att-e", proof_e, j.at(59))
        j.stop_requested(j.at(60))
        j.confirm(admit_d, j.at(60), ledger_seq=1)
        j.commit(admit_f, j.at(61), ledger_seq=3)
        j.final("att-d", j.at(62))
        j.final("att-e", j.at(62))
        j.final("att-f", j.at(62))
        j.stop_effective(j.at(63))
        j.drained(j.at(64))
        j.resources_closed(j.at(65))
        j.closed(j.at(66))
        history = (m6.history_fact(d, attempt_id="att-d", ledger_seq=1),
                   m6.history_fact(e_, attempt_id="att-e", ledger_seq=2),
                   m6.history_fact(f, attempt_id="att-f", ledger_seq=3))

        receipt = m6.reduce(fixture, j, history=history, qualifications=(proof_d, proof_e, proof_f))

        self.assertEqual(receipt.status, "complete", receipt)
        self.assertEqual(m6.outcomes(receipt), {
            "att-d": "credited_whole_run_only", "att-e": "credited_window", "att-f": "credited_whole_run_only",
        })
        self.assertEqual(receipt.metrics, M6PublicationMetrics(window_pages=6, whole_run_pages=12, carry_in_pages=0))
        by_attempt = {line.attempt_id: line for line in receipt.sources}
        self.assertEqual(by_attempt["att-d"].ready_received_ticks, fixture.deadline)
        self.assertEqual(by_attempt["att-f"].ready_received_ticks, j.at(61))

        # The independently bound facts may arrive in any physical order. With
        # seven original pages, only the last fact's tick can decide window
        # membership; exactly-at-deadline and one tick later never backfill.
        single = e2e()
        source = single.entries["a"]
        proof = m6.qualification_for(source, "e2e_publication", "att-a")
        history = (m6.history_fact(source, attempt_id="att-a", ledger_seq=1),)
        for order in permutations(("commit", "confirm", "qualify")):
            for offset, expected_window in ((-1, 7), (0, 0), (1, 0)):
                with self.subTest(arrival_order=order, deadline_tick_offset=offset):
                    journal = opened(single)
                    admission = journal.admit(source, "att-a", journal.at(2))
                    journal.accept("att-a", journal.at(3))
                    append_fact = {
                        "commit": lambda tick: journal.commit(admission, tick, ledger_seq=1),
                        "confirm": lambda tick: journal.confirm(admission, tick, ledger_seq=1),
                        "qualify": lambda tick: journal.qualify("att-a", proof, tick),
                    }
                    for kind, tick in zip(order, (journal.at(10), journal.at(20), single.deadline + offset), strict=True):
                        append_fact[kind](tick)
                    journal.final("att-a", journal.at(61))
                    journal.close_run(stop_at=61)
                    receipt = m6.reduce(single, journal, history=history, qualifications=(proof,))
                    self.assertEqual(receipt.status, "complete", receipt)
                    self.assertEqual(receipt.metrics, M6PublicationMetrics(
                        window_pages=expected_window, whole_run_pages=7, carry_in_pages=0))
                    self.assertEqual(receipt.sources[0].ready_received_ticks, single.deadline + offset)


class ClosureTests(unittest.TestCase):
    def test_complete_zero_is_distinct_from_missing_cleanup(self) -> None:
        fixture = e2e()
        j = opened(fixture)
        j.close_run(stop_at=60)
        receipt = m6.reduce(fixture, j)
        self.assertEqual(receipt.status, "complete", receipt)
        self.assertEqual(receipt.metrics, M6PublicationMetrics(window_pages=0, whole_run_pages=0, carry_in_pages=0))
        self.assertEqual(receipt.sources, ())
        self.assertEqual(receipt.elapsed_ticks, 64 * m6.QPC_HZ)

        def journal(skip: str = "", *, residual: int = 0, children: bool = True) -> m6.Journal:
            j = opened(fixture)
            steps = {
                "stop_requested": lambda: j.stop_requested(j.at(60)),
                "stop_effective": lambda: j.stop_effective(j.at(61)),
                "drained": lambda: j.drained(j.at(62)),
                "resources_closed": lambda: j.resources_closed(j.at(63), residual=residual, children_exited=children),
                "closed": lambda: j.closed(j.at(64)),
            }
            for name, step in steps.items():
                if name != skip:
                    step()
            return j

        self.assertEqual(m6.reduce(fixture, journal("stop_requested")).status, "complete",
                         "a stop request is optional metadata; effective stop is the terminal fact")
        for missing in ("stop_effective", "drained", "resources_closed", "closed"):
            receipt = m6.reduce(fixture, journal(missing))
            self.assertEqual(receipt.status, "incomplete", missing)
            self.assertIsNone(receipt.metrics, missing)
        for label, kwargs in {"residual": {"residual": 1}, "children": {"children": False}}.items():
            receipt = m6.reduce(fixture, journal(**kwargs))  # type: ignore[arg-type]
            self.assertEqual(receipt.status, "incomplete", label)
            self.assertIsNone(receipt.metrics, label)
            self.assertEqual(receipt.tclose_ticks, j.at(64), "raw closure evidence stays visible")

        # A terminal residual report is not replaced by a second resources_closed claiming success.
        j = opened(fixture)
        j.stop_requested(j.at(60))
        j.stop_effective(j.at(61))
        j.drained(j.at(62))
        j.resources_closed(j.at(63), residual=2)
        j.resources_closed(j.at(63))
        j.closed(j.at(64))
        self.assertEqual(m6.reduce(fixture, j).status, "invalid")

        # Drain reported before admission stopped, or a close before the stop, is not a closure.
        j = opened(fixture)
        j.drained(j.at(30))
        j.stop_requested(j.at(60))
        j.stop_effective(j.at(61))
        j.resources_closed(j.at(63))
        j.closed(j.at(64))
        self.assertNotEqual(m6.reduce(fixture, j).status, "complete")

    def test_late_evidence_after_claimed_drain_cannot_leave_complete_receipt(self) -> None:
        fixture = e2e({"a": (7, "fresh"), "b": (3, "fresh")})
        a, b = fixture.entries["a"], fixture.entries["b"]

        def drained_journal() -> tuple[m6.Journal, m6.Journal]:
            j = opened(fixture)
            _, proof, _ = m6.e2e_publish(j, a, "att-a", admit_at=2, ledger_seq=1)
            j.stop_requested(j.at(60))
            j.stop_effective(j.at(61))
            j.drained(j.at(62))
            return j, proof  # type: ignore[return-value]

        j, _ = drained_journal()
        j.qualify("att-a", m6.qualification_for(a, "e2e_publication", "att-a", unit_count=99), j.at(63))
        j.resources_closed(j.at(64))
        j.closed(j.at(65))
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "attempt evidence after drain")

        j, _ = drained_journal()
        j.admit(b, "att-b", j.at(63))
        j.final("att-b", j.at(63), outcome="failed", disposition="not_submitted")
        j.resources_closed(j.at(64))
        j.closed(j.at(65))
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "admission after drain")

        j, _ = drained_journal()
        j.resources_closed(j.at(64))
        j.accept("att-a", j.at(64))
        j.closed(j.at(65))
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "evidence after resource closure")

        # Ordinary pending drain (an attempt still open at drain time) is incomplete, not an incident.
        j = opened(fixture)
        j.admit(b, "att-b", j.at(2))
        j.stop_requested(j.at(60))
        j.stop_effective(j.at(61))
        j.drained(j.at(62))
        j.resources_closed(j.at(64))
        j.closed(j.at(65))
        receipt = m6.reduce(fixture, j)
        self.assertEqual(receipt.status, "incomplete")
        self.assertIsNone(receipt.metrics)

    def test_stop_propagation_admission_is_excluded_but_cost_and_cleanup_remain(self) -> None:
        fixture = e2e()
        a = fixture.entries["a"]

        def late_admission(*, admit_at: float, effective_at: float, with_final: bool = True) -> m6.Journal:
            j = opened(fixture)
            j.stop_requested(j.at(60))
            admission = j.admit(a, "att-late", j.at(admit_at))
            j.accept("att-late", j.at(admit_at + 0.5))
            j.stop_effective(j.at(effective_at))
            j.commit(admission, j.at(effective_at + 0.5), ledger_seq=1)
            j.confirm(admission, j.at(effective_at + 1), ledger_seq=1)
            j.qualify("att-late", m6.qualification_for(a, "e2e_publication", "att-late"), j.at(effective_at + 1))
            if with_final:
                j.final("att-late", j.at(effective_at + 2))
            j.drained(j.at(effective_at + 3))
            j.resources_closed(j.at(effective_at + 4))
            j.closed(j.at(effective_at + 5))
            return j

        proof = m6.qualification_for(a, "e2e_publication", "att-late")
        history = (m6.history_fact(a, attempt_id="att-late", ledger_seq=1),)
        receipt = m6.reduce(fixture, late_admission(admit_at=61, effective_at=63), history=history, qualifications=(proof,))
        self.assertEqual(receipt.status, "complete", receipt)
        self.assertEqual(m6.outcomes(receipt), {"att-late": "admitted_after_deadline"})
        self.assertEqual(receipt.metrics, M6PublicationMetrics(window_pages=0, whole_run_pages=0, carry_in_pages=0))
        self.assertEqual(receipt.sources[0].admission_received_ticks, fixture.at(61))
        self.assertEqual(receipt.sources[0].page_count, 7, "the source's pages are visible but not credited")
        self.assertEqual((receipt.stop_requested_ticks, receipt.stop_effective_ticks), (fixture.at(60), fixture.at(63)))
        self.assertEqual(receipt.elapsed_ticks, 68 * m6.QPC_HZ, "late processing cost stays in whole-run")

        unclosed = m6.reduce(fixture, late_admission(admit_at=61, effective_at=63, with_final=False),
                             history=history, qualifications=(proof,))
        self.assertEqual(unclosed.status, "incomplete", "a late attempt still owes its closure")

        beyond_budget = m6.reduce(fixture, late_admission(admit_at=66, effective_at=67),
                                  history=history, qualifications=(proof,))
        self.assertEqual(beyond_budget.status, "incomplete", "admission/effective stop past the 5 s budget")
        self.assertIsNone(beyond_budget.metrics)

    def test_fresh_admission_after_effective_stop_is_invalid(self) -> None:
        fixture = e2e()
        a = fixture.entries["a"]
        j = opened(fixture)
        j.stop_requested(j.at(30))
        j.stop_effective(j.at(31))
        j.admit(a, "att-a", j.at(32))
        j.final("att-a", j.at(33), outcome="failed", disposition="not_submitted")
        j.drained(j.at(34))
        j.resources_closed(j.at(35))
        j.closed(j.at(36))
        self.assertEqual(m6.reduce(fixture, j).status, "invalid")

        j = m6.Journal(fixture)
        j.start()
        j.admit(a, "att-a", j.at(1))
        j.opened(j.at(2))
        j.final("att-a", j.at(3), outcome="failed", disposition="not_submitted")
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "admission before admission_opened")

        j = m6.Journal(fixture)
        j.start()
        j.opened(j.at(60))
        j.close_run(stop_at=61)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "admission opened at the deadline")

        j = opened(fixture)
        j.stop_requested(j.at(30))
        j.stop_effective(j.at(31))
        j.stop_effective(j.at(32))
        j.drained(j.at(34))
        j.resources_closed(j.at(35))
        j.closed(j.at(36))
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "stop_admission_effective is one terminal fact")

    def test_stop_metadata_distinguishes_early_stop_from_full_supply(self) -> None:
        fixture = e2e()
        a = fixture.entries["a"]
        j = opened(fixture)
        _, proof, fact = m6.e2e_publish(j, a, "att-a", admit_at=2, ledger_seq=1)
        j.stop_requested(j.at(20))
        j.stop_effective(j.at(21))
        j.drained(j.at(22))
        j.resources_closed(j.at(23))
        j.closed(j.at(24), reason="stop_requested")
        early = m6.reduce(fixture, j, history=(fact,), qualifications=(proof,))
        self.assertEqual(early.status, "complete", early)
        self.assertEqual(early.metrics, M6PublicationMetrics(window_pages=7, whole_run_pages=7, carry_in_pages=0))
        self.assertEqual(early.close_reason, "stop_requested")
        self.assertEqual(early.stop_requested_ticks, j.at(20))
        self.assertLess(early.stop_requested_ticks or 0, early.deadline_ticks)
        self.assertLess(early.tclose_ticks or 0, early.deadline_ticks)

        j = opened(fixture)
        _, proof, fact = m6.e2e_publish(j, a, "att-a", admit_at=2, ledger_seq=1)
        j.close_run(stop_at=60)
        full = m6.reduce(fixture, j, history=(fact,), qualifications=(proof,))
        self.assertEqual(full.close_reason, "deadline_drained")
        self.assertEqual(full.stop_requested_ticks, full.deadline_ticks)
        self.assertGreaterEqual(full.tclose_ticks or 0, full.deadline_ticks)
        self.assertNotEqual((early.close_reason, early.stop_requested_ticks), (full.close_reason, full.stop_requested_ticks))

        formal = e2e(phase="hour_baseline", planned_seconds=3600)
        j = opened(formal)
        _, proof, fact = m6.e2e_publish(j, formal.entries["a"], "att-a", admit_at=2, ledger_seq=1)
        j.stop_requested(j.at(20))
        j.stop_effective(j.at(21))
        j.drained(j.at(22))
        j.resources_closed(j.at(23))
        j.closed(j.at(24), reason="stop_requested")
        stopped_formal = m6.reduce(formal, j, history=(fact,), qualifications=(proof,))
        self.assertEqual(stopped_formal.status, "incomplete", "a formal interval closed early is not covered")
        self.assertIsNone(stopped_formal.metrics)

        j = opened(fixture)
        j.stop_requested(j.at(60))
        j.stop_effective(j.at(61))
        j.drained(j.at(62))
        j.resources_closed(j.at(63))
        j.closed(j.at(64), reason="failed")
        failed = m6.reduce(fixture, j)
        self.assertEqual(failed.status, "incomplete", "a failed close never carries a trusted numerator")
        self.assertEqual(failed.close_reason, "failed")

    def test_deep_json_and_zero_elapsed_produce_fail_closed_receipts(self) -> None:
        fixture = e2e()
        j = opened(fixture)
        j.close_run(stop_at=60)
        lines = list(j.lines())
        deep = b"[" * 4000 + b"]" * 4000 + b"\n"
        self.assertLessEqual(len(deep), fixture.spec.resources.max_record_bytes + 1)
        lines.insert(2, deep)
        receipt = m6.reduce(fixture, tuple(lines))
        self.assertEqual(receipt.status, "invalid")
        self.assertIsNone(receipt.metrics)
        self.assertEqual(receipt.events_total, len(lines), "a malformed record is consumed and counted")
        self.assertEqual(receipt.journal_prefix_sha256, m6.sha256_of(b"".join(lines)))
        self.assertEqual(receipt.tclose_ticks, j.at(64), "records after the malformed one are still read")

        nested_object = b'{"contract_version":"m6.run-event.v1","event":' + b"[" * 3000 + b"]" * 3000 + b"}\n"
        receipt = m6.reduce(fixture, tuple(lines[:2]) + (nested_object,) + tuple(lines[3:]))
        self.assertEqual(receipt.status, "invalid")

        j = m6.Journal(fixture)
        j.start()
        j.closed(fixture.spec.t0_ticks)
        zero = m6.reduce(fixture, j)
        self.assertEqual(zero.status, "invalid")
        self.assertIsNone(zero.metrics)

        j = opened(fixture)
        j.stop_requested(j.at(60))
        j.stop_effective(j.at(61))
        j.drained(j.at(62))
        j.resources_closed(j.at(63))
        j.closed(j.at(64), tclose=j.at(63))
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "tclose must be the owner receipt tick")

        j = opened(fixture)
        j.stop_requested(j.at(60))
        j.stop_effective(j.at(61))
        j.drained(j.at(62))
        j.resources_closed(j.at(63))
        j.closed(j.at(91))
        over = m6.reduce(fixture, j)
        self.assertEqual(over.status, "incomplete", "close after max_close_ticks exceeds the close budget")
        self.assertEqual(over.tclose_ticks, j.at(91))


class HistoryAndIdentityTests(unittest.TestCase):
    def test_global_history_is_not_reset_by_new_run_or_profile(self) -> None:
        fixture = e2e()
        a = fixture.entries["a"]

        def run() -> tuple[m6.Journal, object]:
            j = opened(fixture)
            _, proof, _ = m6.e2e_publish(j, a, "att-a", admit_at=2, ledger_seq=500)
            j.close_run(stop_at=60)
            return j, proof

        j, proof = run()
        older = m6.history_fact(a, attempt_id="att-a", ledger_seq=17, first_run="prun-historical")
        receipt = m6.reduce(fixture, j, history=(older,), qualifications=(proof,))  # type: ignore[arg-type]
        self.assertEqual(receipt.status, "complete", receipt)
        self.assertEqual(m6.outcomes(receipt), {"att-a": "not_first_publish"})
        self.assertEqual(receipt.metrics, M6PublicationMetrics(window_pages=0, whole_run_pages=0, carry_in_pages=0))

        for run_id, profile, attempt in (("run-2", "profile-1", "retry-1"),
                                        ("run-3", "profile-2", "retry-2")):
            with self.subTest(run_id=run_id, process_profile=profile, attempt_id=attempt):
                another = e2e(run_id=run_id, profile_label=profile)
                journal = opened(another)
                _, another_proof, _ = m6.e2e_publish(journal, another.entries["a"], attempt, admit_at=2, ledger_seq=501)
                journal.close_run(stop_at=60)
                historical = m6.history_fact(another.entries["a"], attempt_id=attempt, ledger_seq=17,
                                             first_run="prun-historical")
                replay = m6.reduce(another, journal, history=(historical,), qualifications=(another_proof,))
                self.assertEqual(replay.status, "complete", replay)
                self.assertEqual(m6.outcomes(replay), {attempt: "not_first_publish"})
                self.assertEqual(replay.metrics, M6PublicationMetrics(
                    window_pages=0, whole_run_pages=0, carry_in_pages=0))

        same_run_later_ledger = m6.history_fact(a, attempt_id="att-a", ledger_seq=499)
        receipt = m6.reduce(fixture, j, history=(same_run_later_ledger,), qualifications=(proof,))  # type: ignore[arg-type]
        self.assertEqual(m6.outcomes(receipt), {"att-a": "not_first_publish"})

        unknown = m6.unknown_history(a, attempt_id="att-a")
        receipt = m6.reduce(fixture, j, history=(unknown,), qualifications=(proof,))  # type: ignore[arg-type]
        self.assertEqual(m6.outcomes(receipt), {"att-a": "novelty_unverified"})
        self.assertTrue(receipt.metrics is None or receipt.metrics.whole_run_pages == 0)

        receipt = m6.reduce(fixture, j, history=(), qualifications=(proof,))  # type: ignore[arg-type]
        self.assertEqual(m6.outcomes(receipt), {"att-a": "novelty_unverified"}, "absence is not novelty")
        self.assertTrue(receipt.metrics is None or receipt.metrics.whole_run_pages == 0)

        genuine = m6.history_fact(a, attempt_id="att-a", ledger_seq=500)
        receipt = m6.reduce(fixture, j, history=(genuine,), qualifications=(proof,))  # type: ignore[arg-type]
        self.assertEqual(receipt.metrics, M6PublicationMetrics(window_pages=7, whole_run_pages=7, carry_in_pages=0))

    def test_conflicting_page_counts_are_incomplete_not_partial_good_pages(self) -> None:
        fixture = e2e({"a": (7, "fresh"), "g": (2, "fresh")})
        a, g = fixture.entries["a"], fixture.entries["g"]

        def run(**commit_overrides: object) -> tuple[m6.Journal, tuple[object, ...]]:
            j = opened(fixture)
            admission = j.admit(a, "att-a", j.at(2))
            j.accept("att-a", j.at(3))
            j.commit(admission, j.at(4), ledger_seq=1, **commit_overrides)
            j.confirm(admission, j.at(5), ledger_seq=1)
            proof_a = m6.qualification_for(a, "e2e_publication", "att-a")
            j.qualify("att-a", proof_a, j.at(6))
            j.final("att-a", j.at(7))
            _, proof_g, fact_g = m6.e2e_publish(j, g, "att-g", admit_at=10, ledger_seq=2)
            j.close_run(stop_at=60)
            return j, (proof_a, proof_g, fact_g)

        j, (proof_a, proof_g, fact_g) = run(source_page_count=6)
        receipt = m6.reduce(fixture, j, history=(m6.history_fact(a, attempt_id="att-a", ledger_seq=1), fact_g),  # type: ignore[arg-type]
                            qualifications=(proof_a, proof_g))  # type: ignore[arg-type]
        self.assertEqual(receipt.status, "incomplete", "a source with conflicting page counts poisons the measurement")
        self.assertIsNone(receipt.metrics, "the good source g does not become a partial numerator")
        self.assertEqual(m6.outcomes(receipt)["att-a"], "page_count_conflict")

        j, (proof_a, proof_g, fact_g) = run()
        for label, fact in {
            "variants": m6.history_fact(a, attempt_id="att-a", ledger_seq=1, variants=2),
            "first_pages": m6.history_fact(a, attempt_id="att-a", ledger_seq=1, first_pages=8),
        }.items():
            receipt = m6.reduce(fixture, j, history=(fact, fact_g), qualifications=(proof_a, proof_g))  # type: ignore[arg-type]
            self.assertEqual(receipt.status, "incomplete", label)
            self.assertEqual(m6.outcomes(receipt)["att-a"], "page_count_conflict", label)

        j = opened(fixture)
        j.admit(a, "att-a", j.at(2), source_page_count=8)
        j.final("att-a", j.at(3), outcome="failed", disposition="not_submitted")
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "admission disagreeing with the manifest")

        j = opened(fixture)
        j.admit(a, "att-a", j.at(2), source_pdf_sha256=m6.digest("source-pdf:unknown"))
        j.final("att-a", j.at(3), outcome="failed", disposition="not_submitted")
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "admission of a source outside the manifest")

    def test_source_winner_public_bytes_and_history_must_refer_to_same_publication(self) -> None:
        fixture = e2e()
        a = fixture.entries["a"]

        def run(*, confirm_overrides: dict[str, object] | None = None, commit_overrides: dict[str, object] | None = None,
                proof_overrides: dict[str, object] | None = None, history_audit: str | None = None) -> object:
            j = opened(fixture)
            admission = j.admit(a, "att-a", j.at(2))
            j.accept("att-a", j.at(3))
            j.commit(admission, j.at(4), **{"ledger_seq": 1, **(commit_overrides or {})})
            j.confirm(admission, j.at(5), **{"ledger_seq": 1, **(confirm_overrides or {})})
            proof = m6.qualification_for(a, "e2e_publication", "att-a", **(proof_overrides or {}))
            j.qualify("att-a", proof, j.at(6))
            j.final("att-a", j.at(7))
            j.close_run(stop_at=60)
            fact = m6.history_fact(a, attempt_id="att-a", ledger_seq=1, audit=history_audit)
            return m6.reduce(fixture, j, history=(fact,), qualifications=(proof,))

        good = run()
        self.assertEqual(good.status, "complete", good)  # type: ignore[attr-defined]
        cases = {
            "winner": run(confirm_overrides={"winner_sha256": m6.digest("winner:other")}),
            "durable_base": run(confirm_overrides={"durable_base_sha256": m6.digest("base:other")}),
            "ledger_seq": run(confirm_overrides={"ledger_seq": 2}),
            "document_alias": run(commit_overrides={"document_id": "doc-alias"}),
            "processing_run": run(commit_overrides={"processing_run_id": "prun-other"}),
            "history_audit": run(history_audit=m6.digest("history-audit:other")),
            "public_units": run(proof_overrides={"public_units_sha256": m6.digest("public-units:other")}),
        }
        for label, receipt in cases.items():
            self.assertEqual(receipt.status, "invalid", label)  # type: ignore[attr-defined]
            self.assertIsNone(receipt.metrics, label)  # type: ignore[attr-defined]

    def test_scope_profile_mode_and_ack_identity_fail_closed(self) -> None:
        fixture = e2e({"a": (7, "fresh"), "b": (3, "fresh")})
        a = fixture.entries["a"]
        j = opened(fixture)
        j.close_run(stop_at=60)
        lines = j.lines()

        other_manifest = m6.manifest("e2e_publication", a)
        other_plan = m6.quality_plan("e2e_publication", ("x", "review_required"))
        wrong_scope = fixture.spec.model_copy(update={
            "scope_sha256": m6.digest("scope:other")})
        for label, kwargs in {
            "manifest": {"manifest": other_manifest},
            "plan": {"quality_plan": other_plan},
            "scope": {"spec": wrong_scope},
        }.items():
            with self.assertRaises(ValueError, msg=label):
                m6.reduce_m6_run(**{  # type: ignore[arg-type]
                    "spec": fixture.spec, "manifest": fixture.manifest, "quality_plan": fixture.plan,
                    "journal_lines": lines, "history": (), "qualifications": (), **kwargs})

        def closed_after(build: object) -> m6.Journal:
            j = opened(fixture)
            build(j)  # type: ignore[operator]
            j.close_run(stop_at=60)
            return j

        def profile_drift(j: m6.Journal) -> None:
            j.admit(a, "att-a", j.at(2), process_profile_sha256=m6.digest("process-profile:other"))
            j.final("att-a", j.at(3), outcome="failed", disposition="not_submitted")

        def mode_drift(j: m6.Journal) -> None:
            admission = j.admission(a, "att-a").model_copy(update={"processing_run_id": None})
            self.assertIsNone(admission.processing_run_id, "the intended mode drift must reach the reducer")
            j.runner(admission, j.at(2))
            j.final("att-a", j.at(3), outcome="failed", disposition="not_submitted")

        def discarded_remote(j: m6.Journal) -> None:
            j.admit(a, "att-a", j.at(2))
            j.accept("att-a", j.at(3))
            j.final("att-a", j.at(4), outcome="failed", disposition="not_submitted")

        def task_mismatch(j: m6.Journal) -> None:
            j.admit(a, "att-a", j.at(2))
            j.accept("att-a", j.at(3))
            j.final("att-a", j.at(4), outcome="failed", remote_task_identity_sha256=m6.digest("task:other"))

        def service_outcome_in_e2e(j: m6.Journal) -> None:
            j.admit(a, "att-a", j.at(2))
            j.accept("att-a", j.at(3))
            j.final("att-a", j.at(4), outcome="diagnostic_disposed")

        def wrong_producer_role(j: m6.Journal) -> None:
            admission = j.admit(a, "att-a", j.at(2))
            j.accept("att-a", j.at(3))
            j.public(m6.M6PublicationCommitted(  # a verifier cannot report the runner's commit
                attempt_id="att-a", processing_run_id=admission.processing_run_id or "x",
                document_id=admission.document_id or "x", source_pdf_sha256=a.source_pdf_sha256,
                source_page_count=7, ledger_seq=1, winner_sha256=m6.digest("w"),
                durable_base_sha256=m6.digest("b")), j.at(4))
            j.final("att-a", j.at(5), outcome="failed")

        for label, build in {
            "profile_drift": profile_drift, "mode_drift": mode_drift, "discarded_remote": discarded_remote,
            "task_mismatch": task_mismatch, "service_outcome_in_e2e": service_outcome_in_e2e,
            "wrong_producer_role": wrong_producer_role,
        }.items():
            receipt = m6.reduce(fixture, closed_after(build))
            self.assertEqual(receipt.status, "invalid", label)
            self.assertIsNone(receipt.metrics, label)

        j = opened(fixture)
        j.admit(a, "att-a", j.at(2))
        j.accept("att-a", j.at(3))
        j.final("att-a", j.at(4), outcome="published")
        j.close_run(stop_at=60)
        receipt = m6.reduce(fixture, j)
        self.assertEqual(receipt.status, "complete", "published without remote issues is well-formed")
        self.assertEqual(m6.outcomes(receipt), {"att-a": "qualification_missing"})

        j = opened(fixture)
        j.admit(a, "att-a", j.at(2))
        j.final("att-a", j.at(4), outcome="published")
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "incomplete", "published without a bound remote acceptance")

        # A record from another run or spec is never accounted to this run.
        j = opened(fixture)
        foreign = j.event("owner", m6.OWNER_EPOCH, m6.M6AdmissionControl(kind="stop_admission_requested"), run_id="run-9")
        j.stamp(foreign, j.at(30))
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid")


class JournalIntegrityTests(unittest.TestCase):
    def _published(self, fixture: m6.RunFixture) -> tuple[m6.Journal, tuple[object, ...], tuple[object, ...]]:
        j = opened(fixture)
        _, proof, fact = m6.e2e_publish(j, fixture.entries["a"], "att-a", admit_at=2, ledger_seq=1)
        j.close_run(stop_at=60)
        return j, (proof,), (fact,)

    def test_duplicate_records_and_producer_retries_credit_once(self) -> None:
        fixture = e2e()
        j = opened(fixture)
        admission, proof, fact = m6.e2e_publish(j, fixture.entries["a"], "att-a", admit_at=2, ledger_seq=1)
        confirmation = next(r for r in j.records if r.event.payload.kind == "public_confirmation")
        j.records.append(confirmation)  # exact durable record replayed by the reader
        commit = next(r for r in j.records if r.event.payload.kind == "publication_committed")
        j.stamp(commit.event, j.at(20))  # producer retry: same incarnation/sequence/bytes, new owner stamp
        j.close_run(stop_at=60)
        closed = j.records[-1]
        j.records.append(closed)  # exact owner record replay after close is still not new evidence

        receipt = m6.reduce(fixture, j, history=(fact,), qualifications=(proof,))
        self.assertEqual(receipt.status, "complete", receipt)
        self.assertEqual(receipt.duplicate_events, 3)
        self.assertEqual(receipt.events_total, len(j.records))
        self.assertEqual(receipt.metrics, M6PublicationMetrics(window_pages=7, whole_run_pages=7, carry_in_pages=0))
        self.assertEqual(len(receipt.sources), 1)

    def test_owner_journal_reorder_gap_and_clock_regression_are_visible(self) -> None:
        fixture = e2e()
        j, proofs, facts = self._published(fixture)
        clean = m6.reduce(fixture, j, history=facts, qualifications=proofs)  # type: ignore[arg-type]
        self.assertEqual(clean.status, "complete")

        records = list(j.records)
        records[4], records[5] = records[5], records[4]
        reordered = m6.reduce(fixture, m6.lines_of(records), history=facts, qualifications=proofs)  # type: ignore[arg-type]
        self.assertEqual(reordered.status, "invalid", "physical order is evidence; a swapped pair is a reorder")

        records = [r for r in j.records if r.event.payload.kind != "stop_admission_requested"]
        gapped = m6.reduce(fixture, m6.lines_of(records), history=facts, qualifications=proofs)  # type: ignore[arg-type]
        self.assertEqual(gapped.status, "incomplete", "a missing owner sequence is a gap, not a repair")
        self.assertIsNone(gapped.metrics)

        j = opened(fixture)
        j.admit(fixture.entries["a"], "att-a", j.at(5))
        j.accept("att-a", j.at(4))
        j.final("att-a", j.at(6), outcome="failed")
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "owner receipt ticks cannot regress")

        j = opened(fixture)
        j.admit(fixture.entries["a"], "att-a", j.at(5))
        j.final("att-a", j.at(6), outcome="failed", disposition="not_submitted", owner_sequence=2)
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "same owner sequence, different bytes")

        j = m6.Journal(fixture)
        j.opened(j.at(1))
        j.start(j.at(2))
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "run_started must be sequence 1 at T0")

        j = m6.Journal(fixture)
        j.start(j.at(1))
        j.opened(j.at(2))
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "run_started stamped away from T0")

    def test_producer_incarnation_is_part_of_dedup_identity(self) -> None:
        fixture = e2e({"a": (7, "fresh"), "b": (3, "fresh")})
        a, b = fixture.entries["a"], fixture.entries["b"]
        j = opened(fixture)
        j.admit(a, "att-a", j.at(2), epoch=m6.RUNNER_EPOCH)
        j.final("att-a", j.at(3), outcome="failed", disposition="not_submitted", epoch=m6.RUNNER_EPOCH)
        j.admit(b, "att-b", j.at(4), epoch=m6.RUNNER_EPOCH_2)  # new incarnation restarts at sequence 1
        j.final("att-b", j.at(5), outcome="failed", disposition="not_submitted", epoch=m6.RUNNER_EPOCH_2)
        j.close_run(stop_at=60)
        self.assertEqual(j.records[2].event.producer_sequence, j.records[4].event.producer_sequence)
        receipt = m6.reduce(fixture, j)
        self.assertEqual(receipt.status, "complete", receipt)
        self.assertEqual(receipt.duplicate_events, 0)
        self.assertEqual(m6.outcomes(receipt), {"att-a": "failed", "att-b": "failed"})

        j = opened(fixture)
        j.admit(a, "att-a", j.at(2))  # runner sequence 1
        reused = j.event("e2e_runner", m6.RUNNER_EPOCH, j.admission(b, "att-b"), sequence=1)
        j.stamp(reused, j.at(3))  # same incarnation and sequence, different bytes
        j.final("att-a", j.at(4), outcome="failed", disposition="not_submitted")
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "same incarnation and sequence with changed bytes")

        j = opened(fixture)
        j.admit(a, "att-a", j.at(2))  # runner sequence 1
        j.stamp(j.event("e2e_runner", m6.RUNNER_EPOCH, j.admission(b, "att-b"), sequence=5), j.at(4))
        j.final("att-a", j.at(5), outcome="failed", disposition="not_submitted")
        j.final("att-b", j.at(6), outcome="failed", disposition="not_submitted")
        j.close_run(stop_at=60)
        gap = m6.reduce(fixture, j)
        self.assertEqual(gap.status, "incomplete", "runner sequences 2..4 were never observed")
        self.assertIsNone(gap.metrics)

    def test_same_boot_resume_keeps_cost_and_original_deadline(self) -> None:
        fixture = e2e()
        a = fixture.entries["a"]

        def resumed_run(**resume_kwargs: object) -> tuple[m6.Journal, object, object]:
            j = opened(fixture)
            admission = j.admit(a, "att-a", j.at(2))
            j.accept("att-a", j.at(3))
            j.resumed(j.at(20), **resume_kwargs)  # type: ignore[arg-type]
            j.commit(admission, j.at(21), ledger_seq=1)
            j.confirm(admission, j.at(22), ledger_seq=1)
            proof = m6.qualification_for(a, "e2e_publication", "att-a")
            j.qualify("att-a", proof, j.at(23))
            j.final("att-a", j.at(24))
            j.close_run(stop_at=60)
            return j, proof, m6.history_fact(a, attempt_id="att-a", ledger_seq=1)

        j, proof, fact = resumed_run()
        receipt = m6.reduce(fixture, j, history=(fact,), qualifications=(proof,))  # type: ignore[arg-type]
        self.assertEqual(receipt.status, "complete", receipt)
        self.assertEqual(receipt.elapsed_ticks, 64 * m6.QPC_HZ, "recovery delay is whole-run cost")
        self.assertEqual(receipt.deadline_ticks, fixture.deadline)
        self.assertEqual(receipt.metrics, M6PublicationMetrics(window_pages=7, whole_run_pages=7, carry_in_pages=0))
        self.assertEqual(receipt.events_total, len(j.records))
        self.assertEqual({r.stamp.owner_process_epoch_sha256 for r in j.records}, {m6.OWNER_EPOCH, m6.OWNER_EPOCH_2})

        for label, kwargs in {
            "t0_drift": {"t0": fixture.spec.t0_ticks + 1},
            "deadline_drift": {"deadline": fixture.deadline + m6.QPC_HZ},
            "wrong_predecessor": {"previous": m6.digest("owner-epoch:unknown")},
            "same_epoch": {"new_epoch": m6.OWNER_EPOCH},
            "other_clock": {"clock": m6.clock_domain("boot-b")},
        }.items():
            j, proof, fact = resumed_run(**kwargs)
            receipt = m6.reduce(fixture, j, history=(fact,), qualifications=(proof,))  # type: ignore[arg-type]
            self.assertEqual(receipt.status, "invalid", label)

        j = opened(fixture)
        admission = j.admit(a, "att-a", j.at(2))
        j.owner_epoch = m6.OWNER_EPOCH_2  # incarnation changes without an owner_resumed record
        j.accept("att-a", j.at(3))
        j.final("att-a", j.at(4), outcome="failed")
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "silent owner incarnation change")
        del admission

    def test_different_boot_cannot_produce_combined_qpc_elapsed_or_numerator(self) -> None:
        fixture = e2e()
        a = fixture.entries["a"]
        other_boot = m6.digest("boot:boot-b")
        j = opened(fixture)
        admission = j.admit(a, "att-a", j.at(2))
        j.accept("att-a", j.at(3))
        j.resumed(j.at(20), boot=other_boot)
        j.commit(admission, j.at(21), ledger_seq=1, epoch=m6.RUNNER_EPOCH)
        j.records[-1] = j.stamp(j.records[-1].event, j.at(21), sequence=j.records[-1].stamp.sequence,
                                boot=other_boot, record=False)
        for tick, build in ((22, lambda t: j.stop_requested(t)), (23, lambda t: j.stop_effective(t)),
                            (24, lambda t: j.drained(t)), (25, lambda t: j.resources_closed(t)),
                            (26, lambda t: j.closed(t))):
            build(j.at(tick))
            j.records[-1] = j.stamp(j.records[-1].event, j.at(tick), sequence=j.records[-1].stamp.sequence,
                                    boot=other_boot, record=False)
        receipt = m6.reduce(fixture, j)
        self.assertEqual(receipt.status, "incomplete", receipt)
        self.assertIsNone(receipt.elapsed_ticks, "no QPC subtraction across boots")
        self.assertIsNone(receipt.metrics)
        self.assertEqual(receipt.events_total, len(j.records), "raw evidence is still consumed and hashed")
        self.assertEqual(receipt.journal_prefix_sha256, j.prefix_sha256())
        self.assertEqual([line.attempt_id for line in receipt.sources], ["att-a"])

        other_boot_spec = e2e(boot_label="boot-b")
        j = opened(fixture)
        j.close_run(stop_at=60)
        mismatched = m6.reduce_m6_run(
            spec=other_boot_spec.spec, manifest=other_boot_spec.manifest, quality_plan=other_boot_spec.plan,
            journal_lines=j.lines(), history=(), qualifications=())
        self.assertNotEqual(mismatched.status, "complete", "a journal from another boot/spec is never complete")
        self.assertIsNone(mismatched.metrics)

    def test_truncated_noncanonical_and_oversized_wire_never_exposes_metrics(self) -> None:
        fixture = e2e()
        j, proofs, facts = self._published(fixture)
        lines = list(j.lines())

        truncated = lines[:-1] + [lines[-1][:-1]]
        receipt = m6.reduce(fixture, tuple(truncated), history=facts, qualifications=proofs)  # type: ignore[arg-type]
        self.assertEqual(receipt.status, "incomplete")
        self.assertIsNone(receipt.metrics)
        self.assertEqual(receipt.events_total, len(lines), "the partial tail is a consumed record")
        self.assertEqual(receipt.journal_bytes_consumed, sum(len(line) for line in truncated))
        self.assertEqual(receipt.journal_prefix_sha256, m6.sha256_of(b"".join(truncated)),
                         "the prefix hash covers the partial tail responsible for incompleteness")

        mid_truncated = lines[:5] + [lines[5][:-1]] + lines[6:]
        receipt = m6.reduce(fixture, tuple(mid_truncated), history=facts, qualifications=proofs)  # type: ignore[arg-type]
        self.assertEqual(receipt.status, "incomplete")
        self.assertEqual(receipt.events_total, 6, "reading stops at the first record without a terminator")
        self.assertEqual(receipt.journal_bytes_consumed, sum(len(line) for line in mid_truncated[:6]))

        pretty = json.dumps(json.loads(lines[3]), indent=2, sort_keys=True).encode() + b"\n"
        noncanonical = lines[:3] + [pretty] + lines[4:]
        receipt = m6.reduce(fixture, tuple(noncanonical), history=facts, qualifications=proofs)  # type: ignore[arg-type]
        self.assertEqual(receipt.status, "invalid")
        self.assertIsNone(receipt.metrics)
        self.assertEqual(receipt.events_total, len(lines))
        self.assertEqual(receipt.journal_prefix_sha256, m6.sha256_of(b"".join(noncanonical)))

        oversized = b"x" * (fixture.spec.resources.max_record_bytes + 1) + b"\n"
        bounded = lines[:4] + [oversized] + lines[4:]
        receipt = m6.reduce(fixture, tuple(bounded), history=facts, qualifications=proofs)  # type: ignore[arg-type]
        self.assertEqual(receipt.status, "incomplete")
        self.assertIsNone(receipt.metrics)
        self.assertEqual(receipt.events_total, 4, "the over-bound record belongs to neither count nor prefix")
        self.assertEqual(receipt.journal_bytes_consumed, sum(len(line) for line in lines[:4]))
        self.assertEqual(receipt.journal_prefix_sha256, m6.sha256_of(b"".join(lines[:4])))

        small = e2e(resources=m6.envelope(max_events=5, max_attempts=5))
        j, proofs, facts = self._published(small)
        receipt = m6.reduce(small, j, history=facts, qualifications=proofs)  # type: ignore[arg-type]
        self.assertEqual(receipt.status, "incomplete")
        self.assertEqual(receipt.events_total, 5)
        self.assertEqual(receipt.journal_prefix_sha256, m6.sha256_of(b"".join(j.lines()[:5])))

        bound = sum(len(line) for line in lines[:8]) + 10
        tight = e2e(resources=m6.envelope(max_record_bytes=2048, max_log_bytes=bound))
        j, proofs, facts = self._published(tight)
        self.assertEqual(sum(len(line) for line in j.lines()[:8]), bound - 10, "record lengths do not depend on the spec")
        self.assertGreater(len(j.lines()[8]), 10)
        receipt = m6.reduce(tight, j, history=facts, qualifications=proofs)  # type: ignore[arg-type]
        self.assertEqual(receipt.status, "incomplete")
        self.assertEqual(receipt.events_total, 8, "a record that would exceed the log byte bound is not consumed")
        self.assertEqual(receipt.journal_prefix_sha256, m6.sha256_of(b"".join(j.lines()[:8])))

    def test_io_failure_is_not_swallowed_as_successful_prefix(self) -> None:
        fixture = e2e()
        j, proofs, facts = self._published(fixture)
        lines = j.lines()

        def failing() -> object:
            yield lines[0]
            yield lines[1]
            raise OSError("synthetic read failure")

        with self.assertRaises(OSError):
            m6.reduce(fixture, failing(), history=facts, qualifications=proofs)  # type: ignore[arg-type]

        def wrong_type() -> object:
            yield lines[0]
            yield lines[1].decode("utf-8")

        with self.assertRaises(TypeError):
            m6.reduce(fixture, wrong_type(), history=facts, qualifications=proofs)  # type: ignore[arg-type]

        def bytearray_line() -> object:
            yield bytearray(lines[0])

        with self.assertRaises(TypeError):
            m6.reduce(fixture, bytearray_line())  # type: ignore[arg-type]

    def test_producer_conflict_and_postclose_retry_have_distinct_failure_reasons(self) -> None:
        fixture = e2e()
        a = fixture.entries["a"]
        j = opened(fixture)
        admission = j.admit(a, "att-a", j.at(2))
        j.accept("att-a", j.at(3))
        j.commit(admission, j.at(4), ledger_seq=1)
        conflicting = j.event("e2e_runner", m6.RUNNER_EPOCH, m6.M6PublicationCommitted(
            **{**j.records[-1].event.payload.model_dump(), "ledger_seq": 2}), sequence=j.records[-1].event.producer_sequence)
        j.stamp(conflicting, j.at(5))
        j.final("att-a", j.at(6), outcome="failed")
        j.close_run(stop_at=60)
        conflict = m6.reduce(fixture, j)
        self.assertEqual(conflict.status, "invalid")

        j = opened(fixture)
        admission = j.admit(a, "att-a", j.at(2))
        j.accept("att-a", j.at(3))
        j.final("att-a", j.at(6), outcome="failed")
        j.close_run(stop_at=60)
        retry = j.records[2].event  # exact producer bytes, but a *new* owner stamp after Tclose
        j.stamp(retry, j.at(65))
        post_close = m6.reduce(fixture, j)
        self.assertEqual(post_close.status, "invalid")

        self.assertTrue(conflict.invalid_reasons and post_close.invalid_reasons)
        self.assertNotEqual(set(conflict.invalid_reasons), set(post_close.invalid_reasons))

    def test_missing_or_cross_source_qualification_and_record_bound(self) -> None:
        fixture = e2e({"a": (7, "fresh"), "b": (3, "fresh")})
        a, b = fixture.entries["a"], fixture.entries["b"]
        j = opened(fixture)
        admission = j.admit(a, "att-a", j.at(2))
        j.accept("att-a", j.at(3))
        j.commit(admission, j.at(4), ledger_seq=1)
        j.confirm(admission, j.at(5), ledger_seq=1)
        proof_a = m6.qualification_for(a, "e2e_publication", "att-a")
        j.qualify("att-a", proof_a, j.at(6))
        j.final("att-a", j.at(7))
        j.close_run(stop_at=60)
        fact = m6.history_fact(a, attempt_id="att-a", ledger_seq=1)

        missing = m6.reduce(fixture, j, history=(fact,), qualifications=())
        self.assertEqual(missing.status, "incomplete", "a referenced qualification that was not supplied")
        self.assertEqual(m6.outcomes(missing), {"att-a": "qualification_missing"})

        proof_b = m6.qualification_for(b, "e2e_publication", "att-a")
        index = next(i for i, r in enumerate(j.records) if r.event.payload.kind == "document_qualified")
        j.records[index] = j.stamp(j.event("quality_verifier", m6.QUALITY_EPOCH, m6.M6DocumentQualified(
            attempt_id="att-a", qualification_evidence_sha256=proof_b.canonical_sha256()), sequence=1),
            j.at(6), sequence=j.records[index].stamp.sequence, record=False)
        cross = m6.reduce(fixture, j, history=(fact,), qualifications=(proof_b,))
        self.assertEqual(cross.status, "invalid", "evidence for another source cannot qualify this attempt")
        self.assertEqual(m6.outcomes(cross)["att-a"], "quality_not_scorable")

        bounded = e2e({"a": (7, "fresh")}, resources=m6.envelope(max_attempts=1, max_events=50))
        j2 = opened(bounded)
        j2.close_run(stop_at=60)
        with self.assertRaises(ValueError):
            m6.reduce(bounded, j2, qualifications=(proof_a, proof_b))
        with self.assertRaises(ValueError):
            m6.reduce(bounded, j2, history=(fact, m6.history_fact(b, attempt_id="att-b", ledger_seq=2)))
        with self.assertRaises(ValueError):
            m6.reduce(fixture, j, history=(fact, fact))
        with self.assertRaises(ValueError):
            m6.reduce(fixture, j, qualifications=(proof_a, proof_a))

        # A document_qualified for an attempt never admitted is a bound violation, not a credit.
        j = opened(fixture)
        j.qualify("att-ghost", proof_a, j.at(3))
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j, qualifications=(proof_a,)).status, "invalid")

    def test_qualification_failures_have_matching_source_outcome(self) -> None:
        fixture = e2e({"x": (4, "fresh"), "y": (5, "fresh"), "z": (6, "fresh")},
                      policies=(("scan_noise", "review_required"),))
        x, y, z = fixture.entries["x"], fixture.entries["y"], fixture.entries["z"]
        j = opened(fixture)
        proofs = []
        facts = []
        for base, (label, source, kwargs) in enumerate((
            ("x", x, {"checks": m6.check_results(failing=("reading_order_contiguity",))}),
            ("y", y, {"unit_count": 3, "needs_review": 1, "review_reasons": ("scan_noise",)}),
        )):
            attempt = "att-" + label
            start = 2 + 10 * base
            admission = j.admit(source, attempt, j.at(start))
            j.accept(attempt, j.at(start + 1))
            j.commit(admission, j.at(start + 2), ledger_seq=len(proofs) + 1)
            j.confirm(admission, j.at(start + 3), ledger_seq=len(proofs) + 1)
            proof = m6.qualification_for(source, "e2e_publication", attempt, **kwargs)
            j.qualify(attempt, proof, j.at(start + 4))
            j.final(attempt, j.at(start + 5))
            proofs.append(proof)
            facts.append(m6.history_fact(source, attempt_id=attempt, ledger_seq=len(proofs)))
        j.admit(z, "att-z", j.at(30))
        j.accept("att-z", j.at(31))
        j.final("att-z", j.at(32), outcome="failed")
        j.close_run(stop_at=60)

        receipt = m6.reduce(fixture, j, history=tuple(facts), qualifications=tuple(proofs))
        self.assertEqual(receipt.status, "complete", receipt)
        self.assertEqual(m6.outcomes(receipt), {
            "att-x": "quality_not_scorable", "att-y": "quality_review_pending", "att-z": "failed",
        })
        self.assertEqual(receipt.metrics, M6PublicationMetrics(window_pages=0, whole_run_pages=0, carry_in_pages=0))
        self.assertEqual(receipt.elapsed_ticks, 64 * m6.QPC_HZ, "failed attempts still cost time")


class ModeAndOriginTests(unittest.TestCase):
    def test_service_replay_is_labeled_and_never_claims_first_publication(self) -> None:
        fixture = service({"s1": (4, "fresh"), "s2": (6, "replay")})
        s1, s2 = fixture.entries["s1"], fixture.entries["s2"]
        j = opened(fixture)
        _, proof_1 = m6.service_validate(j, s1, "att-1", admit_at=2)
        _, proof_2 = m6.service_validate(j, s2, "att-2", admit_at=10)
        j.close_run(stop_at=60)
        receipt = m6.reduce(fixture, j, qualifications=(proof_1, proof_2))
        self.assertEqual(receipt.status, "complete", receipt)
        self.assertIsInstance(receipt.metrics, M6ServiceMetrics)
        self.assertEqual(receipt.metrics, M6ServiceMetrics(window_pages=10, whole_run_pages=10, replay_pages=6, carry_in_pages=0))
        self.assertEqual(receipt.metrics.kind, "service_validated_source_pages")  # type: ignore[union-attr]
        self.assertEqual(m6.outcomes(receipt), {"att-1": "credited_window", "att-2": "credited_window"})
        self.assertEqual(receipt.sources[0].ready_received_ticks, j.at(5), "credit time is the later of quality and validation")

        j = opened(fixture)
        j.admit(s1, "att-1", j.at(2))
        j.accept("att-1", j.at(3))
        j.runner(m6.M6PublicationCommitted(
            attempt_id="att-1", processing_run_id="prun-x", document_id="doc-x",
            source_pdf_sha256=s1.source_pdf_sha256, source_page_count=s1.source_page_count,
            ledger_seq=1, winner_sha256=m6.digest("w"), durable_base_sha256=m6.digest("b")), j.at(4))
        j.final("att-1", j.at(5))
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "a service run cannot carry publication facts")

        j = opened(fixture)
        j.admit(s1, "att-1", j.at(2))
        j.accept("att-1", j.at(3))
        j.final("att-1", j.at(5), outcome="published")
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "a service attempt cannot report publication")

        j = opened(fixture)
        j.admit(s1, "att-1", j.at(2))
        j.accept("att-1", j.at(3))
        j.validate("att-1", j.at(4), bundle=m6.digest("provider-bundle:other"))
        proof = m6.qualification_for(s1, "service_diagnostic", "att-1")
        j.qualify("att-1", proof, j.at(5))
        j.final("att-1", j.at(6))
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j, qualifications=(proof,)).status, "invalid",
                         "validated bundle and qualified bundle must be the same provider artifact")

        j = opened(fixture)
        j.admit(s1, "att-1", j.at(2))
        j.accept("att-1", j.at(3))
        proof = m6.qualification_for(s1, "service_diagnostic", "att-1")
        j.qualify("att-1", proof, j.at(5))
        j.final("att-1", j.at(6))
        j.close_run(stop_at=60)
        unvalidated = m6.reduce(fixture, j, qualifications=(proof,))
        self.assertEqual(unvalidated.status, "complete")
        self.assertEqual(unvalidated.metrics, M6ServiceMetrics(window_pages=0, whole_run_pages=0, replay_pages=0, carry_in_pages=0))

    def test_service_two_attempts_same_source_credit_original_pages_once(self) -> None:
        fixture = service({"s": (4, "fresh")})
        s = fixture.entries["s"]
        j = opened(fixture)
        _, proof_1 = m6.service_validate(j, s, "att-1", admit_at=2)
        _, proof_2 = m6.service_validate(j, s, "att-2", admit_at=10)
        j.close_run(stop_at=60)
        receipt = m6.reduce(fixture, j, qualifications=(proof_1, proof_2))
        self.assertEqual(receipt.status, "complete", receipt)
        self.assertEqual(receipt.metrics, M6ServiceMetrics(window_pages=4, whole_run_pages=4, replay_pages=0, carry_in_pages=0))
        self.assertEqual(len(receipt.sources), 2, "both attempts remain visible")

        j = opened(fixture)
        j.admit(s, "att-1", j.at(2))
        j.admit(s, "att-1", j.at(3))
        j.final("att-1", j.at(4), outcome="failed", disposition="not_submitted")
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "one attempt cannot be admitted twice")

    def test_e2e_replay_and_unqualified_documents_do_not_supply_credit(self) -> None:
        fixture = e2e({"a": (7, "fresh"), "c": (3, "replay")})
        a, c = fixture.entries["a"], fixture.entries["c"]
        j = opened(fixture)
        _, proof_c, fact_c = m6.e2e_publish(j, c, "att-c", admit_at=2, ledger_seq=1)
        admission = j.admit(a, "att-a", j.at(10))
        j.accept("att-a", j.at(11))
        j.commit(admission, j.at(12), ledger_seq=2)
        j.confirm(admission, j.at(13), ledger_seq=2)
        j.final("att-a", j.at(14))
        j.close_run(stop_at=60)
        receipt = m6.reduce(fixture, j, history=(fact_c, m6.history_fact(a, attempt_id="att-a", ledger_seq=2)),
                            qualifications=(proof_c,))
        self.assertEqual(receipt.status, "complete", receipt)
        self.assertEqual(m6.outcomes(receipt), {"att-a": "qualification_missing", "att-c": "replay"})
        self.assertEqual(receipt.metrics, M6PublicationMetrics(window_pages=0, whole_run_pages=0, carry_in_pages=0))

        j = opened(fixture)
        j.admit(a, "att-a", j.at(2))
        j.accept("att-a", j.at(3))
        j.validate("att-a", j.at(4))
        j.final("att-a", j.at(5))
        j.close_run(stop_at=60)
        self.assertEqual(m6.reduce(fixture, j).status, "invalid", "service validation is not E2E evidence")

    def test_carry_in_is_separate_and_unresolved_carry_in_keeps_run_incomplete(self) -> None:
        fixture = e2e({"a": (7, "fresh"), "k": (9, "carry_in")}, carry_in=("carry-k",))
        a, k = fixture.entries["a"], fixture.entries["k"]
        j = opened(fixture)
        _, proof_k, fact_k = m6.e2e_publish(j, k, "carry-k", admit_at=2, ledger_seq=1)
        _, proof_a, fact_a = m6.e2e_publish(j, a, "att-a", admit_at=10, ledger_seq=2)
        j.close_run(stop_at=60)
        receipt = m6.reduce(fixture, j, history=(fact_a, fact_k), qualifications=(proof_a, proof_k))
        self.assertEqual(receipt.status, "complete", receipt)
        self.assertEqual(m6.outcomes(receipt), {"att-a": "credited_window", "carry-k": "carry_in"})
        self.assertEqual(receipt.metrics, M6PublicationMetrics(window_pages=7, whole_run_pages=7, carry_in_pages=9))

        pending = e2e({"a": (7, "fresh"), "k": (9, "carry_in")}, carry_in=("carry-k", "carry-z"))
        j = opened(pending)
        _, proof_k, fact_k = m6.e2e_publish(j, pending.entries["k"], "carry-k", admit_at=2, ledger_seq=1)
        j.close_run(stop_at=60)
        receipt = m6.reduce(pending, j, history=(fact_k,), qualifications=(proof_k,))
        self.assertEqual(receipt.status, "incomplete", "an unresolved carry-in obligation")
        self.assertIsNone(receipt.metrics)

        j = opened(fixture)
        _, proof_k, fact_k = m6.e2e_publish(j, k, "att-fresh-k", admit_at=2, ledger_seq=1)
        j.close_run(stop_at=60)
        receipt = m6.reduce(fixture, j, history=(fact_k,), qualifications=(proof_k,))
        self.assertEqual(receipt.status, "invalid", "a carry-in source must not be freshly resubmitted")

        j = opened(fixture)
        _, proof_a, fact_a = m6.e2e_publish(j, a, "carry-k", admit_at=2, ledger_seq=1)
        j.close_run(stop_at=60)
        receipt = m6.reduce(fixture, j, history=(fact_a,), qualifications=(proof_a,))
        self.assertEqual(receipt.status, "invalid", "a carry-in attempt id cannot admit a fresh source")

    def test_service_replay_is_not_e2e_and_incident_is_permanent(self) -> None:
        fixture = service({"s": (4, "fresh")})
        s = fixture.entries["s"]
        j = opened(fixture)
        _, proof = m6.service_validate(j, s, "att-1", admit_at=2)
        j.incident(j.at(30), code="observation_refused")
        j.close_run(stop_at=60)
        receipt = m6.reduce(fixture, j, qualifications=(proof,))
        self.assertEqual(receipt.status, "incomplete", "every measurement incident makes the run incomplete")
        self.assertIsNone(receipt.metrics)
        self.assertEqual(receipt.tclose_ticks, j.at(64))
        self.assertEqual(m6.outcomes(receipt), {"att-1": "credited_window"},
                         "the per-source projection stays visible without a trusted numerator")


if __name__ == "__main__":
    unittest.main()
