"""Worker scheduling shell: scan queues, call use cases, report (08 §3).

run_once contains no business logic — every action is an existing use case
(07 sync/download, 04 parse, 05 build/publish). Exception isolation is per
item: one bad company/candidate/document lands in the failure list and the
round continues.
"""

from __future__ import annotations

import threading
import time

from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine

from disclosure_anchor.application.dto.worker_report import (
    WorkerFailure,
    WorkerLimits,
    WorkerReport,
)
from disclosure_anchor.application.ports.disclosure_source import (
    DisclosureSourcePort,
    SourceCompanyProfile,
)
from disclosure_anchor.application.ports.file_store import (
    ArtifactStorePort,
    FileStorePathPort,
    RawDocumentStorePort,
)
from disclosure_anchor.application.ports.parser import DocumentParserPort, ParserOptions
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.use_cases.build_search_projection import (
    BuildSearchProjection,
    BuildSearchProjectionCommand,
)
from disclosure_anchor.application.use_cases.build_units import (
    BuildUnits,
    BuildUnitsCommand,
)
from disclosure_anchor.application.use_cases.download_document import (
    DownloadDocument,
    DownloadDocumentCommand,
)
from disclosure_anchor.application.use_cases.parse_document import (
    ParseDocument,
    ParseDocumentCommand,
)
from disclosure_anchor.application.use_cases.publish_run import (
    PublishRun,
    PublishRunCommand,
)
from disclosure_anchor.application.use_cases.sync_disclosure_index import (
    SyncDisclosureIndex,
    SyncDisclosureIndexCommand,
)
from disclosure_anchor.application.worker import queries
from disclosure_anchor.application.worker.concurrency import (
    AdaptiveConcurrencyLimit,
)
from disclosure_anchor.application.worker.locks import (  # noqa: F401  (re-export, 08 §2)
    DOC_NS,
    WORKER_NS,
    stable_document_hash,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import (
    DisclosureAnchorError,
    ParserVersionProbeError,
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PARSER_INFRASTRUCTURE_ERROR_CODES = frozenset(
    {
        "parse_timeout",
        "parser_timeout",
        "parser_invocation_failed",
        "ParserInvocationError",
        "ParserTimeoutError",
        "parser_version_probe_failed",
    }
)
PROVIDER_INFRASTRUCTURE_ERROR_CODES = frozenset(
    {
        "transport_error",
        "non_json_response",
        "invalid_response_shape",
        "incomplete_response",
        "stock_list_unavailable",
        "resultcode_-1",
        "resultcode_403",
        "resultcode_404",
        "resultcode_405",
    }
)
BUILD_INFRASTRUCTURE_ERROR_CODES = frozenset(
    {
        "ARTIFACT_WRITE_FAILED",
        "DB_WRITE_FAILED",
        "DatabaseError",
        "InterfaceError",
        "OSError",
        "OperationalError",
    }
)
BUILD_ITEM_LOCAL_ERROR_CODES = frozenset(
    {
        "IR_CONTRACT_TOO_OLD",
        "IR_MISSING",
        "RUN_NOT_FOUND",
        "RUN_NOT_SUCCEEDED",
        "UNITS_ALREADY_BUILT",
    }
)


@dataclass(frozen=True)
class WorkerConfig:
    """Settings-derived thresholds and knobs run_once needs (08 §1)."""

    max_parse_retries: int
    max_build_retries: int
    stale_run_threshold_seconds: int
    sync_interval_seconds: int
    cninfo_overlap_days: int
    cninfo_max_retries: int
    cninfo_oversized_kb: int
    # First-sync historical backfill (user decision: 三年是底线).
    initial_lookback_days: int = 1095
    # Backpressure cap for pending-download + downloaded/pending-parse work;
    # 0 disables first-sync deferral.  The env key retains its legacy name.
    backfill_max_pending_downloads: int = 2000
    # None → parse everything; a topic tuple → 'other' docs need a matching
    # disclosure_topic (parse scope 'core', round9).
    process_scope_classes: tuple[str, ...] | None = None
    # Bounded per-round parallelism for the parse chain (1 = serial legacy).
    # Raise with the *-http-client backends: local work is HTTP waiting and
    # the GPU server batches requests; keep small for local CPU backends.
    parse_concurrency: int = 1


@dataclass(frozen=True)
class WorkerDeps:
    """Injected dependencies; production wiring lives in cli/worker.py."""

    engine: Engine
    uow_factory: Callable[[], UnitOfWork]
    path_builder: FileStorePathPort
    raw_store: RawDocumentStorePort
    artifact_store: ArtifactStorePort
    source_factory: Callable[[], DisclosureSourcePort]
    profile_loader_factory: Callable[
        [DisclosureSourcePort], Callable[[str], SourceCompanyProfile | None]
    ]
    parser_factory: Callable[[], DocumentParserPort]
    parse_timeout_seconds: int
    config: WorkerConfig
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    # Settings-driven parse defaults (backend/server_url cascade) — the CLI
    # builds this from env so GPU offload is a config flip, not a code change.
    parser_options: ParserOptions = ParserOptions()
    # Loop-lifetime adaptive parse concurrency; None gives each stage call a
    # fresh limiter opened at the configured bound (fixed-limit behavior).
    parse_limiter: AdaptiveConcurrencyLimit | None = None
    # CLI resident mode owns one lazy CNINFO client for the process lifetime,
    # preserving both its token cache and 1-QPS bucket across zero-wait rounds.
    # Tests/other callers retain the legacy per-round close by default.
    source_close_after_round: bool = True
    close_source: Callable[[], None] = lambda: None


def _merge_acquisition_report(
    report: WorkerReport, acquisition: WorkerReport
) -> None:
    """Fold the acquisition thread's sub-report into the round report."""

    report.synced_companies += acquisition.synced_companies
    report.candidates_discovered += acquisition.candidates_discovered
    report.downloaded += acquisition.downloaded
    report.deferred_backfill += acquisition.deferred_backfill
    report.skipped_oversized += acquisition.skipped_oversized
    report.failed += acquisition.failed
    report.sync_quota_break = (
        report.sync_quota_break or acquisition.sync_quota_break
    )
    report.sync_rate_limited = (
        report.sync_rate_limited or acquisition.sync_rate_limited
    )
    report.source_outage_break = (
        report.source_outage_break or acquisition.source_outage_break
    )
    report.failures.extend(acquisition.failures)


def run_once(
    limits: WorkerLimits,
    deps: WorkerDeps,
    *,
    should_stop: Callable[[], bool] = lambda: False,
) -> WorkerReport:
    started_at = deps.clock()
    report = WorkerReport(started_at=started_at)

    with deps.engine.begin() as conn:
        report.stale_reclaimed = queries.reclaim_stale_runs(
            conn, threshold_seconds=deps.config.stale_run_threshold_seconds
        )

    # Acquisition (sync -> download) runs beside the parse stage: they share
    # no mutable state except the DB (disjoint documents) — acquisition
    # writes its own sub-report, merged deterministically after join, so the
    # GPU no longer idles while the provider stages run.  Build/publish stay
    # sequential after both: they consume this round's parse outputs.
    acquisition = WorkerReport(started_at=started_at)

    def _acquisition_stages() -> None:
        source: DisclosureSourcePort | None = None
        try:
            try:
                if limits.sync > 0 and not should_stop():
                    source = _sync_stage(
                        acquisition,
                        deps,
                        source,
                        limit=limits.sync,
                        stage_seconds=limits.sync_stage_seconds,
                        should_stop=should_stop,
                    )
                if (
                    limits.download > 0
                    and not acquisition.sync_quota_break
                    and not acquisition.source_outage_break
                    and not should_stop()
                ):
                    source = _download_stage(
                        acquisition,
                        deps,
                        source,
                        limit=limits.download,
                        should_stop=should_stop,
                    )
            except DisclosureAnchorError as exc:
                # Provider family (credentials, CNINFO HTTP, token): pause
                # acquisition this round; parse/build/publish continue.
                acquisition.failed += 1
                acquisition.source_outage_break = True
                acquisition.failures.append(
                    WorkerFailure(
                        stage="source",
                        item_ref="cninfo",
                        error_code=type(exc).__name__,
                        message=str(exc)[:500],
                    )
                )
            except Exception as exc:
                # Local failure (queue-read SQL, programming error): still
                # stage-isolated, but it is NOT a provider outage — tagging it
                # as one disguised local DB faults as CNINFO downtime and
                # triggered the wrong backoff (round23).
                acquisition.failed += 1
                acquisition.failures.append(
                    WorkerFailure(
                        stage="source_local",
                        item_ref="worker",
                        error_code=type(exc).__name__,
                        message=str(exc)[:500],
                    )
                )
        finally:
            close = getattr(source, "close", None)
            if deps.source_close_after_round and callable(close):
                close()

    acquisition_thread = threading.Thread(
        target=_acquisition_stages, name="acquire", daemon=True
    )
    acquisition_thread.start()
    try:
        if limits.parse > 0 and not should_stop():
            _parse_stage(
                report,
                deps,
                limit=limits.parse,
                should_stop=should_stop,
                keep_feeding=acquisition_thread.is_alive,
            )
    finally:
        acquisition_thread.join()
    _merge_acquisition_report(report, acquisition)
    if (
        limits.build > 0
        and not any(failure.stage == "build" for failure in report.failures)
        and not should_stop()
    ):
        _build_stage(report, deps, limit=limits.build, should_stop=should_stop)
    if (
        limits.publish > 0
        and not any(failure.stage == "publish" for failure in report.failures)
        and not should_stop()
    ):
        _publish_stage(report, deps, limit=limits.publish, should_stop=should_stop)
    if limits.publish > 0 and not should_stop():
        # Derived retrieval projection (U7): delta rebuild bounded by the publish
        # batch limit. It reads active-run units and writes only the projection
        # table, so it is isolated from and never blocks the publish chain.
        _project_stage(report, deps, limit=limits.publish)

    report.duration_seconds = (deps.clock() - started_at).total_seconds()
    return report


def _sync_stage(
    report: WorkerReport,
    deps: WorkerDeps,
    source: DisclosureSourcePort | None,
    *,
    limit: int,
    stage_seconds: int,
    should_stop: Callable[[], bool],
) -> DisclosureSourcePort | None:
    with deps.engine.connect() as conn:
        due = queries.sync_due(
            conn, interval_seconds=deps.config.sync_interval_seconds, limit=limit
        )
    if not due:
        return source
    source = source or deps.source_factory()
    use_case = SyncDisclosureIndex(
        source=source,
        profile_loader=deps.profile_loader_factory(source),
        uow_factory=deps.uow_factory,
    )
    today = deps.clock().astimezone(SHANGHAI_TZ).date()
    processing_backlog_now: int | None = None
    stage_deadline = time.monotonic() + stage_seconds if stage_seconds > 0 else None
    for row in due:
        if should_stop():
            return source
        if stage_deadline is not None and time.monotonic() >= stage_deadline:
            # Time-boxed: yield to download/parse so no stage starves; the
            # remaining companies stay due for the next round.
            return source
        security_code = row.get("security_code")
        exchange = row.get("exchange")
        if not security_code or not exchange:
            _record_sync_failure_access(
                deps,
                row,
                error_code="tracked_company_without_security",
            )
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="sync",
                    item_ref=str(row.get("company_id")),
                    error_code="tracked_company_without_security",
                )
            )
            continue
        never_synced = row.get("last_synced_at") is None and not row.get("window_end")
        if never_synced and deps.config.backfill_max_pending_downloads > 0:
            # Admission watermark (changedetection.io MAX_QUEUE_SIZE pattern):
            # scan pending-download + pending-parse once per round, then add
            # each admitted company's full candidate_count as a conservative
            # upper bound. Downloads merely move items between those queues,
            # so a GPU outage cannot admit the universe as raw files. A company
            # sync is atomic, therefore one company's set may cross the mark.
            if processing_backlog_now is None:
                with deps.engine.connect() as conn:
                    processing_backlog_now = (
                        queries.pending_processing_backlog_count(
                            conn,
                            max_retries=deps.config.cninfo_max_retries,
                            scope_classes=deps.config.process_scope_classes,
                        )
                    )
            if processing_backlog_now >= deps.config.backfill_max_pending_downloads:
                report.deferred_backfill += 1
                continue
        window_start = _sync_window_start(
            row.get("window_end"),
            today=today,
            overlap_days=deps.config.cninfo_overlap_days,
            initial_lookback_days=deps.config.initial_lookback_days,
            lookback=row.get("lookback"),
        )
        try:
            result = use_case.execute(
                SyncDisclosureIndexCommand(
                    security_code=str(security_code),
                    exchange=str(exchange),
                    window_start=window_start,
                    window_end=today,
                )
            )
        except Exception as exc:
            error_code, retryable = _source_error_details(exc)
            _record_sync_failure_access(
                deps,
                row,
                error_code=error_code,
                retryable=True if retryable is None else retryable,
            )
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="sync",
                    item_ref=str(security_code),
                    error_code=error_code,
                    retryable=retryable,
                )
            )
            if _is_rate_limit_error(exc):
                # Short-window provider verdict: yield the stage now and let
                # the controller apply a brief cooldown while local download/
                # parse work continues.
                report.sync_rate_limited = True
                return source
            if _is_quota_error(exc):
                # Round-level breaker (edgartools guidance: stop, do not keep
                # burning quota); remaining companies stay due for next round.
                report.sync_quota_break = True
                return source
            if _is_provider_infrastructure_error(error_code, retryable):
                # A retryable provider outage is batch-wide, not 13/50
                # independent item failures. Stop touching CNINFO now; the
                # resident controller cools source while local parse drains.
                report.source_outage_break = True
                return source
            continue
        report.synced_companies += 1
        report.candidates_discovered += result.candidate_count
        if never_synced and processing_backlog_now is not None:
            # Conservative in-round cache: candidate_count can include rows
            # already known from overlap, so it may overestimate but cannot
            # admit below the real watermark. The next round re-counts truth.
            processing_backlog_now += result.candidate_count
    return source


