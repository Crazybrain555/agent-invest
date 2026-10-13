from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import threading
from typing import Any
import unittest
from unittest import mock

import httpx

from disclosure_anchor.adapters.sources.cninfo.web_source import CninfoWebSource
from disclosure_anchor.application.contracts.processing_request import (
    MAX_ITEM_FAILURES,
    AttemptRecord,
    DownloadFacts,
    RequestRecord,
    TargetRecord,
    derive_status,
    normalize_intent,
)
from disclosure_anchor.application.dto.worker_report import WorkerLimits
from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    DisclosureWindow,
    SourceCompanyProfile,
    SourceSecurity,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.ports.processing_requests import PersistedCandidate
from disclosure_anchor.application.use_cases.processing_requests import (
    IdempotencyConflict,
    ProcessingRequestLimitExceeded,
    ProcessingRequestPolicy,
    ProcessingRequestResolver,
    ProcessingRequestService,
    ProcessingTargetMismatch,
    ProcessingTargetNotFound,
    ResolverRunResult,
)
from disclosure_anchor.application.worker import worker as worker_module
from disclosure_anchor.application.worker.worker import WorkerConfig, WorkerDeps, run_once
from disclosure_anchor.domain.errors import SourceRequestError

from tests.unit._fakes import FakeUnitOfWork
from tests.unit.processing_request_fakes import (
    ProcessingRequestStore,
    StoreStatusReader,
    local_document,
    uow_factory,
)

TODAY = date(2026, 10, 7)
START = datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self.now = START
        self.lock = threading.Lock()

    def __call__(self) -> datetime:
        with self.lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.now += timedelta(seconds=seconds)


def _body(key: str, target: dict[str, object], *, enqueue_parse: bool = True) -> dict[str, object]:
    return {"idempotency_key": key, "enqueue_parse": enqueue_parse, "target": target}


def _title(title: str = "2025年年度报告", **overrides: object) -> dict[str, object]:
    target: dict[str, object] = {
        "kind": "title_match", "security_code": "600519", "exchange": "SSE",
        "date_from": "2026-01-01", "date_to": "2026-03-31", "title_contains": title,
    }
    target.update(overrides)
    return target


def _provider(pdid: str = "1220000001", **overrides: object) -> dict[str, object]:
    target: dict[str, object] = {
        "kind": "provider_document", "security_code": "600519", "exchange": "SSE",
        "date_from": "2026-03-01", "date_to": "2026-03-31", "provider_document_id": pdid,
    }
    target.update(overrides)
    return target


def _ref(pdid: str, title: str, day: date, *, code: str = "600519") -> AnnouncementRef:
    return AnnouncementRef(
        provider="cninfo",
        provider_document_id=pdid,
        title=title,
        download_url=f"https://static.cninfo.com.cn/finalpage/{day.isoformat()}/{pdid}.PDF",
        raw_category="010301",
        announcement_date=day,
        security_code=code,
        security_name="贵州茅台",
        file_size=2048,
        index_updated_at=None,
        filing_type="annual_report",
        raw_record={"announcementId": pdid},
    )


class _Source:
    """Window-filtered provider fake; ``fail_on`` maps call index → exception."""

    def __init__(self, refs: list[AnnouncementRef], *, fail_on: dict[int, Exception] | None = None,
                 on_call: Any = None) -> None:
        self.refs = refs
        self.fail_on = fail_on or {}
        self.on_call = on_call
        self.calls: list[tuple[date, date]] = []

    def search_announcements(self, security: SourceSecurity, window: DisclosureWindow) -> list[AnnouncementRef]:
        index = len(self.calls)
        self.calls.append((window.start, window.end))
        if self.on_call is not None:
            self.on_call(index)
        if index in self.fail_on:
            raise self.fail_on[index]
        return [ref for ref in self.refs if window.start <= ref.announcement_date <= window.end]

    def download_pdf_to(self, ref: AnnouncementRef, sink: object) -> object:
        raise AssertionError("discovery never downloads")


def _profile(code: str) -> SourceCompanyProfile:
    return SourceCompanyProfile(
        security_code=code, security_name="贵州茅台", legal_name="贵州茅台酒股份有限公司",
        provider_org_id="gssh0600519", uscc=None,
    )


def _classify(exc: BaseException) -> str:
    code = getattr(exc, "error_code", None)
    if code == "rate_limited":
        return "rate_limited"
    if code == "quota_exhausted":
        return "quota_exhausted"
    if code == "http_503":
        return "outage"
    return "item"


