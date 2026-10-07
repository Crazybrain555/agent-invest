from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import unittest

from disclosure_anchor.application.contracts.processing_request import (
    MAX_ATTEMPTS,
    MAX_ITEM_FAILURES,
    AttemptRecord,
    DocumentFacts,
    DownloadFacts,
    FilingFacts,
    ProcessingFacts,
    ProcessingRequestInvalid,
    RequestRecord,
    TargetRecord,
    derive_status,
    discovery_due,
    next_attempt_after,
    normalize_intent,
    title_matches,
    window_chunks,
)

TODAY = date(2026, 10, 7)
NOW = datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)
KEY = "agent-01K7ZQ3M8V2X4N6P9R1T3W5Y7A"


def _body(target: dict[str, object], **extra: object) -> dict[str, object]:
    return {"idempotency_key": KEY, "target": target, **extra}


def _title_target(**overrides: object) -> dict[str, object]:
    target: dict[str, object] = {
        "kind": "title_match",
        "security_code": "600519",
        "exchange": "SSE",
        "date_from": "2026-03-01",
        "date_to": "2026-04-30",
        "title_contains": " 2025年 年度报告 ",
    }
    target.update(overrides)
    return target


def _attempt(
    number: int,
    *,
    outcome: str | None = None,
    started: datetime = NOW - timedelta(minutes=30),
    lease: timedelta = timedelta(seconds=900),
    next_attempt_at: datetime | None = None,
    error: dict[str, object] | None = None,
    choices: tuple[dict[str, object], ...] | None = None,
) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=f"dpra_{number:026d}",
        attempt_no=number,
        started_at=started,
        lease_expires_at=started + lease,
        finished_at=None if outcome is None else started + timedelta(seconds=5),
        outcome=outcome,
        next_attempt_at=next_attempt_at,
        error=error,
        choices=choices,
        provider_row_count=None if outcome is None else 7,
    )


def _request(*, enqueue_parse: bool = True, kind: str = "title_match") -> RequestRecord:
    return RequestRecord(
        request_id="dreq_01K7ZQ4B1C2D3E4F5G6H7J8K9M",
        idempotency_key=KEY,
        intent_sha256="sha256:" + "0" * 64,
        target={"kind": kind},
        enqueue_parse=enqueue_parse,
        created_at=NOW - timedelta(hours=1),
    )


def _filing() -> FilingFacts:
    return FilingFacts(
        provider="cninfo",
        provider_document_id="1220000001",
        security_code="600519",
        exchange="SSE",
        title="2025年年度报告",
        announcement_date=date(2026, 3, 30),
    )


def _candidate_target(**overrides: object) -> TargetRecord:
    values: dict[str, object] = {
        "target_id": "dprt_" + "1" * 26,
        "resolution_method": "provider_discovery",
        "admit_download": True,
        "admit_parse": True,
        "attempt_id": "dpra_" + "0" * 25 + "1",
        "provider": "cninfo",
        "provider_document_id": "1220000001",
        "index_source_access_id": "sa_index",
    }
    values.update(overrides)
    return TargetRecord(**values)  # type: ignore[arg-type]


def _document(**overrides: object) -> DocumentFacts:
    values: dict[str, object] = {
        "document_id": "doc_01M4B45TP76AY533REBBYHCBGX",
        "status": "registered",
        "raw_file_hash": "sha256:" + "a" * 64,
        "filing": _filing(),
        "retry": {
            "item_failure_count": 0,
            "charged_failure_count": 0,
            "max_item_failures": 3,
            "max_charged_failures": 15,
            "latest_failed_run_id": None,
            "latest_failed_run_retryable": None,
            "latest_failed_run_released": False,
            "unreleased_contract_failures": 0,
        },
    }
    values.update(overrides)
    return DocumentFacts(**values)  # type: ignore[arg-type]


def _status(**facts: object) -> dict[str, object]:
    values: dict[str, object] = {
        "request": _request(),
        "attempts": (),
        "target": None,
        "observed_at": NOW,
        "max_download_retries": 3,
    }
    values.update(facts)
    return derive_status(ProcessingFacts(**values))  # type: ignore[arg-type]