def _record_sync_failure_access(
    deps: WorkerDeps,
    row: dict[str, Any],
    *,
    error_code: str,
    retryable: bool = True,
) -> None:
    """Persist a scheduler-visible failure marker for fair due rotation.

    Provider use cases already retain request-level provenance. This separate
    marker covers failures before/around those requests as well, so every
    caught item failure moves behind untouched companies for at least 60s.
    """

    company_id = row.get("company_id")
    if not isinstance(company_id, str) or not company_id:
        raise RuntimeError("sync due row is missing company_id")
    security_id = row.get("security_id")
    safe_code = error_code[:128]
    with deps.uow_factory() as uow:
        uow.source_accesses.add(
            e.SourceAccess(
                source_access_id=ids.new_source_access_id(),
                provider="cninfo",
                provider_interface="cninfo:worker_sync_failure",
                dataset_key="worker_sync_failure",
                query_params={
                    "security_code": row.get("security_code"),
                    "exchange": row.get("exchange"),
                },
                accessed_at=deps.clock(),
                status="failed",
                error=json.dumps(
                    {
                        "stage": "sync",
                        "error_code": safe_code,
                        "retryable": retryable,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                result_snapshot={"tracked_company_id": row.get("tracked_company_id")},
                company_id=company_id,
                security_id=(security_id if isinstance(security_id, str) else None),
            )
        )
        uow.commit()


def _is_rate_limit_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "error_code", None) == "rate_limited":
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_quota_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "error_code", None) == "quota_exhausted":
            return True
        current = current.__cause__ or current.__context__
    return False