class _Harness:
    def __init__(self, *, limit: int = 200) -> None:
        self.clock = _Clock()
        self.store = ProcessingRequestStore(clock=self.clock)
        self.base = FakeUnitOfWork()
        self.factory = uow_factory(self.store, self.base)
        self.reader = StoreStatusReader(self.store)
        self.slept: list[float] = []
        self.service = ProcessingRequestService(
            uow_factory=self.factory,
            status_reader=self.reader,
            policy=ProcessingRequestPolicy(daily_request_limit=limit),
            clock=self.clock,
            sleep=self._sleep,
            monotonic=self._monotonic,
        )
        self.mono = 0.0

    def _sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.mono += seconds
        self.clock.advance(seconds)

    def _monotonic(self) -> float:
        return self.mono

    def submit(self, key: str, target: dict[str, object], *, enqueue_parse: bool = True) -> Any:
        return self.service.submit(normalize_intent(_body(key, target, enqueue_parse=enqueue_parse), today=TODAY))

    def resolver(self, source: _Source, *, stop: Any = None, monotonic: Any = None,
                 deadline: float = 300) -> ProcessingRequestResolver:
        return ProcessingRequestResolver(
            uow_factory=self.factory,
            source=source,
            profile_loader=_profile,
            index_interface="cninfo:p_info3015",
            classify_failure=_classify,
            clock=self.clock,
            should_stop=stop or (lambda: False),
            monotonic=monotonic or (lambda: 0.0),
            deadline_seconds=deadline,
        )

    def attempts(self, request_id: str) -> list[AttemptRecord]:
        return list(self.store.committed.attempts.get(request_id, []))

    def target(self, request_id: str) -> TargetRecord | None:
        return self.store.committed.targets.get(request_id)


KEY_A = "agent-key-aaaaaaaaaaaaaaaa"
KEY_B = "agent-key-bbbbbbbbbbbbbbbb"


class SubmitTests(unittest.TestCase):
    def test_accept_is_durable_and_prompt_and_replay_joins(self) -> None:
        harness = _Harness()
        first = harness.submit(KEY_A, _title())
        self.assertFalse(first.replayed)
        self.assertEqual(first.status["state"], "resolving")
        self.assertEqual(first.status["next_action"], "wait")
        replay = harness.submit(KEY_A, _title())
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.request.request_id, first.request.request_id)
        self.assertEqual(len(harness.store.committed.requests), 1)
        # Accepting and replaying never create attempts: only the worker does.
        self.assertEqual(harness.attempts(first.request.request_id), [])

    def test_same_key_different_intent_conflicts_without_writing(self) -> None:
        harness = _Harness()
        first = harness.submit(KEY_A, _title())
        with self.assertRaises(IdempotencyConflict) as raised:
            harness.submit(KEY_A, _title(), enqueue_parse=False)
        self.assertEqual(raised.exception.request_id, first.request.request_id)
        self.assertEqual(len(harness.store.committed.requests), 1)

    def test_concurrent_same_key_creates_exactly_one_request(self) -> None:
        harness = _Harness()
        results: list[Any] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def submit() -> None:
            barrier.wait()
            try:
                results.append(harness.submit(KEY_A, _title()))
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(harness.store.committed.requests), 1)
        self.assertEqual({view.request.request_id for view in results}, {results[0].request.request_id})
        self.assertEqual(sum(1 for view in results if not view.replayed), 1)

    def test_rolling_limit_refuses_only_new_requests(self) -> None:
        harness = _Harness(limit=1)
        harness.submit(KEY_A, _title())
        with self.assertRaises(ProcessingRequestLimitExceeded):
            harness.submit(KEY_B, _title("半年度报告"))
        self.assertEqual(len(harness.store.committed.requests), 1)
        self.assertTrue(harness.submit(KEY_A, _title()).replayed)

    def test_local_targets_bind_in_the_accepting_transaction(self) -> None:
        harness = _Harness()
        harness.store.documents["doc_01M4B45TP76AY533REBBYHCBGX"] = local_document(
            "doc_01M4B45TP76AY533REBBYHCBGX")
        harness.store.documents["doc_01M49JD4FDZT6GERD01X7BV499"] = local_document(
            "doc_01M49JD4FDZT6GERD01X7BV499", status="published", provider_document_id="1220000009")
        registered = harness.submit(KEY_A, {"kind": "document", "document_id": "doc_01M4B45TP76AY533REBBYHCBGX"})
        target = harness.target(registered.request.request_id)
        assert target is not None
        self.assertEqual((target.resolution_method, target.admit_parse, target.admit_download),
                         ("document_id", True, False))
        self.assertEqual(target.raw_file_hash, "sha256:" + "a" * 64)
        published = harness.submit(KEY_B, _provider("1220000009"))
        bound = harness.target(published.request.request_id)
        assert bound is not None
        # Published work is reused, never re-admitted to parse.
        self.assertEqual((bound.resolution_method, bound.admit_parse), ("local_document", False))

    def test_persisted_candidate_admits_its_download_without_provider_io(self) -> None:
        harness = _Harness()
        harness.store.candidates["1220000002"] = PersistedCandidate(
            index_source_access_id="sa_index_old",
            candidate={"provider_document_id": "1220000002", "security_code": "600519",
                       "exchange": "SSE", "announcement_date": "2026-03-15"},
        )
        view = harness.submit(KEY_A, _provider("1220000002"))
        target = harness.target(view.request.request_id)
        assert target is not None
        self.assertEqual((target.resolution_method, target.admit_download, target.admit_parse,
                          target.index_source_access_id),
                         ("local_candidate", True, True, "sa_index_old"))

    def test_unknown_or_contradicting_local_targets_store_nothing(self) -> None:
        harness = _Harness()
        with self.assertRaises(ProcessingTargetNotFound):
            harness.submit(KEY_A, {"kind": "document", "document_id": "doc_01M4B45TP76AY533REBBYHCBG0"})
        harness.store.documents["doc_x"] = local_document(
            "doc_x", provider_document_id="1220000003", security_code="000001", exchange="SZSE")
        with self.assertRaises(ProcessingTargetMismatch):
            harness.submit(KEY_B, _provider("1220000003"))
        self.assertEqual(harness.store.committed.requests, {})