class NormalizeIntentTests(unittest.TestCase):
    def test_kinds_normalize_and_key_is_not_part_of_the_intent(self) -> None:
        intent = normalize_intent(_body(_title_target()), today=TODAY)
        self.assertTrue(intent.enqueue_parse)
        self.assertEqual(intent.target.to_json()["title_contains"], "2025年 年度报告")
        other_key = normalize_intent(
            {**_body(_title_target()), "idempotency_key": "agent-another-key-000001"},
            today=TODAY,
        )
        self.assertEqual(intent.intent_sha256, other_key.intent_sha256)
        changed = normalize_intent(_body(_title_target(), enqueue_parse=False), today=TODAY)
        self.assertNotEqual(intent.intent_sha256, changed.intent_sha256)
        document = normalize_intent(
            _body({"kind": "document", "document_id": "doc_01M4B45TP76AY533REBBYHCBGX"}),
            today=TODAY,
        )
        self.assertEqual(document.target.kind, "document")
        provider = normalize_intent(
            _body(
                {
                    "kind": "provider_document",
                    "security_code": "000001",
                    "exchange": "SZSE",
                    "date_from": "2026-01-01",
                    "date_to": "2026-12-31",
                    "provider_document_id": "1224567890",
                }
            ),
            today=date(2027, 1, 1),
        )
        self.assertEqual(provider.target.to_json()["date_to"], "2026-12-31")

    def test_rejections_name_the_field_and_store_nothing(self) -> None:
        cases = [
            ({**_body(_title_target()), "idempotency_key": "short"}, "idempotency_key"),
            ({**_body(_title_target()), "extra": 1}, "extra"),
            (_body(_title_target(exchange="SZSE")), "target.exchange"),
            (_body(_title_target(exchange="HKEX")), "target.exchange"),
            (_body(_title_target(security_code="60051")), "target.security_code"),
            (_body(_title_target(date_from="2025-01-01", date_to="2026-01-02")), "target.date_to"),
            (_body(_title_target(date_to="2026-10-08")), "target.date_to"),
            (_body(_title_target(date_from="2026-05-01")), "target.date_from"),
            (_body(_title_target(title_contains=" x ")), "target.title_contains"),
            (_body(_title_target(provider_document_id="1")), "target.provider_document_id"),
            (_body({"kind": "url", "url": "https://example.invalid"}), "target.kind"),
            (_body({"kind": "document", "document_id": "../etc/passwd"}), "target.document_id"),
            (_body(_title_target(), enqueue_parse="yes"), "enqueue_parse"),
        ]
        for payload, field_name in cases:
            with self.subTest(field=field_name), self.assertRaises(ProcessingRequestInvalid) as raised:
                normalize_intent(payload, today=TODAY)
            self.assertEqual(raised.exception.field_name, field_name)

    def test_title_match_ignores_whitespace_width_and_case(self) -> None:
        self.assertTrue(title_matches("贵州茅台2025年年度报告（ＡＢＣ）", "2025年 年度报告(abc)"))
        self.assertFalse(title_matches("2025年年度报告摘要", "2024年年度报告"))


class AttemptScheduleTests(unittest.TestCase):
    def test_window_chunks_cover_the_window_exactly(self) -> None:
        chunks = window_chunks(date(2026, 1, 1), date(2026, 3, 5), 30)
        self.assertEqual(chunks[0], (date(2026, 1, 1), date(2026, 1, 30)))
        self.assertEqual(chunks[-1][1], date(2026, 3, 5))
        for (_, end), (start, _) in zip(chunks, chunks[1:]):
            self.assertEqual(start, end + timedelta(days=1))
        self.assertEqual(window_chunks(date(2026, 1, 1), date(2026, 1, 1), 30), [(date(2026, 1, 1), date(2026, 1, 1))])

    def test_item_failures_are_bounded_while_deferrals_spend_only_the_total(self) -> None:
        failed = [_attempt(n, outcome="failed") for n in range(1, MAX_ITEM_FAILURES + 1)]
        self.assertIsNone(
            next_attempt_after(failed, outcome="failed", retryable=True, finished_at=NOW)
        )
        two_failed = failed[:-1]
        scheduled = next_attempt_after(two_failed, outcome="failed", retryable=True, finished_at=NOW)
        self.assertIsNotNone(scheduled)
        assert scheduled is not None
        self.assertGreater(scheduled, NOW)
        self.assertIsNone(
            next_attempt_after(two_failed, outcome="failed", retryable=False, finished_at=NOW)
        )
        deferred = [_attempt(n, outcome="deferred") for n in range(1, MAX_ATTEMPTS)]
        self.assertIsNotNone(
            next_attempt_after(deferred, outcome="deferred", retryable=True, finished_at=NOW)
        )
        deferred.append(_attempt(MAX_ATTEMPTS, outcome="deferred"))
        self.assertIsNone(
            next_attempt_after(deferred, outcome="deferred", retryable=True, finished_at=NOW)
        )
        self.assertIsNone(
            next_attempt_after([_attempt(1, outcome="not_found")], outcome="not_found",
                               retryable=True, finished_at=NOW)
        )

    def test_due_after_lease_expiry_or_schedule_only(self) -> None:
        self.assertTrue(discovery_due([], now=NOW))
        live = _attempt(1, started=NOW - timedelta(seconds=10))
        self.assertFalse(discovery_due([live], now=NOW))
        expired = _attempt(1, started=NOW - timedelta(seconds=901))
        self.assertTrue(discovery_due([expired], now=NOW))
        later = _attempt(1, outcome="failed", next_attempt_at=NOW + timedelta(minutes=1))
        self.assertFalse(discovery_due([later], now=NOW))
        self.assertTrue(discovery_due([later], now=NOW + timedelta(minutes=1)))
        terminal = _attempt(1, outcome="failed", next_attempt_at=None)
        self.assertFalse(discovery_due([terminal], now=NOW))