def _source_error_details(exc: BaseException) -> tuple[str, bool | None]:
    """Recover provider semantics hidden below a use-case wrapper."""

    seen: set[int] = set()
    current: BaseException | None = exc
    fallback = type(exc).__name__
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        error_code = getattr(current, "error_code", None)
        if isinstance(error_code, str) and error_code:
            retryable = getattr(current, "retryable", None)
            return error_code, retryable if isinstance(retryable, bool) else None
        current = current.__cause__ or current.__context__
    return fallback, None


def _is_provider_infrastructure_error(
    error_code: str, retryable: bool | None
) -> bool:
    return retryable is True and (
        error_code in PROVIDER_INFRASTRUCTURE_ERROR_CODES
        or error_code.startswith("http_5")
        or error_code == "http_429"
    )


def _sync_window_start(
    window_end: object,
    *,
    today: date,
    overlap_days: int,
    initial_lookback_days: int = 1095,
    lookback: object = None,
) -> date:
    if isinstance(window_end, str) and window_end:
        return date.fromisoformat(window_end) - timedelta(days=overlap_days)
    # Never synced: default historical backfill (user decision 2026-07-06,
    # 三年是底线), with an optional per-company tracked override {"days": N}.
    days = initial_lookback_days
    if isinstance(lookback, dict):
        override = lookback.get("days")
        if isinstance(override, int) and override >= 0:
            days = override
    return today - timedelta(days=days)