class WaitTests(unittest.TestCase):
    def test_wait_returns_on_terminal_change_or_timeout_without_side_effects(self) -> None:
        harness = _Harness()
        view = harness.submit(KEY_A, _title())
        request_id = view.request.request_id
        timeout = harness.service.wait(request_id, timeout_s=3, since=None)
        self.assertEqual(timeout.outcome, "timeout")
        self.assertEqual(timeout.view.request.request_id, request_id)
        self.assertEqual(harness.attempts(request_id), [])
        changed_from = harness.service.wait(request_id, timeout_s=1, since="sha256:" + "f" * 64)
        self.assertEqual(changed_from.outcome, "changed")
        with self.assertRaises(ValueError):
            harness.service.wait(request_id, timeout_s=21, since=None)

    def test_wait_observes_worker_progress(self) -> None:
        harness = _Harness()
        harness.reader._facts = lambda request, target: (  # type: ignore[attr-defined]
            {"download": DownloadFacts(selected=True)} if target is not None else {})
        view = harness.submit(KEY_A, _title())
        resolver = harness.resolver(_Source([_ref("1220000001", "贵州茅台2025年年度报告", date(2026, 3, 30))]))

        def progress(seconds: float) -> None:
            harness.mono += seconds
            resolver.run(limit=1)

        harness.service._sleep = progress  # type: ignore[attr-defined]
        outcome = harness.service.wait(view.request.request_id, timeout_s=5, since=None)
        self.assertEqual(outcome.outcome, "changed")
        self.assertEqual(outcome.view.status["resolution"]["state"], "resolved")
        self.assertEqual(outcome.view.status["state"], "download_queued")


