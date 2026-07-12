"""Worker scheduling shell: scan queues, call use cases, report (08 §3).

run_once contains no business logic — every action is an existing use case
(07 sync/download, 04 parse, 05 build/publish). Exception isolation is per
item: one bad company/candidate/document lands in the failure list and the
round continues.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
from disclosure_anchor.application.worker.locks import (  # noqa: F401  (re-export, 08 §2)
    DOC_NS,
    WORKER_NS,
    stable_document_hash,
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


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
    # Backpressure cap for the pending-download queue; 0 disables deferral.
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

    source: DisclosureSourcePort | None = None
    try:
        if limits.sync > 0 and not should_stop():
            source = source or deps.source_factory()
            _sync_stage(report, deps, source, limit=limits.sync, should_stop=should_stop)
        if limits.download > 0 and not should_stop():
            source = source or deps.source_factory()
            _download_stage(
                report, deps, source, limit=limits.download, should_stop=should_stop
            )
    finally:
        close = getattr(source, "close", None)
        if callable(close):
            close()

    if limits.parse > 0 and not should_stop():
        _parse_stage(report, deps, limit=limits.parse, should_stop=should_stop)
    if limits.build > 0 and not should_stop():
        _build_stage(report, deps, limit=limits.build, should_stop=should_stop)
    if limits.publish > 0 and not should_stop():
        _publish_stage(report, deps, limit=limits.publish, should_stop=should_stop)

    report.duration_seconds = (deps.clock() - started_at).total_seconds()
    return report


def _sync_stage(
    report: WorkerReport,
    deps: WorkerDeps,
    source: DisclosureSourcePort,
    *,
    limit: int,
    should_stop: Callable[[], bool],
) -> None:
    with deps.engine.connect() as conn:
        due = queries.sync_due(
            conn, interval_seconds=deps.config.sync_interval_seconds, limit=limit
        )
    use_case = SyncDisclosureIndex(
        source=source,
        profile_loader=deps.profile_loader_factory(source),
        uow_factory=deps.uow_factory,
    )
    today = deps.clock().astimezone(SHANGHAI_TZ).date()
    pending_downloads_now: int | None = None
    for row in due:
        if should_stop():
            return
        security_code = row.get("security_code")
        exchange = row.get("exchange")
        if not security_code or not exchange:
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
            # Backpressure (changedetection.io MAX_QUEUE_SIZE pattern): a full
            # historical backfill enqueues the whole window at once — defer
            # new companies while the download queue is saturated.
            if pending_downloads_now is None:
                with deps.engine.connect() as conn:
                    pending_downloads_now = queries.pending_download_count(
                        conn,
                        max_retries=deps.config.cninfo_max_retries,
                        scope_classes=deps.config.process_scope_classes,
                    )
            if pending_downloads_now >= deps.config.backfill_max_pending_downloads:
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
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="sync",
                    item_ref=str(security_code),
                    error_code=type(exc).__name__,
                )
            )
            if _is_quota_error(exc):
                # Round-level breaker (edgartools guidance: stop, do not keep
                # burning quota); remaining companies stay due for next round.
                report.sync_quota_break = True
                return
            continue
        report.synced_companies += 1
        report.candidates_discovered += result.candidate_count


def _is_quota_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "error_code", None) == "quota_exhausted":
            return True
        current = current.__cause__ or current.__context__
    return False


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
    source: DisclosureSourcePort,
    *,
    limit: int,
    should_stop: Callable[[], bool],
) -> None:
    with deps.engine.connect() as conn:
        pending = queries.pending_downloads(
            conn,
            max_retries=deps.config.cninfo_max_retries,
            limit=limit,
            scope_classes=deps.config.process_scope_classes,
        )
    downloader = DownloadDocument(
        source=source,
        raw_store=deps.raw_store,
        path_builder=deps.path_builder,
        uow_factory=deps.uow_factory,
    )
    for row in pending:
        if should_stop():
            return
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
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="download", item_ref=item_ref, error_code=type(exc).__name__
                )
            )
            continue
        if result.document_id is not None:
            report.downloaded += 1
        else:
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="download",
                    item_ref=item_ref,
                    error_code=result.quarantine_reason or "download_failed",
                )
            )


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
    parse_use_case = ParseDocument(
        parser=deps.parser_factory(),
        path_builder=deps.path_builder,
        raw_store=deps.raw_store,
        artifact_store=deps.artifact_store,
        uow_factory=deps.uow_factory,
        default_timeout_seconds=deps.parse_timeout_seconds,
    )
    try:
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
        outcome.failure = WorkerFailure(
            stage="parse", item_ref=document_id, error_code=type(exc).__name__
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


def _parse_stage(
    report: WorkerReport,
    deps: WorkerDeps,
    *,
    limit: int,
    should_stop: Callable[[], bool],
) -> None:
    """Parse pending documents through the 05 process chain (parse→build→publish).

    Per-document chains are independent (doc-level xact locks), so the stage
    runs them on a bounded pool when parse_concurrency > 1 — the fit for the
    remote *-http-client backends where each chain is mostly waiting on the
    GPU server (vllm continuous batching absorbs the parallel requests).
    Miniflux WORKER_POOL_SIZE / changedetection.io FETCH_WORKERS analog.
    """

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

    concurrency = max(1, deps.config.parse_concurrency)
    if concurrency == 1:
        for document_id in document_ids:
            if should_stop():
                return
            _fold_outcome(report, _process_one_document(deps, document_id))
        return
    with ThreadPoolExecutor(
        max_workers=concurrency, thread_name_prefix="parse"
    ) as pool:
        futures = []
        for document_id in document_ids:
            if should_stop():
                break  # submitted chains still finish; no new ones start
            futures.append(pool.submit(_process_one_document, deps, document_id))
        for future in as_completed(futures):
            _fold_outcome(report, future.result())


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
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="build", item_ref=run_id, error_code=type(exc).__name__
                )
            )
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
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="publish", item_ref=run_id, error_code=type(exc).__name__
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
        f"- failed: {report.failed}",
        f"- skipped_oversized: {report.skipped_oversized}",
        f"- deferred_backfill: {report.deferred_backfill}",
        f"- sync_quota_break: {report.sync_quota_break}",
    ]
    if report.failures:
        lines.append("- failures:")
        lines.extend(
            f"  - {failure.stage} {failure.item_ref} {failure.error_code}"
            for failure in report.failures
        )
    lines.append("")
    return "\n".join(lines)


def _error_code(error: object) -> str:
    if isinstance(error, dict):
        return str(error.get("error_code", "unknown"))
    return "unknown"