def _download_stage(
    report: WorkerReport,
    deps: WorkerDeps,
    source: DisclosureSourcePort | None,
    *,
    limit: int,
    should_stop: Callable[[], bool],
) -> DisclosureSourcePort | None:
    with deps.engine.connect() as conn:
        pending = queries.pending_downloads(
            conn,
            max_retries=deps.config.cninfo_max_retries,
            limit=limit,
            scope_classes=deps.config.process_scope_classes,
        )
    if not pending:
        return source
    source = source or deps.source_factory()
    downloader = DownloadDocument(
        source=source,
        raw_store=deps.raw_store,
        path_builder=deps.path_builder,
        uow_factory=deps.uow_factory,
    )
    for row in pending:
        if should_stop():
            return source
        item_ref = str(row["provider_document_id"])
        candidate = row.get("candidate")
        if not isinstance(candidate, dict):
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="download", item_ref=item_ref, error_code="candidate_shape"
                )
            )
            continue
        try:
            result = downloader.execute(
                DownloadDocumentCommand(
                    candidate=candidate,
                    oversized_kb=deps.config.cninfo_oversized_kb,
                )
            )
        except Exception as exc:
            error_code, retryable = _source_error_details(exc)
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="download",
                    item_ref=item_ref,
                    error_code=error_code,
                    retryable=retryable,
                )
            )
            if _is_provider_infrastructure_error(error_code, retryable):
                report.source_outage_break = True
                return source
            continue
        if result.document_id is not None:
            report.downloaded += 1
        else:
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="download",
                    item_ref=item_ref,
                    error_code=(
                        result.error_code
                        or result.quarantine_reason
                        or "download_failed"
                    ),
                    retryable=result.retryable,
                )
            )
            if _is_provider_infrastructure_error(
                result.error_code or "", result.retryable
            ):
                report.source_outage_break = True
                return source
    return source