class ResolverTests(unittest.TestCase):
    def _pending(self, harness: _Harness, target: dict[str, object], key: str = KEY_A) -> str:
        return str(harness.submit(key, target).request.request_id)

    def test_worker_resolves_unique_exact_filing_without_any_client_action(self) -> None:
        harness = _Harness()
        request_id = self._pending(harness, _title())
        refs = [
            _ref("1220000001", "贵州茅台2025年年度报告", date(2026, 3, 30)),
            _ref("1220000004", "贵州茅台关于分红的公告", date(2026, 1, 15)),
            _ref("1220000005", "其他公司2025年年度报告", date(2026, 3, 30), code="000001"),
        ]
        source = _Source(refs)
        result = harness.resolver(source).run(limit=5)
        self.assertEqual((result.claimed, result.matched), (1, 1))
        self.assertEqual(len(source.calls), 3)  # 90-day window, one call per 30-day chunk
        target = harness.target(request_id)
        assert target is not None
        self.assertEqual((target.resolution_method, target.provider_document_id,
                          target.admit_download, target.admit_parse),
                         ("provider_discovery", "1220000001", True, True))
        index = harness.base.source_accesses.get(str(target.index_source_access_id))
        assert index is not None and index.result_snapshot is not None
        snapshot = index.result_snapshot
        # Only the selected candidate is acquirable; the window is summarized.
        self.assertEqual([c["provider_document_id"] for c in snapshot["candidates"]], ["1220000001"])
        self.assertEqual(snapshot["selection"]["provider_row_count"], 3)
        self.assertEqual(snapshot["selection"]["window_chunks"], 3)
        self.assertEqual(index.query_params, {"scode": "600519", "sdate": "2026-01-01", "edate": "2026-03-31"})
        # No pool membership or checkpoint side effects for the company.
        self.assertEqual(harness.base.tracked_companies.all(), [])
        self.assertEqual(harness.base.source_checkpoints.all(), [])
        self.assertEqual(harness.resolver(source).run(limit=5).claimed, 0)

    def test_ambiguous_title_returns_bounded_choices_and_binds_nothing(self) -> None:
        harness = _Harness()
        # A choice names a local document only when that row is the filing.
        harness.store.documents["doc_known"] = local_document(
            "doc_known", provider_document_id="1220000122", announcement_date=date(2026, 3, 23))
        harness.store.documents["doc_other"] = local_document(
            "doc_other", provider_document_id="1220000121", security_code="600520",
            announcement_date=date(2026, 3, 22))
        request_id = self._pending(harness, _title("年度报告"))
        refs = [_ref(f"12200001{n:02d}", f"2025年年度报告{n}", date(2026, 3, 1) + timedelta(days=n % 28))
                for n in range(23)]
        harness.resolver(_Source(refs)).run(limit=1)
        self.assertIsNone(harness.target(request_id))
        attempt = harness.attempts(request_id)[-1]
        self.assertEqual(attempt.outcome, "ambiguous")
        assert attempt.choices is not None
        self.assertEqual(len(attempt.choices), 20)
        self.assertTrue(attempt.choices_truncated)
        named = {choice["provider_document_id"]: choice["document_id"] for choice in attempt.choices}
        self.assertEqual((named["1220000122"], named["1220000121"]), ("doc_known", None))
        status = harness.service.get(request_id).status
        self.assertEqual((status["state"], status["next_action"]),
                         ("needs_selection", "choose_and_submit_new_request"))

    def test_partial_window_failure_never_concludes_and_keeps_provenance(self) -> None:
        harness = _Harness()
        request_id = self._pending(harness, _title())
        source = _Source(
            [_ref("1220000001", "2025年年度报告", date(2026, 3, 30))],
            fail_on={1: SourceRequestError("partial", error_code="incomplete_response", retryable=True)},
        )
        result = harness.resolver(source).run(limit=1)
        self.assertEqual(result.failed, 1)
        self.assertIsNone(harness.target(request_id))
        attempt = harness.attempts(request_id)[-1]
        self.assertEqual(attempt.outcome, "failed")
        assert attempt.error is not None
        self.assertEqual(attempt.error["coverage"]["chunks_completed"], 1)
        self.assertEqual(attempt.error["coverage"]["chunks_total"], 3)
        self.assertIsNotNone(attempt.next_attempt_at)
        failed_access = harness.base.source_accesses.get(str(attempt.index_source_access_id))
        assert failed_access is not None
        self.assertEqual(failed_access.status, "failed")
        self.assertEqual(failed_access.query_params,
                         {"scode": "600519", "sdate": "2026-01-31", "edate": "2026-03-01"})
        status = harness.service.get(request_id).status
        self.assertEqual((status["state"], status["next_action"]), ("resolving", "wait"))
        self.assertNotIn(status["resolution"]["state"], ("not_found", "resolved"))

    def test_provider_refusal_defers_without_spending_the_item_budget(self) -> None:
        harness = _Harness()
        first = self._pending(harness, _title())
        harness.clock.advance(1)
        second = self._pending(harness, _title("半年度报告"), key=KEY_B)
        source = _Source([], fail_on={0: SourceRequestError("slow down", error_code="rate_limited", retryable=True)})
        result = harness.resolver(source).run(limit=5)
        self.assertEqual((result.deferred, result.provider_refusal), (1, "rate_limited"))
        self.assertEqual(harness.attempts(second), [])  # the run stopped at the refusal
        resolution = harness.service.get(first).status["resolution"]
        self.assertEqual((resolution["state"], resolution["item_failure_count"]), ("pending", 0))
        self.assertIsNotNone(resolution["next_attempt_at"])

    def test_deadline_and_stop_between_chunks_defer_without_conclusion(self) -> None:
        harness = _Harness()
        request_id = self._pending(harness, _title())
        ticks = iter([0.0, 0.0, 400.0, 400.0])
        harness.resolver(_Source([]), monotonic=lambda: next(ticks)).run(limit=1)
        attempt = harness.attempts(request_id)[-1]
        assert attempt.error is not None
        self.assertEqual((attempt.outcome, attempt.error["error_code"]),
                         ("deferred", "discovery_deadline_exceeded"))
        harness.clock.advance(3600)
        calls = {"n": 0}

        def stop() -> bool:
            calls["n"] += 1
            return calls["n"] > 3

        harness.resolver(_Source([]), stop=stop).run(limit=1)
        stopped = harness.attempts(request_id)[-1]
        assert stopped.error is not None
        self.assertEqual((stopped.outcome, stopped.error["error_code"]), ("deferred", "worker_stop_requested"))
        self.assertEqual(stopped.next_attempt_at, harness.clock())
        self.assertIsNone(harness.target(request_id))

    def test_expired_lease_is_abandoned_then_retried_with_history(self) -> None:
        harness = _Harness()
        request_id = self._pending(harness, _title())
        with harness.factory() as uow:
            uow.processing_requests.start_attempt(
                attempt_id="dpra_" + "0" * 26, request_id=request_id, attempt_no=1,
                started_at=harness.clock(), lease_expires_at=harness.clock() + timedelta(seconds=900))
            uow.commit()
        source = _Source([_ref("1220000001", "2025年年度报告", date(2026, 3, 30))])
        self.assertEqual(harness.resolver(source).run(limit=1).claimed, 0)  # live lease
        harness.clock.advance(901)
        recovered = harness.resolver(source).run(limit=1)
        self.assertEqual((recovered.abandoned, recovered.claimed), (1, 0))
        abandoned = harness.attempts(request_id)[0]
        self.assertEqual(abandoned.outcome, "abandoned")
        assert abandoned.next_attempt_at is not None
        harness.clock.now = abandoned.next_attempt_at
        self.assertEqual(harness.resolver(source).run(limit=1).matched, 1)
        self.assertEqual([a.outcome for a in harness.attempts(request_id)], ["abandoned", "matched"])

    def test_stale_executor_result_cannot_overwrite_the_takeover(self) -> None:
        harness = _Harness()
        request_id = self._pending(harness, _title(date_from="2026-03-01", date_to="2026-03-01"))
        ref = _ref("1220000001", "2025年年度报告", date(2026, 3, 1))
        successor = harness.resolver(_Source([ref]))

        def stall_then_takeover(_index: int) -> None:
            # The executor stalls past its lease; a restarted worker reclaims
            # the request and completes a fresh attempt meanwhile.
            harness.clock.advance(901)
            self.assertEqual(successor.run(limit=1).abandoned, 1)
            reclaimed = harness.attempts(request_id)[0]
            assert reclaimed.next_attempt_at is not None
            harness.clock.now = reclaimed.next_attempt_at
            self.assertEqual(successor.run(limit=1).matched, 1)

        result = harness.resolver(_Source([ref], on_call=stall_then_takeover)).run(limit=1)
        self.assertEqual(result.abandoned, 1)
        attempts = harness.attempts(request_id)
        self.assertEqual([a.outcome for a in attempts], ["abandoned", "matched"])
        target = harness.target(request_id)
        assert target is not None
        self.assertEqual(target.attempt_id, attempts[1].attempt_id)

    def test_item_failures_end_in_terminal_failure(self) -> None:
        harness = _Harness()
        request_id = self._pending(harness, _title())
        error = SourceRequestError("bad", error_code="invalid_response_shape", retryable=True)
        for _ in range(MAX_ITEM_FAILURES):
            harness.resolver(_Source([], fail_on={0: error})).run(limit=1)
            latest = harness.attempts(request_id)[-1]
            if latest.next_attempt_at is not None:
                harness.clock.now = latest.next_attempt_at
        self.assertEqual(len(harness.attempts(request_id)), MAX_ITEM_FAILURES)
        self.assertEqual(harness.resolver(_Source([])).run(limit=1).claimed, 0)
        status = harness.service.get(request_id).status
        self.assertEqual((status["state"], status["terminal"]), ("resolution_failed", True))

    def test_status_after_resolution_reports_the_queue_answer(self) -> None:
        harness = _Harness()
        harness.reader._facts = lambda request, target: (  # type: ignore[attr-defined]
            {"download": DownloadFacts(selected=True)} if target is not None else {})
        request_id = self._pending(harness, _title())
        harness.resolver(_Source([_ref("1220000001", "2025年年度报告", date(2026, 3, 30))])).run(limit=1)
        status = harness.service.get(request_id).status
        self.assertEqual((status["state"], status["next_action"]), ("download_queued", "wait"))
        self.assertEqual(status["filing"]["provider_document_id"], "1220000001")