class DeriveStatusTests(unittest.TestCase):
    def test_unresolved_states_always_wait_for_the_worker(self) -> None:
        pending = _status()
        self.assertEqual((pending["state"], pending["terminal"], pending["next_action"]),
                         ("resolving", False, "wait"))
        self.assertEqual(pending["resolution"]["state"], "pending")  # type: ignore[index]
        live = _status(attempts=(_attempt(1, started=NOW - timedelta(seconds=10)),))
        self.assertEqual(live["resolution"]["state"], "discovering")  # type: ignore[index]
        expired = _status(attempts=(_attempt(1, started=NOW - timedelta(hours=1)),))
        self.assertEqual(expired["resolution"]["state"], "pending")  # type: ignore[index]
        retry_at = NOW + timedelta(minutes=5)
        scheduled = _status(
            attempts=(_attempt(1, outcome="deferred", next_attempt_at=retry_at,
                               error={"error_code": "rate_limited", "retryable": True}),)
        )
        resolution = scheduled["resolution"]
        assert isinstance(resolution, dict)
        self.assertEqual(resolution["state"], "pending")
        self.assertEqual(resolution["next_attempt_at"], retry_at.isoformat())
        self.assertEqual(resolution["item_failure_count"], 0)
        self.assertEqual(scheduled["next_action"], "wait")
        for status in (pending, live, expired, scheduled):
            self.assertNotEqual(status["next_action"], "retry_same_request")

    def test_terminal_resolution_outcomes(self) -> None:
        choices = ({"provider_document_id": "1", "title": "A", "announcement_date": "2026-03-30",
                    "security_code": "600519", "exchange": "SSE", "document_id": None},)
        ambiguous = _status(attempts=(_attempt(1, outcome="ambiguous", choices=choices),))
        self.assertEqual((ambiguous["state"], ambiguous["terminal"], ambiguous["next_action"]),
                         ("needs_selection", True, "choose_and_submit_new_request"))
        self.assertEqual(len(ambiguous["resolution"]["choices"]), 1)  # type: ignore[index]
        not_found = _status(attempts=(_attempt(1, outcome="not_found"),))
        self.assertEqual((not_found["state"], not_found["terminal"]), ("not_found", True))
        exhausted = _status(attempts=tuple(_attempt(n, outcome="failed") for n in (1, 2, 3)))
        self.assertEqual((exhausted["state"], exhausted["terminal"]), ("resolution_failed", True))

    def test_original_and_download_states(self) -> None:
        queued = _status(target=_candidate_target(), download=DownloadFacts(selected=True))
        self.assertEqual((queued["state"], queued["next_action"]), ("download_queued", "wait"))
        self.assertEqual(queued["parse"]["state"], "waiting_for_original")  # type: ignore[index]
        exhausted = _status(
            target=_candidate_target(),
            download=DownloadFacts(selected=False, failed_download_count=3),
        )
        self.assertEqual(exhausted["state"], "download_blocked")
        self.assertEqual(exhausted["original"]["blocked_reason"], "download_retry_exhausted")  # type: ignore[index]
        latched = _status(
            target=_candidate_target(),
            download=DownloadFacts(selected=False, failed_download_count=1,
                                   nonretryable_unresolved=True,
                                   last_failure={"error_code": "http_404", "retryable": False,
                                                 "accessed_at": NOW.isoformat()}),
        )
        self.assertEqual(latched["original"]["blocked_reason"], "download_nonretryable_failure")  # type: ignore[index]
        archived = _status(target=_candidate_target(), document=_document(parse_selected=True))
        original = archived["original"]
        assert isinstance(original, dict)
        self.assertEqual(original["state"], "archived")
        self.assertEqual(original["text_path"], "/v1/documents/doc_01M4B45TP76AY533REBBYHCBGX/text")
        self.assertNotIn("text_ready", original)

    def test_parse_states_follow_the_real_queue_facts(self) -> None:
        cases = [
            (_document(status="published", current_processing_run_id="prun_1"), "published", True),
            (_document(running_processing_run_id="prun_2"), "parse_running", False),
            (_document(status="parsed", post_parse_selected=True), "post_processing", False),
            (_document(parse_selected=True), "parse_queued", False),
        ]
        for document, state, terminal in cases:
            with self.subTest(state=state):
                status = _status(target=_candidate_target(), document=document)
                self.assertEqual((status["state"], status["terminal"]), (state, terminal))
        stuck = _status(target=_candidate_target(), document=_document(status="parsed"))
        self.assertEqual(stuck["parse"]["blocked_reasons"], ["post_processing_not_selected"])  # type: ignore[index]

    def test_blocked_parse_reports_the_retry_gate_never_resets_it(self) -> None:
        retry = {
            "item_failure_count": 3, "charged_failure_count": 3, "max_item_failures": 3,
            "max_charged_failures": 15, "latest_failed_run_id": "prun_f",
            "latest_failed_run_retryable": False, "latest_failed_run_released": False,
            "unreleased_contract_failures": 1,
        }
        status = _status(target=_candidate_target(), document=_document(retry=retry))
        self.assertEqual((status["state"], status["next_action"]),
                         ("parse_blocked", "operator_action_required"))
        self.assertEqual(
            status["parse"]["blocked_reasons"],  # type: ignore[index]
            ["latest_failure_nonretryable", "unreleased_contract_failure",
             "item_retry_budget_exhausted"],
        )

    def test_out_of_scope_parse_is_production_policy_not_a_retry_failure(self) -> None:
        status = _status(target=_candidate_target(), document=_document(outside_parse_scope=True))
        self.assertEqual((status["state"], status["terminal"], status["next_action"]),
                         ("parse_blocked", True, "none"))
        parse = status["parse"]
        assert isinstance(parse, dict)
        self.assertEqual(parse["blocked_reasons"], ["outside_parse_scope"])
        # Retry facts are reported as they are; nothing is claimed exhausted.
        self.assertEqual((parse["retry"]["item_failure_count"], parse["retry"]["latest_failed_run_id"]),
                         (0, None))
        original = status["original"]
        assert isinstance(original, dict)
        self.assertEqual((original["state"], original["text_path"]),
                         ("archived", "/v1/documents/doc_01M4B45TP76AY533REBBYHCBGX/text"))
        # Only an unselected, requested parse reads the flag.
        for document, state in ((_document(outside_parse_scope=True, parse_selected=True), "parse_queued"),
                                (_document(outside_parse_scope=True, status="published"), "published")):
            with self.subTest(state=state):
                self.assertEqual(_status(target=_candidate_target(), document=document)["state"], state)
        download_only = _status(request=_request(enqueue_parse=False),
                                target=_candidate_target(admit_parse=False),
                                document=_document(outside_parse_scope=True))
        self.assertEqual((download_only["state"], download_only["parse"]["state"]),  # type: ignore[index]
                         ("archived", "not_requested"))

    def test_not_requested_parse_ends_at_archived(self) -> None:
        status = _status(request=_request(enqueue_parse=False),
                         target=_candidate_target(admit_parse=False), document=_document())
        self.assertEqual((status["state"], status["terminal"]), ("archived", True))
        self.assertEqual(status["parse"]["state"], "not_requested")  # type: ignore[index]

    def test_status_hash_ignores_only_the_observation_time(self) -> None:
        first = _status()
        later = derive_status(ProcessingFacts(
            request=_request(), attempts=(), target=None,
            observed_at=NOW + timedelta(seconds=3), max_download_retries=3,
        ))
        self.assertEqual(first["status_sha256"], later["status_sha256"])
        changed = _status(attempts=(_attempt(1, started=NOW - timedelta(seconds=1)),))
        self.assertNotEqual(first["status_sha256"], changed["status_sha256"])


if __name__ == "__main__":
    unittest.main()