@dataclass
class _DocOutcome:
    """Per-document result of one parse→build→publish chain (thread-safe fold
    into the shared report happens on the caller's thread only)."""

    parsed: bool = False
    built: bool = False
    published: bool = False
    build_stats: dict[str, Any] | None = None
    failure: WorkerFailure | None = None


def _process_one_document(
    deps: WorkerDeps, document_id: str
) -> _DocOutcome:
    """Run the 05 chain for ONE document. Safe to run concurrently: each call
    builds its own parser (no shared _version_cache), every DB write is a
    fresh UoW, and parse-finish/publish already take the doc-level advisory
    xact lock (worker/locks.py DOC_NS)."""

    outcome = _DocOutcome()
    stage = "parse"
    try:
        parse_use_case = ParseDocument(
            parser=deps.parser_factory(),
            path_builder=deps.path_builder,
            raw_store=deps.raw_store,
            artifact_store=deps.artifact_store,
            uow_factory=deps.uow_factory,
            default_timeout_seconds=deps.parse_timeout_seconds,
        )
        parse_result = parse_use_case.execute(
            ParseDocumentCommand(document_id=document_id, options=deps.parser_options)
        )
        if parse_result.status != "succeeded":
            outcome.failure = WorkerFailure(
                stage="parse",
                item_ref=document_id,
                error_code=_error_code(parse_result.error),
            )
            return outcome
        outcome.parsed = True
        stage = "build"
        build_result = BuildUnits(
            path_builder=deps.path_builder,
            artifact_store=deps.artifact_store,
            uow_factory=deps.uow_factory,
        ).execute(
            BuildUnitsCommand(processing_run_id=parse_result.processing_run_id)
        )
        if build_result.status != "succeeded":
            outcome.failure = WorkerFailure(
                stage="build",
                item_ref=document_id,
                error_code=_error_code(build_result.error),
            )
            return outcome
        outcome.built = True
        if build_result.build_stats:
            outcome.build_stats = dict(build_result.build_stats)
        stage = "publish"
        publish_result = PublishRun(uow_factory=deps.uow_factory).execute(
            PublishRunCommand(processing_run_id=parse_result.processing_run_id)
        )
        if publish_result.status != "published":
            outcome.failure = WorkerFailure(
                stage="publish",
                item_ref=document_id,
                error_code=str(publish_result.status),
            )
            return outcome
        outcome.published = True
    except Exception as exc:
        structured_error = getattr(exc, "error", None)
        outcome.failure = WorkerFailure(
            stage=stage,
            item_ref=document_id,
            error_code=(
                _error_code(structured_error)
                if isinstance(structured_error, dict)
                else type(exc).__name__
            ),
        )
    return outcome