def _single_chunk_request(harness: _Harness) -> str:
    view = harness.submit(KEY_A, _title(date_from="2026-03-01", date_to="2026-03-01"))
    return str(view.request.request_id)


def _selection_accesses(harness: _Harness, request_id: str) -> list[Any]:
    return [
        access for access in harness.base.source_accesses.all()
        if isinstance(access.result_snapshot, dict)
        and isinstance(access.result_snapshot.get("selection"), dict)
        and access.result_snapshot["selection"].get("request_id") == request_id
    ]


class LeaseAuthorityTests(unittest.TestCase):
    """The final provider call cannot outlive the attempt's authority to conclude."""

    REF = _ref("1220000001", "2025年年度报告", date(2026, 3, 1))

    def test_stop_or_deadline_during_the_final_call_discards_the_observation(self) -> None:
        for case, code in (("stop", "worker_stop_requested"), ("deadline", "discovery_deadline_exceeded")):
            with self.subTest(case=case):
                harness = _Harness()
                request_id = _single_chunk_request(harness)
                stop = {"value": False}

                def interrupt(_index: int, case: str = case) -> None:
                    if case == "stop":
                        stop["value"] = True
                    else:
                        harness.mono = 301.0

                harness.resolver(_Source([self.REF], on_call=interrupt), stop=lambda: stop["value"],
                                 monotonic=lambda: harness.mono).run(limit=1)
                attempt = harness.attempts(request_id)[-1]
                assert attempt.error is not None
                self.assertEqual((attempt.outcome, attempt.error["error_code"]), ("deferred", code))
                self.assertEqual(attempt.error["coverage"], {"chunks_total": 1, "chunks_completed": 1})
                self.assertIsNone(harness.target(request_id))
                self.assertEqual(_selection_accesses(harness, request_id), [])
                resolution = harness.service.get(request_id).status["resolution"]
                self.assertEqual((resolution["state"], resolution["item_failure_count"]), ("pending", 0))

    def test_lease_expiring_during_the_final_call_binds_nothing_then_recovers(self) -> None:
        harness = _Harness()
        request_id = _single_chunk_request(harness)
        result = harness.resolver(
            _Source([self.REF], on_call=lambda _index: harness.clock.advance(901))).run(limit=1)
        self.assertEqual(result.abandoned, 1)
        abandoned = harness.attempts(request_id)[-1]
        assert abandoned.error is not None and abandoned.finished_at is not None
        self.assertEqual((abandoned.outcome, abandoned.error["error_code"]),
                         ("abandoned", "attempt_lease_expired"))
        self.assertGreaterEqual(abandoned.finished_at, abandoned.lease_expires_at)
        self.assertIsNone(harness.target(request_id))
        self.assertEqual(_selection_accesses(harness, request_id), [])
        self.assertEqual(harness.service.get(request_id).status["resolution"]["item_failure_count"], 1)
        assert abandoned.next_attempt_at is not None
        harness.clock.now = abandoned.next_attempt_at
        self.assertEqual(harness.resolver(_Source([self.REF])).run(limit=1).matched, 1)
        target = harness.target(request_id)
        assert target is not None
        self.assertEqual((target.admit_download, target.admit_parse), (True, True))

    def test_lease_ending_inside_the_recording_transaction_commits_nothing(self) -> None:
        harness = _Harness()
        request_id = _single_chunk_request(harness)
        accesses = harness.base.source_accesses
        add = accesses.add

        def add_then_expire(item: Any) -> Any:
            stored = add(item)
            if isinstance(item.result_snapshot, dict) and "selection" in item.result_snapshot:
                harness.clock.advance(901)  # the guarded finish now sees an ended lease
            return stored

        commits = harness.store.commits
        with mock.patch.object(accesses, "add", side_effect=add_then_expire):
            result = harness.resolver(_Source([self.REF])).run(limit=1)
        self.assertEqual(result.abandoned, 1)
        # Claim and abandonment commit; the discovery transaction never does.
        self.assertEqual(harness.store.commits - commits, 2)
        self.assertEqual(harness.attempts(request_id)[-1].outcome, "abandoned")
        self.assertIsNone(harness.target(request_id))


