"""Worker scheduling shell: scan queues, call use cases, report (08 §3).

run_once contains no business logic — every action is an existing use case
(07 sync/download, 04 parse, 05 build/publish). Exception isolation is per
item: one bad company/candidate/document lands in the failure list and the
round continues.
"""

from __future__ import annotations

import logging
import threading
import time

from collections import Counter, deque
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import json
from pathlib import Path
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
    NormalizedIRPublicationGuard,
    PublishRun,
    PublishRunCommand,
    TERMINAL_PUBLICATION_ERROR_CODES,
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
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import (
    DisclosureAnchorError,
    ParserVersionProbeError,
)

LOGGER = logging.getLogger(__name__)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
# The dispatcher, rather than a document future, owns the resident-worker
# heartbeat. Long documents may legally remain silent for hours, so wake the
# dispatcher periodically while at least one registered parse is still owned.
# The adapter's remote runaway guard, not the page estimate, bounds a child
# that remains alive but never returns.
PARSE_HEARTBEAT_INTERVAL_SECONDS = 30.0
# A readiness probe is an admission gate, not a liveness verdict. One remote
# connect timeout is common even while the backend is serving requests; only
# consecutive failures should end the parse round and enter cooldown.
PARSER_READINESS_FAILURE_THRESHOLD = 3
PARSER_READINESS_RETRY_SECONDS = 5.0
# Single source in worker/queries.py: the queue's retry-budget predicate and
# the scheduler's outage detection must agree on what "infrastructure" means.
# Only explicit, current global-capacity signals may halt rolling admission.
# Legacy parser_invocation_failed/parse_timeout rows remain retry-budget
# compatible in queries.py, but a task-local deadline or generic CLI failure
# is not evidence that the shared GPU path is unavailable.
PARSER_CONTROL_ERROR_CODES = frozenset(
    {
        "parser_backend_overloaded",
        "parser_backend_unavailable",
        "parser_readiness_failed",
        "parser_local_invocation_failed",
        "parser_version_probe_failed",
        "DatabaseError",
        "InterfaceError",
        "OSError",
        "OperationalError",
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
        "IR_READ_FAILED",
        "OSError",
        "OperationalError",
    }
)
BUILD_ITEM_LOCAL_ERROR_CODES = frozenset(
    {
        "IR_CONTRACT_TOO_OLD",
        "IR_CONTRACT_UNSUPPORTED",
        "IR_HASH_MISMATCH",
        "IR_MISSING",
        "PARTIAL_PDF_NOT_PUBLISHABLE",
        "RUN_NOT_FOUND",
        "RUN_NOT_SUCCEEDED",
        "UNITS_ALREADY_BUILT",
    }
)
PUBLISH_INFRASTRUCTURE_ERROR_CODES = BUILD_INFRASTRUCTURE_ERROR_CODES
PUBLISH_ITEM_LOCAL_ERROR_CODES = frozenset(
    {
        "ALLOW_EMPTY_REASON_REQUIRED",
        "EMPTY_RUN",
        "RUN_HASH_AGGREGATE_INVALID",
        "RUN_NOT_FOUND",
        "RUN_NOT_SUCCEEDED",
        "UNITS_NOT_BUILT",
    }
    | TERMINAL_PUBLICATION_ERROR_CODES
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
    # Legacy name: archived byte-size threshold for the HUGE lane only.
    # Large documents remain eligible and may borrow the full idle pool.
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
    # Cost-aware bulkheads. Mixed queues reserve most slots for regular PDFs,
    # while heavy/huge lanes keep nominal shares and borrow idle capacity.
    parse_heavy_page_threshold: int = 80
    parse_heavy_saturated_share: int = 4
    parse_huge_page_threshold: int = 500
    parse_huge_saturated_share: int = 1
    parse_candidate_window: int = 200
    finalize_concurrency: int = 2
    # Page-aware expected-duration envelope. It drives one soft warning only;
    # it must never terminate a healthy whole-document parse.
    parse_timeout_per_page_seconds: int = 12
    parse_timeout_max_seconds: int = 14400
    # Remote last-resort guard for a child process that remains alive but
    # never returns. Normal long documents continue through the soft envelope.
    parse_runaway_timeout_seconds: int = 86400


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
    parse_expected_seconds: int
    config: WorkerConfig
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    # Settings-driven parse defaults (backend/server_url cascade) — the CLI
    # builds this from env so GPU offload is a config flip, not a code change.
    parser_options: ParserOptions = ParserOptions()
    # Read-only cost probe injected by the composition root. A failed/unknown
    # probe never blocks parsing, but enters the resource-conservative heavy
    # lane and uses the base expected-duration envelope rather than
    # masquerading as a cheap notice.
    page_counter: Callable[[Path], int] | None = None
    # CLI resident mode owns one lazy CNINFO client for the process lifetime,
    # preserving both its token cache and 1-QPS bucket across zero-wait rounds.
    # Tests/other callers retain the legacy per-round close by default.
    source_close_after_round: bool = True
    close_source: Callable[[], None] = lambda: None
    # Liveness heartbeat, bumped at item granularity and periodically while a
    # whole parse future remains inside its extreme runaway lease.
    heartbeat: Callable[[], None] = lambda: None
    # Python threads cannot be killed safely. Production injects a
    # process-supervisor handoff that terminates registered MinerU groups and
    # exits for launchd replacement when the entire parse future (including
    # artifact read/map/store/DB finish) exceeds the extreme lease.
    on_parse_runaway: Callable[[str], None] = lambda _document_id: None
    # Resident production wiring re-validates the dedicated PostgreSQL
    # singleton session before every admission boundary and during long
    # waits. Losing that session must stop active MinerU groups before this
    # worker can overlap a replacement process.
    admission_guard: Callable[[], None] = lambda: None


def _merge_acquisition_report(
    report: WorkerReport, acquisition: WorkerReport
) -> None:
    """Fold the acquisition thread's sub-report into the round report."""

    report.synced_companies += acquisition.synced_companies
    report.candidates_discovered += acquisition.candidates_discovered
    report.downloaded += acquisition.downloaded
    report.deferred_backfill += acquisition.deferred_backfill
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
    # GPU no longer idles while the provider stages run. Newly parsed runs
    # finalize on a separate bounded pool; the later build/publish stages
    # drain leftovers from interrupted or older rounds.
    acquisition = WorkerReport(started_at=started_at)

    # "Parse stage returned" gates whether the pump starts ANOTHER pass, not
    # the work inside a pass already in flight. While the pump is alive,
    # _parse_stage returns only on halt or hard stop (its normal drain exits
    # require the pump to be dead first), so a parse return means the parser
    # is down: finish the current acquisition pass, then close the round.
    # Without this a dead parser (GPU outage) left run_once blocked in join
    # for the whole acquisition window, deferring the round report, the
    # operator alert, and the controller's parse cooldown by up to an hour.
    # It must NOT short-circuit the first pass's sync/download — a fast/empty
    # parse queue finishing before the acquisition thread's first pass would
    # otherwise skip acquisition entirely (legacy single-pass regression).
    parse_exited = threading.Event()

    def _acquisition_stages() -> None:
        # Pump loop, symmetric to the parse stage's continuous re-dequeue:
        # sync and download passes repeat inside the round until the
        # acquisition window closes or a full pass makes no successful
        # progress. Before this, acquisition ran exactly once per round, so
        # a single heavy parse batch (annual-report cohorts run for hours)
        # capped downloads at one batch per multi-hour round — measurably
        # below the parse rate, draining the raw-file buffer toward a GPU
        # stall. acquisition_seconds <= 0 keeps the legacy single pass.
        source: DisclosureSourcePort | None = None
        deadline = time.monotonic() + limits.acquisition_seconds

        def stop_acquiring() -> bool:
            # Loop-continuation gate only (see parse_exited note above);
            # in-pass work uses should_stop (the hard SIGTERM/SIGINT signal).
            return should_stop() or parse_exited.is_set()

        # One sync attempt per company per round: sync failures cool down
        # for only 60s (fair due rotation), so without this a company whose
        # sync keeps failing would be re-attempted every ~60s for as long
        # as download progress keeps the pump alive. Deferred-backfill
        # companies are likewise counted once, not once per pass.
        attempted_sync: set[str] = set()
        try:
            while True:
                synced_before = acquisition.synced_companies
                downloaded_before = acquisition.downloaded
                try:
                    if (
                        limits.sync > 0
                        and not acquisition.sync_rate_limited
                        and not should_stop()
                    ):
                        source = _sync_stage(
                            acquisition,
                            deps,
                            source,
                            limit=limits.sync,
                            stage_seconds=limits.sync_stage_seconds,
                            should_stop=should_stop,
                            attempted=attempted_sync,
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
                    # stage-isolated, but it is NOT a provider outage — tagging
                    # it as one disguised local DB faults as CNINFO downtime
                    # and triggered the wrong backoff (round23).
                    acquisition.failed += 1
                    acquisition.failures.append(
                        WorkerFailure(
                            stage="source_local",
                            item_ref="worker",
                            error_code=type(exc).__name__,
                            message=str(exc)[:500],
                        )
                    )
                if (
                    stop_acquiring()
                    or acquisition.sync_quota_break
                    or acquisition.source_outage_break
                ):
                    return
                if limits.acquisition_seconds <= 0:
                    return
                if time.monotonic() >= deadline:
                    # Window closed: stop starting new passes (a pass in
                    # flight already finished — the window bounds pass starts,
                    # not a hard abort), so the parse stage can drain its tail
                    # and the round can report/observe on a bounded cadence.
                    return
                if (
                    acquisition.synced_companies == synced_before
                    and acquisition.downloaded == downloaded_before
                ):
                    # No successful progress this pass: both queues are idle
                    # or every remaining item failed. Ending the round's
                    # acquisition here (instead of hot-retrying the same
                    # items) leaves them due/pending for the next round with
                    # their retry accounting intact.
                    return
        finally:
            close = getattr(source, "close", None)
            if deps.source_close_after_round and callable(close):
                close()

    acquisition_thread = threading.Thread(
        target=_acquisition_stages, name="acquire", daemon=True
    )
    acquisition_thread.start()
    parse_refill_deadline = (
        time.monotonic() + limits.acquisition_seconds
        if limits.acquisition_seconds > 0
        else 0.0
    )
    try:
        if limits.parse > 0 and not should_stop():
            try:
                _parse_stage(
                    report,
                    deps,
                    limit=limits.parse,
                    should_stop=should_stop,
                    keep_feeding=acquisition_thread.is_alive,
                    keep_refilling=(
                        (
                            lambda: time.monotonic()
                            < parse_refill_deadline
                        )
                        if parse_refill_deadline > 0
                        else None
                    ),
                )
            finally:
                parse_exited.set()
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
        # Derived retrieval projection (U7): drain the whole delta every
        # round. Index maintenance must be proportional to NEW units, never
        # capped by an unrelated constant — the publish batch limit (10,
        # document-scale) once bounded this unit-scale rebuild, so the
        # projection fell ~170x behind parse and search coverage decayed to
        # 48% while every round reported success (2026-07-23). The use case
        # keyset-batches with a commit per batch, so an unbounded drain
        # never rides one giant transaction.
        _project_stage(
            report,
            deps,
            should_stop=should_stop,
            prune=report.runs_deactivated > 0,
        )

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
    attempted: set[str] | None = None,
) -> DisclosureSourcePort | None:
    with deps.engine.connect() as conn:
        due = queries.sync_due(
            conn, interval_seconds=deps.config.sync_interval_seconds, limit=limit
        )
    if attempted is not None:
        due = [
            row for row in due if str(row.get("company_id")) not in attempted
        ]
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
        if attempted is not None:
            # Companies the stage never reached stay out of the set, so a
            # later pass in the same round still picks them up.
            attempted.add(str(row.get("company_id")))
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
        deps.heartbeat()
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
                DownloadDocumentCommand(candidate=candidate)
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
            deps.heartbeat()
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
    processing_run_id: str | None = None
    built: bool = False
    published: bool = False
    superseded_run: bool = False
    build_stats: dict[str, Any] | None = None
    failure: WorkerFailure | None = None


@dataclass(frozen=True)
class _ParseWorkItem:
    document_id: str
    page_count: int | None
    raw_byte_count: int | None


class _ParseLane(str, Enum):
    REGULAR = "regular"
    HEAVY = "heavy"
    HUGE = "huge"


@dataclass(frozen=True)
class _InFlightParse:
    item: _ParseWorkItem
    lane: _ParseLane
    expected_seconds: int
    expected_until_monotonic: float
    runaway_until_monotonic: float


def _parse_work_items(
    pending: Iterable[dict[str, Any]],
    *,
    deps: WorkerDeps,
) -> list[_ParseWorkItem]:
    items: list[_ParseWorkItem] = []
    for row in pending:
        page_count: int | None = None
        raw_byte_count_value = row.get("raw_byte_count")
        raw_byte_count = (
            raw_byte_count_value
            if isinstance(raw_byte_count_value, int)
            and not isinstance(raw_byte_count_value, bool)
            and raw_byte_count_value >= 0
            else None
        )
        raw_file_relpath = row.get("raw_file_relpath")
        if deps.page_counter is not None and isinstance(raw_file_relpath, str):
            try:
                probed = deps.page_counter(
                    deps.path_builder.data_path(Path(raw_file_relpath))
                )
            except (OSError, ValueError, RuntimeError):
                probed = 0
            if probed > 0:
                page_count = probed
        items.append(
            _ParseWorkItem(
                document_id=str(row["document_id"]),
                page_count=page_count,
                raw_byte_count=raw_byte_count,
            )
        )
    return items


def _parse_lane(item: _ParseWorkItem, config: WorkerConfig) -> _ParseLane:
    # CNINFO's provider size hint is not unit-stable. The queue supplies the
    # archived raw byte count instead, and the legacy-named threshold now
    # means isolation only: even a very large PDF remains parse-eligible.
    if (
        config.cninfo_oversized_kb > 0
        and item.raw_byte_count is not None
        and item.raw_byte_count > config.cninfo_oversized_kb * 1024
    ):
        return _ParseLane.HUGE
    if item.page_count is None:
        return _ParseLane.HEAVY
    if item.page_count >= config.parse_huge_page_threshold:
        return _ParseLane.HUGE
    if item.page_count >= config.parse_heavy_page_threshold:
        return _ParseLane.HEAVY
    return _ParseLane.REGULAR


def _parse_lane_caps(
    *,
    ready: tuple[_ParseLane, ...],
    capacity: int,
    config: WorkerConfig,
) -> dict[_ParseLane, int]:
    """Return work-conserving nominal quotas for the currently ready lanes."""

    if not ready:
        return {}
    if len(ready) == 1:
        return {ready[0]: capacity}
    caps = {lane: 0 for lane in ready}
    ordered = tuple(
        lane
        for lane in (
            _ParseLane.REGULAR,
            _ParseLane.HEAVY,
            _ParseLane.HUGE,
        )
        if lane in caps
    )
    # When fewer slots exist than ready lanes, every empty lane stays
    # eligible and the caller chooses the oldest head item. The global pool
    # still enforces ``capacity``; this avoids permanent huge-lane starvation
    # for safe single-document/local configurations.
    for lane in ordered:
        caps[lane] = 1
    remaining = max(0, capacity - len(ordered))
    for lane, share in (
        (_ParseLane.HUGE, config.parse_huge_saturated_share),
        (_ParseLane.HEAVY, config.parse_heavy_saturated_share),
    ):
        if lane in caps:
            extra = min(remaining, max(0, share - caps[lane]))
            caps[lane] += extra
            remaining -= extra
    preferred = next(
        (lane for lane in ordered if lane == _ParseLane.REGULAR),
        ordered[0],
    )
    caps[preferred] += remaining
    return caps


def _parse_expected_seconds(deps: WorkerDeps, item: _ParseWorkItem) -> int:
    page_budget = (
        0
        if item.page_count is None
        else item.page_count * deps.config.parse_timeout_per_page_seconds
    )
    return min(
        deps.config.parse_timeout_max_seconds,
        max(deps.parse_expected_seconds, page_budget),
    )


def _parse_one_document(
    deps: WorkerDeps, item: _ParseWorkItem
) -> _DocOutcome:
    """Parse one whole PDF; downstream build/publish uses a separate pool."""

    outcome = _DocOutcome()
    document_id = item.document_id
    timeout_seconds = deps.config.parse_runaway_timeout_seconds
    try:
        parse_use_case = ParseDocument(
            parser=deps.parser_factory(),
            path_builder=deps.path_builder,
            raw_store=deps.raw_store,
            artifact_store=deps.artifact_store,
            uow_factory=deps.uow_factory,
            default_timeout_seconds=timeout_seconds,
            check_readiness=False,
        )
        parse_result = parse_use_case.execute(
            ParseDocumentCommand(
                document_id=document_id,
                options=replace(
                    deps.parser_options,
                    timeout_seconds=timeout_seconds,
                ),
            )
        )
        if parse_result.status != "succeeded":
            outcome.failure = WorkerFailure(
                stage="parse",
                item_ref=document_id,
                error_code=_error_code(parse_result.error),
                retryable=(
                    bool(parse_result.error.get("retryable"))
                    if isinstance(parse_result.error, dict)
                    and parse_result.error.get("retryable") is not None
                    else None
                ),
                message=(
                    str(parse_result.error.get("message"))
                    if isinstance(parse_result.error, dict)
                    and parse_result.error.get("message")
                    else None
                ),
            )
            return outcome
        outcome.parsed = True
        outcome.processing_run_id = parse_result.processing_run_id
    except Exception as exc:
        structured_error = getattr(exc, "error", None)
        outcome.failure = WorkerFailure(
            stage="parse",
            item_ref=document_id,
            error_code=(
                _error_code(structured_error)
                if isinstance(structured_error, dict)
                else type(exc).__name__
            ),
        )
    return outcome


def _finalize_one_document(
    deps: WorkerDeps,
    *,
    document_id: str,
    processing_run_id: str,
) -> _DocOutcome:
    """Build and publish one parsed run without occupying a GPU slot."""

    outcome = _DocOutcome()
    stage = "build"
    try:
        build_result = BuildUnits(
            path_builder=deps.path_builder,
            artifact_store=deps.artifact_store,
            uow_factory=deps.uow_factory,
        ).execute(
            BuildUnitsCommand(processing_run_id=processing_run_id)
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
        publish_result = PublishRun(
            uow_factory=deps.uow_factory,
            publication_guard=NormalizedIRPublicationGuard(deps.path_builder),
        ).execute(
            PublishRunCommand(processing_run_id=processing_run_id)
        )
        if publish_result.status != "published":
            outcome.failure = WorkerFailure(
                stage="publish",
                item_ref=document_id,
                error_code=str(publish_result.status),
            )
            return outcome
        outcome.published = True
        outcome.superseded_run = publish_result.superseded_run_id is not None
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
        if outcome.superseded_run:
            report.runs_deactivated += 1
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


def publish_failures_indicate_outage(
    failures: Iterable[WorkerFailure],
) -> bool:
    """Keep deterministic publication poison local to its processing run."""

    publish_codes = [
        failure.error_code
        for failure in failures
        if failure.stage == "publish"
    ]
    if any(
        code in PUBLISH_INFRASTRUCTURE_ERROR_CODES
        for code in publish_codes
    ):
        return True
    unknown_counts = Counter(
        code
        for code in publish_codes
        if code not in PUBLISH_ITEM_LOCAL_ERROR_CODES
    )
    return any(count >= 2 for count in unknown_counts.values())


def _halts_parse_refill(
    outcome: _DocOutcome, failures: Iterable[WorkerFailure]
) -> bool:
    failure = outcome.failure
    return failure is not None and (
        (
            failure.stage == "publish"
            and publish_failures_indicate_outage(failures)
        )
        or (failure.stage == "build" and build_failures_indicate_outage(failures))
        or (
            failure.stage == "parse"
            and failure.error_code in PARSER_CONTROL_ERROR_CODES
        )
    )


def _parse_stage(
    report: WorkerReport,
    deps: WorkerDeps,
    *,
    limit: int,
    should_stop: Callable[[], bool],
    keep_feeding: Callable[[], bool] = lambda: False,
    keep_refilling: Callable[[], bool] | None = None,
) -> None:
    """Parse whole PDFs, then finalize them on a separate bounded pool.

    Parse slots represent GPU-producing documents only. Build/publish releases
    that slot immediately and runs behind bounded downstream backpressure.

    While ``keep_feeding`` holds (the acquisition thread is still syncing/
    downloading), an exhausted batch re-dequeues and keeps the GPU fed —
    without this the round can block on the acquisition join while newly
    downloaded work is already parseable. Fresh downloads landed by the live
    acquisition thread become parseable within the same round. The 2026-07-24
    audit found fixed parse-batch turnover itself contributes only ~2 seconds,
    so it is not treated as the current GPU sawtooth root cause.
    """

    while True:
        feeding_before = keep_feeding()
        batch_done = _parse_one_batch(
            report,
            deps,
            limit=limit,
            should_stop=should_stop,
            keep_refilling=keep_refilling,
        )
        if batch_done in {"halt", "closed"} or should_stop():
            return
        if batch_done == "empty":
            if not feeding_before:
                # Acquisition had already finished before this dequeue came
                # back empty, so nothing else can land this round. Exiting on
                # a live thread's empty instead would race a download that
                # commits between the dequeue and the join.
                return
            # Nothing to parse yet; give the acquisition thread a moment to
            # land more downloads instead of hammering the queue query.
            for _ in range(10):
                if should_stop():
                    return
                if not keep_feeding():
                    break
                time.sleep(0.5)
            continue
        if keep_refilling is None:
            # Direct/once mode is count-bounded: one dequeue batch may overlap
            # acquisition, but a still-running acquisition thread must not
            # silently grant a second full ``limit`` batch.
            return
        # Batch done. If acquisition was already over before this batch even
        # started, nothing new can have landed since — stop here so rounds
        # stay bounded (the backlog belongs to the next round). A batch that
        # STARTED while acquisition was live loops once more, which is what
        # closes the race with a download committing near the join.
        if not feeding_before and not keep_feeding():
            return


def _parse_one_batch(
    report: WorkerReport,
    deps: WorkerDeps,
    *,
    limit: int,
    should_stop: Callable[[], bool],
    keep_refilling: Callable[[], bool] | None = None,
) -> str:
    """Run one dequeue-and-parse wave.

    Returns ``done`` | ``empty`` | ``closed`` | ``halt``. ``closed`` is a
    resident admission boundary, checked before queue I/O/thread-pool setup.
    """

    if keep_refilling is not None and not keep_refilling():
        return "closed"

    candidate_limit = max(limit, deps.config.parse_candidate_window)
    known_ids: set[str] = set()

    def dequeue() -> list[_ParseWorkItem]:
        with deps.engine.connect() as conn:
            pending = queries.pending_parse(
                conn,
                max_retries=deps.config.max_parse_retries,
                limit=candidate_limit,
                scope_classes=deps.config.process_scope_classes,
            )
        unseen = (
            row
            for row in pending
            if str(row["document_id"]) not in known_ids
        )
        return _parse_work_items(unseen, deps=deps)

    readiness_failures = 0
    readiness_retry_at = 0.0
    readiness_deferred = False

    def parser_ready() -> bool:
        nonlocal readiness_failures, readiness_retry_at
        try:
            parser = deps.parser_factory()
            parser.identity()
            readiness = getattr(parser, "readiness", None)
            if callable(readiness):
                readiness(deps.parser_options)
        except ParserVersionProbeError as exc:
            readiness_failures += 1
            readiness_retry_at = (
                time.monotonic() + PARSER_READINESS_RETRY_SECONDS
            )
            LOGGER.warning(
                "MinerU readiness probe failed (%s/%s); admission paused: %s",
                readiness_failures,
                PARSER_READINESS_FAILURE_THRESHOLD,
                exc,
            )
            if readiness_failures >= PARSER_READINESS_FAILURE_THRESHOLD:
                report.failed += 1
                report.failures.append(
                    WorkerFailure(
                        stage="parse",
                        item_ref="parser",
                        error_code="parser_readiness_failed",
                        retryable=True,
                        message=str(exc)[:500],
                    )
                )
            return False
        if readiness_failures:
            LOGGER.info(
                "MinerU readiness recovered after %s consecutive failure(s)",
                readiness_failures,
            )
        readiness_failures = 0
        readiness_retry_at = 0.0
        return True

    rolling_admission = keep_refilling is not None

    def admission_open() -> bool:
        return bool(keep_refilling is not None and keep_refilling())

    work_items = dequeue()
    if not work_items:
        return "empty"

    concurrency = max(1, deps.config.parse_concurrency)
    queued = {lane: deque[_ParseWorkItem]() for lane in _ParseLane}
    input_order: dict[str, int] = {}
    sequence = 0

    def enqueue(items: Iterable[_ParseWorkItem]) -> None:
        nonlocal sequence
        for item in items:
            if item.document_id in known_ids:
                continue
            known_ids.add(item.document_id)
            input_order[item.document_id] = sequence
            sequence += 1
            queued[_parse_lane(item, deps.config)].append(item)

    enqueue(work_items)
    finalize_backlog_limit = max(2, concurrency * 2)
    halt_refill = False
    dispatched = 0
    report.parse_concurrency_limit = concurrency
    with (
        ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="parse",
        ) as parse_pool,
        ThreadPoolExecutor(
            max_workers=deps.config.finalize_concurrency,
            thread_name_prefix="finalize",
        ) as finalize_pool,
    ):
        parse_futures: dict[Future[_DocOutcome], _InFlightParse] = {}
        finalize_futures: dict[Future[_DocOutcome], str] = {}
        expected_duration_warned: set[Future[_DocOutcome]] = set()
        runaway_duration_warned: set[Future[_DocOutcome]] = set()
        lane_inflight = {lane: 0 for lane in _ParseLane}

        def halt_admission() -> None:
            nonlocal halt_refill, readiness_deferred
            halt_refill = True
            readiness_deferred = False

        def long_lane_inflight() -> bool:
            return any(
                lane_inflight[lane] > 0
                for lane in (_ParseLane.HEAVY, _ParseLane.HUGE)
            )

        def rolling_admission_allowed() -> bool:
            return admission_open() or long_lane_inflight()

        def submit_one() -> bool:
            nonlocal dispatched, halt_refill
            if should_stop():
                return False
            # Direct/once mode has a count bound. Resident rolling mode has a
            # time bound instead: the instant that admission window closes,
            # queued candidates remain for the next round even when fewer
            # than ``limit`` documents were admitted.
            window_open = admission_open() if rolling_admission else False
            if rolling_admission:
                if not window_open and not long_lane_inflight():
                    return False
            elif dispatched >= limit:
                return False
            # After the reporting/acquisition window closes, do not admit a
            # new long tail. While an already-admitted heavy/huge document is
            # still running, regular PDFs may fill its otherwise idle sibling
            # slots. Once the last long lane drains, regular fillers already
            # in flight finish and the round closes.
            allowed_lanes = (
                tuple(_ParseLane)
                if not rolling_admission or window_open
                else (_ParseLane.REGULAR,)
            )
            ready = tuple(
                lane for lane in allowed_lanes if queued[lane]
            )
            if (
                not ready
                and rolling_admission
                and (window_open or long_lane_inflight())
            ):
                new_items = dequeue()
                enqueue(new_items)
                ready = tuple(
                    lane for lane in allowed_lanes if queued[lane]
                )
            occupied_elsewhere = sum(
                lane_inflight[lane]
                for lane in _ParseLane
                if lane not in ready
            )
            caps = _parse_lane_caps(
                ready=ready,
                capacity=max(0, concurrency - occupied_elsewhere),
                config=deps.config,
            )
            eligible = tuple(
                lane
                for lane in ready
                if lane_inflight[lane] < caps.get(lane, 0)
            )
            if not eligible:
                return False
            lane = min(
                eligible,
                key=lambda candidate: input_order[
                    queued[candidate][0].document_id
                ],
            )
            item = queued[lane].popleft()
            expected_seconds = _parse_expected_seconds(deps, item)
            admitted_at = time.monotonic()
            future = parse_pool.submit(_parse_one_document, deps, item)
            parse_futures[future] = (
                _InFlightParse(
                    item=item,
                    lane=lane,
                    expected_seconds=expected_seconds,
                    expected_until_monotonic=(
                        admitted_at + expected_seconds
                    ),
                    runaway_until_monotonic=(
                        admitted_at
                        + deps.config.parse_runaway_timeout_seconds
                    ),
                )
            )
            lane_inflight[lane] += 1
            dispatched += 1
            if item.page_count is None:
                report.parse_unknown_page_count += 1
            if lane == _ParseLane.HUGE:
                report.parse_huge_dispatched += 1
            elif lane == _ParseLane.HEAVY:
                report.parse_heavy_dispatched += 1
            else:
                report.parse_regular_dispatched += 1
            report.parse_peak_inflight = max(
                report.parse_peak_inflight, len(parse_futures)
            )
            return True

        def refill() -> None:
            nonlocal readiness_deferred
            if (
                halt_refill
                or should_stop()
                or (
                    rolling_admission
                    and not rolling_admission_allowed()
                )
                or (
                    not rolling_admission
                    and dispatched >= limit
                )
            ):
                # A deferred probe is meaningful only while admission can
                # still resume. Closing the window or halting must not leave
                # a zero-time wait state behind.
                readiness_deferred = False
                return
            if (
                readiness_deferred
                and time.monotonic() < readiness_retry_at
            ):
                return
            # One coordinator owns admission health. MinerU caches successful
            # remote probes for 60 seconds, so this is cheap, but every refill
            # boundary still fails closed before another document starts.
            # A transient failure pauses admission in this dispatcher; it does
            # not end the round unless the consecutive-failure threshold trips.
            deps.admission_guard()
            if not parser_ready():
                if readiness_failures >= PARSER_READINESS_FAILURE_THRESHOLD:
                    halt_admission()
                else:
                    readiness_deferred = True
                return
            readiness_deferred = False
            while (
                not halt_refill
                and len(parse_futures) < concurrency
                and len(parse_futures) + len(finalize_futures)
                < finalize_backlog_limit
                and submit_one()
            ):
                pass

        refill()
        while parse_futures or finalize_futures or readiness_deferred:
            if readiness_deferred and not parse_futures and not finalize_futures:
                if should_stop():
                    halt_admission()
                    break
                if (
                    rolling_admission
                    and not rolling_admission_allowed()
                ):
                    readiness_deferred = False
                    break
                remaining = max(0.0, readiness_retry_at - time.monotonic())
                if remaining > 0:
                    time.sleep(min(0.5, remaining))
                    continue
                refill()
                continue

            futures = tuple((*parse_futures, *finalize_futures))
            wait_timeout = PARSE_HEARTBEAT_INTERVAL_SECONDS
            if readiness_deferred:
                wait_timeout = min(
                    wait_timeout,
                    max(0.0, readiness_retry_at - time.monotonic()),
                )
            completed, _ = wait(
                futures,
                timeout=wait_timeout,
                return_when=FIRST_COMPLETED,
            )
            now = time.monotonic()
            # Inspect every still-pending parse even when another parse or
            # finalize future completed. Otherwise a stream of short peers can
            # indefinitely hide one stuck future from the extreme lease.
            for future, admitted in parse_futures.items():
                # A future can complete after wait() captured its result set
                # but before this scan. Never hand an already-finished
                # 24-hour result to the process supervisor as a runaway.
                if future in completed or future.done():
                    continue
                if now >= admitted.runaway_until_monotonic:
                    if future not in runaway_duration_warned:
                        LOGGER.error(
                            "MinerU whole parse future exceeded the "
                            "extreme runaway lease: document_id=%s "
                            "lane=%s page_count=%s "
                            "runaway_guard_seconds=%s; closing admission "
                            "and handing off to the process supervisor",
                            admitted.item.document_id,
                            admitted.lane.value,
                            admitted.item.page_count,
                            deps.config.parse_runaway_timeout_seconds,
                        )
                        runaway_duration_warned.add(future)
                        halt_admission()
                        deps.on_parse_runaway(admitted.item.document_id)
                    continue
                if (
                    future in expected_duration_warned
                    or now < admitted.expected_until_monotonic
                ):
                    continue
                LOGGER.warning(
                    "MinerU parse exceeded its soft expected-duration "
                    "envelope and will continue: document_id=%s lane=%s "
                    "page_count=%s expected_seconds=%s "
                    "runaway_guard_seconds=%s",
                    admitted.item.document_id,
                    admitted.lane.value,
                    admitted.item.page_count,
                    admitted.expected_seconds,
                    deps.config.parse_runaway_timeout_seconds,
                )
                expected_duration_warned.add(future)
            if not completed:
                deps.admission_guard()
                if readiness_deferred and now >= readiness_retry_at:
                    refill()
                if any(
                    now < admitted.runaway_until_monotonic
                    for admitted in parse_futures.values()
                ):
                    deps.heartbeat()
                continue

            for future in completed & parse_futures.keys():
                admitted = parse_futures.pop(future)
                expected_duration_warned.discard(future)
                runaway_duration_warned.discard(future)
                lane_inflight[admitted.lane] -= 1
                try:
                    outcome = future.result()
                except Exception as exc:  # defensive boundary around a worker future
                    outcome = _DocOutcome(
                        failure=WorkerFailure(
                            stage="parse",
                            item_ref=admitted.item.document_id,
                            error_code=type(exc).__name__,
                        )
                    )
                _fold_outcome(report, outcome)
                deps.heartbeat()
                if (
                    outcome.parsed
                    and outcome.processing_run_id is not None
                ):
                    finalize_futures[
                        finalize_pool.submit(
                            _finalize_one_document,
                            deps,
                            document_id=admitted.item.document_id,
                            processing_run_id=outcome.processing_run_id,
                        )
                    ] = admitted.item.document_id
                if _halts_parse_refill(outcome, report.failures):
                    halt_admission()

            for future in completed & finalize_futures.keys():
                document_id = finalize_futures.pop(future)
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = _DocOutcome(
                        failure=WorkerFailure(
                            stage="build",
                            item_ref=document_id,
                            error_code=type(exc).__name__,
                        )
                    )
                _fold_outcome(report, outcome)
                deps.heartbeat()
                if _halts_parse_refill(outcome, report.failures):
                    halt_admission()

            if should_stop():
                halt_admission()
                for future in (*parse_futures, *finalize_futures):
                    future.cancel()
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
    use_case = PublishRun(
        uow_factory=deps.uow_factory,
        publication_guard=NormalizedIRPublicationGuard(deps.path_builder),
    )
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
            structured_error = getattr(exc, "error", None)
            error_code = (
                _error_code(structured_error)
                if isinstance(structured_error, dict)
                else type(exc).__name__
            )
            report.failed += 1
            report.failures.append(
                WorkerFailure(
                    stage="publish",
                    item_ref=run_id,
                    error_code=error_code,
                    retryable=(
                        bool(structured_error.get("retryable"))
                        if isinstance(structured_error, dict)
                        else None
                    ),
                    message=str(exc)[:500],
                )
            )
            continue
        if result.status == "published":
            report.published += 1
            if result.superseded_run_id is not None:
                report.runs_deactivated += 1
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
    should_stop: Callable[[], bool],
    prune: bool,
) -> None:
    """Drain the search-projection delta for this round (08 + 06R §5).

    Unbounded on purpose: the delta anti-join selects exactly the units the
    projection is missing (or stamped with an older retrieval-rules version),
    so once caught up the per-round work equals that round's newly published
    units. The stop signal is honored between batches, so a shutdown during
    a large catch-up drain yields within seconds and the committed batches
    resume next round. Failure-isolated like every other stage: a projection
    fault lands in the failure list under stage='project' and the round still
    completes. The projection is derived and regenerable, so a skipped round
    self-heals on the next round's drain or a CLI rebuild.
    """

    try:
        result = BuildSearchProjection(engine=deps.engine).execute(
            BuildSearchProjectionCommand(full=False, limit=None, prune=prune),
            should_stop=should_stop,
            on_progress=deps.heartbeat,
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
        f"- deferred_backfill: {report.deferred_backfill}",
        f"- sync_quota_break: {report.sync_quota_break}",
        f"- sync_rate_limited: {report.sync_rate_limited}",
        f"- parse_concurrency_limit: {report.parse_concurrency_limit}",
        f"- parse_peak_inflight: {report.parse_peak_inflight}",
        f"- parse_regular_dispatched: {report.parse_regular_dispatched}",
        f"- parse_heavy_dispatched: {report.parse_heavy_dispatched}",
        f"- parse_huge_dispatched: {report.parse_huge_dispatched}",
        f"- parse_unknown_page_count: {report.parse_unknown_page_count}",
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