def _fold_outcome(report: WorkerReport, outcome: _DocOutcome) -> None:
    if outcome.parsed:
        report.parsed += 1
    if outcome.built:
        report.built += 1
    if outcome.build_stats:
        report.build_stats.append(outcome.build_stats)
    if outcome.published:
        report.published += 1
    if outcome.failure is not None:
        report.failed += 1
        report.failures.append(outcome.failure)


def build_failures_indicate_outage(
    failures: Iterable[WorkerFailure],
) -> bool:
    """Classify shared build failure without letting one bad IR block peers.

    Explicit DB/shared-write codes trip immediately. An otherwise unknown
    code needs two same-round occurrences; known item-local contract/IR codes
    remain isolated and rely on the existing per-run attempt cap.
    """

    build_codes = [
        failure.error_code for failure in failures if failure.stage == "build"
    ]
    if any(code in BUILD_INFRASTRUCTURE_ERROR_CODES for code in build_codes):
        return True
    unknown_counts = Counter(
        code for code in build_codes if code not in BUILD_ITEM_LOCAL_ERROR_CODES
    )
    return any(count >= 2 for count in unknown_counts.values())


def _halts_parse_refill(
    outcome: _DocOutcome, failures: Iterable[WorkerFailure]
) -> bool:
    failure = outcome.failure
    return failure is not None and (
        failure.stage == "publish"
        or (failure.stage == "build" and build_failures_indicate_outage(failures))
        or (
            failure.stage == "parse"
            and failure.error_code in PARSER_INFRASTRUCTURE_ERROR_CODES
        )
    )


def _parse_stage(
    report: WorkerReport,
    deps: WorkerDeps,
    *,
    limit: int,
    should_stop: Callable[[], bool],
    keep_feeding: Callable[[], bool] = lambda: False,
) -> None:
    """Parse pending documents through the 05 process chain (parse→build→publish).

    Per-document chains are independent (doc-level xact locks), so the stage
    runs them on a bounded pool when parse_concurrency > 1 — the fit for the
    remote *-http-client backends where each chain is mostly waiting on the
    GPU server (vllm continuous batching absorbs the parallel requests).
    Miniflux WORKER_POOL_SIZE / changedetection.io FETCH_WORKERS analog.

    While ``keep_feeding`` holds (the acquisition thread is still syncing/
    downloading), an exhausted batch re-dequeues and keeps the GPU fed —
    without this the round blocks on the acquisition join with an idle GPU
    (the observed 15-minute sawtooth valleys).  Fresh downloads landed by
    the live acquisition thread become parseable within the same round.
    """

    while True:
        batch_done = _parse_one_batch(
            report, deps, limit=limit, should_stop=should_stop
        )
        if batch_done == "halt" or should_stop():
            return
        if not keep_feeding():
            return
        if batch_done == "empty":
            # Nothing to parse yet; give the acquisition thread a moment to
            # land more downloads instead of hammering the queue query.
            for _ in range(10):
                if should_stop() or not keep_feeding():
                    return
                time.sleep(0.5)