class LateLocalDocumentTests(unittest.TestCase):
    """A local row holding the matched id binds only if it is the requested filing.

    The row is looked up when the result is recorded, so one that arrived
    during the provider call is judged exactly like one that already existed.
    """

    def _resolve_with_arrival(self, document: Any) -> tuple[_Harness, str]:
        harness = _Harness()
        request_id = _single_chunk_request(harness)

        def arrive(_index: int) -> None:
            harness.store.documents[document.document_id] = document

        source = _Source([_ref("1220000001", "2025年年度报告", date(2026, 3, 1))], on_call=arrive)
        harness.resolver(source).run(limit=1)
        return harness, request_id

    def test_equivalent_local_row_is_bound_as_the_document_not_a_download(self) -> None:
        harness, request_id = self._resolve_with_arrival(
            local_document("doc_late", announcement_date=date(2026, 3, 1)))
        target = harness.target(request_id)
        assert target is not None
        self.assertEqual(
            (target.resolution_method, target.document_id, target.admit_download, target.admit_parse),
            ("provider_discovery", "doc_late", False, True))

    def test_contradicting_local_row_fails_the_resolution_and_admits_nothing(self) -> None:
        on_day = local_document("doc_late", announcement_date=date(2026, 3, 1))
        cases = {
            "other security": (replace(on_day, security_code="600520"), "local_filing_mismatch"),
            "other exchange": (replace(on_day, exchange="SZSE"), "local_filing_mismatch"),
            "outside the window": (replace(on_day, announcement_date=date(2026, 3, 30)),
                                   "local_filing_mismatch"),
            "no archived original": (replace(on_day, raw_file_hash=None), "local_original_missing"),
        }
        for name, (document, code) in cases.items():
            with self.subTest(case=name):
                harness, request_id = self._resolve_with_arrival(document)
                self.assertIsNone(harness.target(request_id))
                attempt = harness.attempts(request_id)[-1]
                assert attempt.error is not None
                self.assertEqual((attempt.outcome, attempt.error["error_code"], attempt.error["retryable"]),
                                 ("failed", code, False))
                self.assertEqual(attempt.error["found"]["document_id"], "doc_late")
                self.assertEqual((attempt.error["observed"]["provider_document_id"],
                                  attempt.error["observed"]["security_code"]), ("1220000001", "600519"))
                index = harness.base.source_accesses.get(str(attempt.index_source_access_id))
                assert index is not None and index.result_snapshot is not None
                self.assertEqual(index.result_snapshot["candidates"], [])
                self.assertEqual(index.result_snapshot["selection"]["local_conflict"]["error_code"], code)
                status = harness.service.get(request_id).status
                self.assertEqual((status["state"], status["terminal"]), ("resolution_failed", True))
                self.assertEqual(status["resolution"]["last_attempt"]["error_code"], code)


_SHANGHAI = timezone(timedelta(hours=8))


def _web_record(pdid: str, title: str, day: date) -> dict[str, object]:
    return {
        "announcementId": pdid, "announcementTitle": title,
        "adjunctUrl": f"finalpage/{day.isoformat()}/{pdid}.PDF", "adjunctSize": 120,
        "secCode": "600519", "secName": "贵州茅台", "orgId": "gssh0600519",
        "announcementTime": int(datetime(day.year, day.month, day.day, tzinfo=_SHANGHAI).timestamp() * 1000),
    }


class WebChannelResolutionTests(unittest.TestCase):
    """Title resolution over the website channel's own pagination; only HTTP is faked."""

    ANNUAL = _web_record("1220000001", "贵州茅台2025年年度报告", date(2026, 3, 30))
    OTHER = _web_record("1220000002", "贵州茅台关于分红的公告", date(2026, 3, 15))

    def _resolve(self, pages: list[dict[str, object]]) -> tuple[_Harness, str, ResolverRunResult]:
        harness = _Harness()
        view = harness.submit(KEY_A, _title(date_from="2026-03-01", date_to="2026-03-30"))
        served = iter(pages)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/data/szse_stock.json"):
                return httpx.Response(200, json={"stockList": [{"code": "600519", "orgId": "gssh0600519"}]})
            return httpx.Response(200, json=next(served))

        web = CninfoWebSource(transport=httpx.MockTransport(handler), sleep=lambda _: None)
        try:
            result = ProcessingRequestResolver(
                uow_factory=harness.factory, source=web, profile_loader=_profile,
                index_interface="cninfo:p_info3015",
                classify_failure=worker_module._classify_discovery_failure, clock=harness.clock,
            ).run(limit=1)
        finally:
            web.close()
        return harness, str(view.request.request_id), result

    def test_complete_listings_conclude(self) -> None:
        harness, request_id, result = self._resolve(
            [{"announcements": [self.ANNUAL, self.OTHER], "hasMore": False}])
        target = harness.target(request_id)
        assert target is not None
        self.assertEqual((result.matched, target.provider_document_id, target.admit_download),
                         (1, "1220000001", True))
        harness, request_id, _ = self._resolve([{"announcements": None, "hasMore": False}])
        self.assertEqual(harness.service.get(request_id).status["resolution"]["state"], "not_found")

    def test_drifted_or_truncated_listing_never_concludes(self) -> None:
        cases = {
            "announcements not an array": ([{"announcements": "shape drift", "hasMore": True}],
                                           "invalid_response_shape"),
            "no hasMore flag": ([{"announcements": [self.ANNUAL]}], "invalid_response_shape"),
            "empty page promising more": ([{"announcements": [self.ANNUAL], "hasMore": True},
                                           {"announcements": [], "hasMore": True}], "incomplete_response"),
            "repeated page promising more": ([{"announcements": [self.ANNUAL], "hasMore": True},
                                              {"announcements": [self.ANNUAL], "hasMore": True}],
                                             "incomplete_response"),
        }
        for name, (pages, code) in cases.items():
            with self.subTest(case=name):
                harness, request_id, result = self._resolve(pages)
                self.assertIsNone(harness.target(request_id))
                attempt = harness.attempts(request_id)[-1]
                assert attempt.error is not None
                # The existing provider-infrastructure classification: a deferral
                # that cools the source down, never a not_found or a match.
                self.assertEqual((attempt.outcome, attempt.error["error_code"], result.provider_refusal),
                                 ("deferred", code, "outage"))
                resolution = harness.service.get(request_id).status["resolution"]
                self.assertEqual((resolution["state"], resolution["item_failure_count"]), ("pending", 0))


class _DueRepo:
    def __init__(self, due: list[RequestRecord]) -> None:
        self.due = due

    def due_discovery_requests(self, *, now: datetime, limit: int) -> list[RequestRecord]:
        return self.due[:limit]