def _parse_one_batch(
    report: WorkerReport,
    deps: WorkerDeps,
    *,
    limit: int,
    should_stop: Callable[[], bool],
) -> str:
    """Run one dequeue-and-parse wave; returns "done" | "empty" | "halt"."""

    with deps.engine.connect() as conn:
        pending = queries.pending_parse(
            conn,
            max_retries=deps.config.max_parse_retries,
            limit=limit,
            scope_classes=deps.config.process_scope_classes,
        )
    document_ids: list[str] = []
    for row in pending:
        if bool(row.get("oversized")):
            report.skipped_oversized += 1
            continue
        document_ids.append(str(row["document_id"]))

    if not document_ids:
        return "empty"

    # Parser identity is process/configuration health. Probe once before
    # dequeuing any item so a missing/broken MinerU binary cannot consume
    # every document's retry budget in concurrency-sized waves.
    try:
        deps.parser_factory().identity()
    except ParserVersionProbeError:
        report.failed += 1
        report.failures.append(
            WorkerFailure(
                stage="parse",
                item_ref="parser",
                error_code="parser_version_probe_failed",
                retryable=True,
            )
        )
        return "halt"

    concurrency = max(1, deps.config.parse_concurrency)
    if concurrency == 1:
        for document_id in document_ids:
            if should_stop():
                return "halt"
            outcome = _process_one_document(deps, document_id)
            _fold_outcome(report, outcome)
            if _halts_parse_refill(outcome, report.failures):
                return "halt"
        return "done"
    limiter = deps.parse_limiter or AdaptiveConcurrencyLimit(
        max_limit=concurrency
    )
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="parse") as pool:
        pending_ids = iter(document_ids)
        in_flight: dict[Future[_DocOutcome], str] = {}

        def submit_one() -> bool:
            if should_stop():
                return False
            try:
                document_id = next(pending_ids)
            except StopIteration:
                return False
            in_flight[pool.submit(_process_one_document, deps, document_id)] = document_id
            return True

        def refill() -> None:
            while len(in_flight) < limiter.current and submit_one():
                pass

        refill()
        halt_refill = False
        while in_flight:
            completed, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
            for future in completed:
                document_id = in_flight.pop(future)
                try:
                    outcome = future.result()
                except Exception as exc:  # defensive boundary around a worker future
                    outcome = _DocOutcome(
                        failure=WorkerFailure(
                            stage="parse",
                            item_ref=document_id,
                            error_code=type(exc).__name__,
                        )
                    )
                _fold_outcome(report, outcome)
                failure = outcome.failure
                if (
                    failure is not None
                    and failure.stage == "parse"
                    and failure.error_code in PARSER_INFRASTRUCTURE_ERROR_CODES
                ):
                    # An infrastructure drop is backpressure first: shrink
                    # and keep dispatching at the reduced rate.  Only a
                    # drop while already at the floor reads as a dead
                    # backend and stops the refill.
                    if limiter.current <= limiter.min_limit:
                        halt_refill = True
                    limiter.on_drop()
                else:
                    if failure is None:
                        limiter.on_success(inflight=len(in_flight) + 1)
                    halt_refill = halt_refill or _halts_parse_refill(
                        outcome, report.failures
                    )
            report.parse_concurrency_limit = limiter.current
            if should_stop():
                for future in in_flight:
                    future.cancel()
                continue
            if not halt_refill:
                refill()
    return "halt" if halt_refill else "done"