class _DueUow:
    def __init__(self, repo: _DueRepo) -> None:
        self.processing_requests = repo

    def __enter__(self) -> _DueUow:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _worker_deps(due: list[RequestRecord], source_factory: Any) -> WorkerDeps:
    engine = mock.MagicMock()
    return WorkerDeps(
        engine=engine,
        uow_factory=lambda: mock.MagicMock(),
        path_builder=mock.MagicMock(),
        raw_store=mock.MagicMock(),
        artifact_store=mock.MagicMock(),
        provider_source=mock.MagicMock(),
        source_factory=source_factory,
        profile_loader_factory=lambda source: lambda code: None,
        parser_factory=lambda: mock.MagicMock(),
        semantic_router=mock.MagicMock(),
        semantic_receipts=mock.MagicMock(),
        parse_expected_seconds=1800,
        config=WorkerConfig(
            max_parse_retries=3, max_build_retries=3, stale_run_threshold_seconds=3600,
            sync_interval_seconds=86400, cninfo_overlap_days=7, cninfo_max_retries=3,
            cninfo_oversized_kb=10240,
        ),
        parser_options=ParserOptions(runtime_bundle_identity_sha256="sha256:" + "b" * 64),
        clock=lambda: START,
        processing_request_uow_factory=lambda: _DueUow(_DueRepo(due)),  # type: ignore[arg-type,return-value]
    )


class WorkerDiscoveryStageTests(unittest.TestCase):
    LIMITS = WorkerLimits(sync=0, download=0, parse=0, build=0, publish=0, request_discovery=2)

    def _due(self) -> list[RequestRecord]:
        return [RequestRecord(request_id="dreq_" + "1" * 26, idempotency_key=KEY_A,
                              intent_sha256="sha256:" + "0" * 64, target=_title(),
                              enqueue_parse=True, created_at=START)]

    def test_maintenance_round_resolves_and_maps_refusal_to_existing_cooldown(self) -> None:
        sources: list[object] = []
        deps = _worker_deps(self._due(), lambda: sources.append(object()) or sources[-1])
        result = ResolverRunResult(claimed=1, matched=1, provider_refusal="rate_limited")
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module, "ProcessingRequestResolver") as resolver_cls,
        ):
            resolver_cls.return_value.run.return_value = result
            report = run_once(self.LIMITS, deps)
        resolver_cls.return_value.run.assert_called_once_with(limit=2)
        self.assertEqual(resolver_cls.call_args.kwargs["index_interface"], "cninfo:p_info3015")
        self.assertEqual((report.request_discovery_claimed, report.requests_resolved), (1, 1))
        self.assertTrue(report.sync_rate_limited)
        self.assertEqual(len(sources), 1)

    def test_outage_refusal_stops_the_downloads_of_that_pass(self) -> None:
        deps = _worker_deps(self._due(), lambda: mock.MagicMock())
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module.queries, "pending_downloads") as pending,
            mock.patch.object(worker_module, "ProcessingRequestResolver") as resolver_cls,
        ):
            resolver_cls.return_value.run.return_value = ResolverRunResult(
                claimed=1, deferred=1, provider_refusal="outage")
            report = run_once(replace_limits(self.LIMITS, download=5), deps)
        self.assertTrue(report.source_outage_break)
        pending.assert_not_called()

    def test_no_due_request_or_stop_creates_no_provider_client(self) -> None:
        factory = mock.MagicMock()
        deps = _worker_deps([], factory)
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module, "ProcessingRequestResolver") as resolver_cls,
        ):
            run_once(self.LIMITS, deps)
            run_once(self.LIMITS, _worker_deps(self._due(), factory), should_stop=lambda: True)
        factory.assert_not_called()
        resolver_cls.assert_not_called()


def replace_limits(limits: WorkerLimits, **changes: int) -> WorkerLimits:
    from dataclasses import replace

    return replace(limits, **changes)


class StatusSerializationTests(unittest.TestCase):
    def test_resolution_fields_are_additive_and_hash_stable(self) -> None:
        harness = _Harness()
        view = harness.submit(KEY_A, _title())
        body = view.to_json()
        self.assertEqual(body["contract_version"], "disclosure-processing-request.v1")
        self.assertEqual(set(body["status"]["resolution"]) >= {"item_failure_count", "max_item_failures",
                                                              "next_attempt_at"}, True)
        again = harness.service.get(view.request.request_id)
        self.assertEqual(again.status["status_sha256"], view.status["status_sha256"])
        self.assertNotIn("replayed", again.to_json())
        facts = harness.reader.read(view.request.request_id, observed_at=START)
        assert facts is not None
        self.assertEqual(derive_status(facts)["status_sha256"], view.status["status_sha256"])


if __name__ == "__main__":
    unittest.main()