def _build_stage(
    report: WorkerReport,
    deps: WorkerDeps,
    *,
    limit: int,
    should_stop: Callable[[], bool],
) -> None:
    """Drain build leftovers from interrupted process chains."""

    with deps.engine.connect() as conn:
        pending = queries.pending_build(
            conn, max_retries=deps.config.max_build_retries, limit=limit
        )
    use_case = BuildUnits(
        path_builder=deps.path_builder,
        artifact_store=deps.artifact_store,
        uow_factory=deps.uow_factory,
    )
    for row in pending:
        if should_stop():
            return
        run_id = str(row["processing_run_id"])
        try:
            result = use_case.execute(BuildUnitsCommand(processing_run_id=run_id))
        except Exception as exc:
            structured_error = getattr(exc, "error", None)
            error_code = (
                _error_code(structured_error)
                if isinstance(structured_error, dict)
                else type(exc).__name__
            )
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="build", item_ref=run_id, error_code=error_code
                )
            )
            if build_failures_indicate_outage(report.failures):
                return
            continue
        if result.status == "succeeded":
            report.built += 1
            if result.build_stats:
                report.build_stats.append(dict(result.build_stats))
        else:
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="build",
                    item_ref=run_id,
                    error_code=_error_code(result.error),
                )
            )
            if build_failures_indicate_outage(report.failures):
                return


def _publish_stage(
    report: WorkerReport,
    deps: WorkerDeps,
    *,
    limit: int,
    should_stop: Callable[[], bool],
) -> None:
    with deps.engine.connect() as conn:
        pending = queries.pending_publish(conn, limit=limit)
    use_case = PublishRun(uow_factory=deps.uow_factory)
    for row in pending:
        if should_stop():
            return
        run_id = str(row["processing_run_id"])
        try:
            result = use_case.execute(PublishRunCommand(processing_run_id=run_id))
        except Exception as exc:
            # Continue with the rest of the batch: a single deterministically
            # failing run must not head-of-line block every other document's
            # publish round after round (round23).
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="publish",
                    item_ref=run_id,
                    error_code=type(exc).__name__,
                    message=str(exc)[:500],
                )
            )
            continue
        if result.status == "published":
            report.published += 1
        else:
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="publish", item_ref=run_id, error_code=str(result.status)
                )
            )
            continue


def _project_stage(
    report: WorkerReport,
    deps: WorkerDeps,
    *,
    limit: int,
) -> None:
    """Delta-rebuild the derived search projection (08 + 06R §5).

    Failure-isolated like every other stage: a projection fault lands in the
    failure list under stage='project' and the round still completes. The
    projection is derived and regenerable, so a skipped round self-heals on the
    next delta pass or a CLI full rebuild.
    """

    try:
        result = BuildSearchProjection(engine=deps.engine).execute(
            BuildSearchProjectionCommand(full=False, limit=limit)
        )
    except Exception as exc:
        report.failed += 1
        report.failures.append(
            WorkerFailure(
                stage="project",
                item_ref="search_projection",
                error_code=type(exc).__name__,
                message=str(exc)[:500],
            )
        )
        return
    report.projected += result.projected


def render_report_section(report: WorkerReport) -> str:
    """One `## run <ISO>` section; the CLI appends it to the daily file."""

    lines = [
        f"## run {report.started_at.isoformat()}",
        "",
        f"- duration_seconds: {round(report.duration_seconds, 3)}",
        f"- stale_reclaimed: {report.stale_reclaimed}",
        f"- synced_companies: {report.synced_companies}",
        f"- candidates_discovered: {report.candidates_discovered}",
        f"- downloaded: {report.downloaded}",
        f"- parsed: {report.parsed}",
        f"- built: {report.built}",
        f"- published: {report.published}",
        f"- projected: {report.projected}",
        f"- failed: {report.failed}",
        f"- skipped_oversized: {report.skipped_oversized}",
        f"- deferred_backfill: {report.deferred_backfill}",
        f"- sync_quota_break: {report.sync_quota_break}",
        f"- sync_rate_limited: {report.sync_rate_limited}",
        f"- parse_concurrency_limit: {report.parse_concurrency_limit}",
        f"- source_outage_break: {report.source_outage_break}",
    ]
    if report.failures:
        lines.append("- failures:")
        lines.extend(
            f"  - {failure.stage} {failure.item_ref} {failure.error_code}"
            + (f" — {failure.message}" if failure.message else "")
            for failure in report.failures
        )
    lines.append("")
    return "\n".join(lines)


def _error_code(error: object) -> str:
    if isinstance(error, dict):
        return str(error.get("error_code", "unknown"))
    return "unknown"
