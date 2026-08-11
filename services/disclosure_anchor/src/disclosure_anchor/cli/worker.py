"""Worker CLI: singleton-locked once/loop runner (08 §2/§3)."""

from __future__ import annotations


import argparse
import queue
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

import sqlalchemy

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
    migration_database_url,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.parsers.mineru.mineru_process import (
    MinerUProcess,
    terminate_active_mineru_processes,
)
from disclosure_anchor.adapters.parsers.mineru_medium.parser import (
    MinerUMediumDocumentParser,
)
from disclosure_anchor.adapters.parsers.pdf_page_probe import count_pdf_pages
from disclosure_anchor.adapters.sources.cninfo import CninfoClient, CninfoSource
from disclosure_anchor.adapters.sources.cninfo.source import CninfoWebIndexSource
from disclosure_anchor.adapters.sources.cninfo.web_source import CninfoWebSource
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.provider_document_source import (
    ProviderDocumentFileSource,
)
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.application.dto.worker_report import (
    WorkerFailure,
    WorkerLimits,
    WorkerReport,
)
from disclosure_anchor.application.ports.disclosure_source import DisclosureSourcePort
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.contracts.provider_unit import (
    PROVIDER_UNIT_BUILDER_VERSION,
)
from disclosure_anchor.application.worker.locks import WORKER_NS
from disclosure_anchor.application.worker.worker import (
    WorkerConfig,
    WorkerDeps,
    _is_provider_infrastructure_error,
    build_failures_indicate_outage,
    publish_failures_indicate_outage,
    render_report_section,
    run_once,
    run_resident_parse,
)
from disclosure_anchor.settings import Settings, load_settings

SKIP_MESSAGE = "[skip] another worker holds the singleton lock"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SYSTEM_ERROR_BASE_SECONDS = 60
SYNC_COOLDOWN_BASE_SECONDS = 1800
RATE_LIMIT_COOLDOWN_BASE_SECONDS = 90
RATE_LIMIT_COOLDOWN_MAX_SECONDS = 600
SYNC_COOLDOWN_MAX_SECONDS = 7200
PARSE_COOLDOWN_BASE_SECONDS = 120
PROVIDER_ERROR_COOLDOWN_BASE_SECONDS = 60


class WorkerSingletonGuardError(RuntimeError):
    """The process-lifetime singleton session can no longer be trusted."""


@dataclass(frozen=True)
class ExactReplayGuard:
    """One fail-closed boundary around the regular resident worker."""

    manifest_sha256: str
    reset_boundary_at: datetime
    document_count: int
    runtime_check: Callable[[WorkerDeps], None]
    database_check: Callable[[Engine], None]
    raw_identity_check: Callable[[str, str | None], None]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="disclosure-anchor worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("once")
    subparsers.add_parser("loop")
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.command == "loop":
        return run_resident_worker(settings)
    _print_version_banner(settings)
    # Singleton lock on a dedicated NullPool connection: a pooled connection
    # would leak the session lock back into the pool on release (08 §2 E6).
    lock_engine = sqlalchemy.create_engine(
        _database_url(settings),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    lock_conn = lock_engine.connect()
    try:
        acquired = lock_conn.execute(
            text("SELECT pg_try_advisory_lock(:ns, 0)"), {"ns": WORKER_NS}
        ).scalar_one()
        if not acquired:
            print(SKIP_MESSAGE)
            return 0
        return _run_rounds(settings, rounds=1, lock_conn=lock_conn)
    finally:
        lock_conn.close()
        lock_engine.dispose()


def _run_rounds(
    settings: Settings,
    *,
    rounds: int | None,
    lock_conn: Connection,
) -> int:
    engine = create_db_engine(_database_url(settings))
    deps = _deps(
        settings,
        engine,
        admission_guard=lambda: _assert_singleton_or_cancel(lock_conn),
    )
    stop = _StopFlag()
    stop.install()
    try:
        completed = 0
        while rounds is None or completed < rounds:
            report = run_once(
                _limits(settings),
                deps,
                should_stop=stop.is_set,
            )
            _append_reports(settings, report)
            print(render_report_section(report))
            completed += 1
            if stop.is_set():
                break
            if rounds is None:
                _sleep_interruptible(
                    settings.worker_loop_interval_seconds, stop=stop
                )
        return 0
    finally:
        deps.close_source()
        engine.dispose()


def _wedge_watchdog(
    *,
    plane: str,
    last_progress: list[float],
    threshold_seconds: int,
    stop: _StopFlag,
) -> threading.Thread:
    """Fail loudly when one execution plane stops proving liveness.

    Rounds may honestly run for hours (heavy parse batches), so duration
    bounds nothing — liveness does. Parse/startup and maintenance use distinct
    timestamps: progress in one plane must never conceal a deadlock in the
    other. Beyond the watchdog threshold the named plane is wedged in something
    no inner timeout covers. Dump every thread's stack and exit nonzero:
    launchd KeepAlive restarts clean, and write paths are batch-committed.
    """

    def _watch() -> None:
        import faulthandler

        while not stop.is_set():
            time.sleep(30)
            silent = time.monotonic() - last_progress[0]
            if silent > threshold_seconds and not stop.is_set():
                print(
                    f"[watchdog] {plane} plane made no progress for "
                    f"{int(silent)}s "
                    f"(threshold {threshold_seconds}s) — dumping stacks and "
                    "exiting for a clean relaunch",
                    file=sys.stderr,
                    flush=True,
                )
                faulthandler.dump_traceback(file=sys.stderr)
                sys.stderr.flush()
                _exit_wedged_worker()

    thread = threading.Thread(
        target=_watch,
        name=f"wedge-watchdog-{plane}",
        daemon=True,
    )
    thread.start()
    return thread


def _exit_wedged_worker() -> None:
    """Kill detached parser groups before launchd replaces this worker."""

    import os

    # MinerU children intentionally run in their own process groups so an
    # individual timeout can kill the whole temporary API tree. os._exit()
    # skips signal handlers/finally blocks, so the watchdog must explicitly
    # remove those groups; otherwise the replacement singleton worker starts
    # with orphan requests still consuming the same GPU budget.
    terminate_active_mineru_processes()
    os._exit(70)


def run_resident_worker(
    settings: Settings,
    *,
    exact_replay_guard: ExactReplayGuard | None = None,
) -> int:
    """Run the one resident worker, optionally bound to an exact replay.

    Replay is an admission guard, not a second scheduler: it keeps the production
    singleton, dispatcher, retry budgets, crash recovery, projection loop and
    watchdog. Acquisition and taxonomy routing are disabled because the
    immutable manifest owns corpus membership. The guard validates identity
    on the resident composition, rechecks the source closure, and checks each
    raw hash immediately before parse.
    """

    _print_version_banner(settings)
    if exact_replay_guard is not None:
        print(
            "[replay] exact manifest="
            f"{exact_replay_guard.manifest_sha256} "
            f"documents={exact_replay_guard.document_count} "
            f"reset_boundary_at="
            f"{exact_replay_guard.reset_boundary_at.isoformat()}"
        )
    lock_engine = sqlalchemy.create_engine(
        _database_url(settings),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    lock_conn = lock_engine.connect()
    try:
        acquired = lock_conn.execute(
            text("SELECT pg_try_advisory_lock(:ns, 0)"), {"ns": WORKER_NS}
        ).scalar_one()
        if not acquired:
            if exact_replay_guard is not None:
                raise WorkerSingletonGuardError(
                    "worker singleton lock is held; stop and drain the "
                    "resident worker before exact replay"
                )
            print(SKIP_MESSAGE)
            return 0
        return _run_loop(
            settings,
            lock_conn=lock_conn,
            exact_replay_guard=exact_replay_guard,
        )
    finally:
        lock_conn.close()
        lock_engine.dispose()


def _run_loop(
    settings: Settings,
    *,
    lock_conn: Connection,
    exact_replay_guard: ExactReplayGuard | None = None,
) -> int:
    """Run a resident data plane with independent maintenance/reporting."""

    engine = create_db_engine(_database_url(settings))
    stop = _StopFlag()
    stop.install()
    base_limits = _limits(settings)
    if exact_replay_guard is not None:
        base_limits = replace(
            base_limits,
            sync=0,
            sync_stage_seconds=0,
            acquisition_seconds=0,
            download=0,
        )

    def admission_guard() -> None:
        _assert_singleton_or_cancel(lock_conn)
        if exact_replay_guard is not None:
            exact_replay_guard.database_check(engine)

    base_deps = _deps(
        settings,
        engine,
        admission_guard=admission_guard,
    )
    if exact_replay_guard is not None:
        base_deps = replace(
            base_deps,
            config=replace(
                base_deps.config,
                process_scope_classes=None,
            ),
            replay_raw_identity_guard=(
                exact_replay_guard.raw_identity_check
            ),
        )
        exact_replay_guard.runtime_check(base_deps)
        exact_replay_guard.database_check(engine)
    deps = base_deps
    maintenance_deps = base_deps
    maintenance_progress: list[float] | None = None
    if settings.worker_wedge_timeout_seconds > 0:
        parse_progress = [time.monotonic()]
        maintenance_progress = [parse_progress[0]]
        deps = replace(
            base_deps,
            heartbeat=lambda: parse_progress.__setitem__(
                0, time.monotonic()
            ),
        )
        maintenance_deps = replace(
            base_deps,
            heartbeat=lambda: maintenance_progress.__setitem__(
                0, time.monotonic()
            ),
        )
        _wedge_watchdog(
            plane="parse",
            last_progress=parse_progress,
            threshold_seconds=settings.worker_wedge_timeout_seconds,
            stop=stop,
        )
    reports: queue.SimpleQueue[WorkerReport | None] = queue.SimpleQueue()
    prune_tracker = _ProjectionPruneTracker()
    report_thread = threading.Thread(
        target=_report_writer,
        kwargs={
            "settings": settings,
            "reports": reports,
            "prune_tracker": prune_tracker,
        },
        name="worker-reports",
    )
    report_thread.start()
    work_available = threading.Event()
    fatal = threading.Event()
    fatal_errors: queue.SimpleQueue[BaseException] = queue.SimpleQueue()
    maintenance_thread: threading.Thread | None = None

    def should_stop() -> bool:
        return stop.is_set() or fatal.is_set()

    def maintenance_target() -> None:
        try:
            _run_maintenance_loop(
                settings,
                deps=maintenance_deps,
                base_limits=base_limits,
                should_stop=should_stop,
                reports=reports,
                prune_tracker=prune_tracker,
                work_available=work_available,
            )
        except BaseException as exc:
            fatal_errors.put(exc)
            fatal.set()
            work_available.set()
            terminate_active_mineru_processes()

    try:
        # Stale runs belong to a prior singleton owner. Recover them and any
        # crash leftovers exactly once before the first resident admission;
        # periodic age-based reclaim would kill legitimate >1h whole PDFs.
        _run_startup_recovery(
            settings,
            lock_conn=lock_conn,
            deps=deps,
            base_limits=base_limits,
            should_stop=should_stop,
            reports=reports,
        )
        if should_stop():
            return 0
        if maintenance_progress is not None:
            maintenance_progress[0] = time.monotonic()
            _wedge_watchdog(
                plane="maintenance",
                last_progress=maintenance_progress,
                threshold_seconds=settings.worker_wedge_timeout_seconds,
                stop=stop,
            )
        maintenance_thread = threading.Thread(
            target=maintenance_target,
            name="worker-maintenance",
        )
        maintenance_thread.start()
        run_resident_parse(
            deps,
            limit=base_limits.parse,
            should_stop=should_stop,
            report_interval_seconds=settings.worker_report_interval_seconds,
            emit_report=reports.put,
            work_available=work_available,
            build_recovery_limit=base_limits.build,
            publish_recovery_limit=base_limits.publish,
            outage_backoff_initial_seconds=PARSE_COOLDOWN_BASE_SECONDS,
            outage_backoff_max_seconds=settings.worker_loop_max_interval_seconds,
        )
        maintenance_thread.join()
        maintenance_thread = None
        if not fatal_errors.empty():
            raise fatal_errors.get()
        return 0
    except BaseException:
        fatal.set()
        work_available.set()
        terminate_active_mineru_processes()
        raise
    finally:
        fatal.set()
        work_available.set()
        if maintenance_thread is not None:
            maintenance_thread.join()
        reports.put(None)
        report_thread.join()
        base_deps.close_source()
        engine.dispose()


class _ProjectionPruneTracker:
    """Generation-based handoff from published reports to projection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._acknowledged = 0

    def mark(self, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            self._generation += count

    def pending_generation(self) -> int:
        with self._lock:
            return (
                self._generation
                if self._generation > self._acknowledged
                else 0
            )

    def acknowledge(self, generation: int) -> None:
        with self._lock:
            self._acknowledged = max(
                self._acknowledged, min(generation, self._generation)
            )


def _emit_worker_report(settings: Settings, report: WorkerReport) -> None:
    """Serialize report side effects without making them data-plane fatal."""

    # Alert first, and on its own: a full/unmounted report volume is exactly
    # the condition an operator must hear about.
    try:
        _maybe_alert(settings, report)
    except Exception:
        traceback.print_exc()
    try:
        _append_reports(settings, report)
        print(render_report_section(report))
    except Exception:
        # Reporting failure must not stop a healthy resident dispatcher.
        traceback.print_exc()


def _report_writer(
    *,
    settings: Settings,
    reports: queue.SimpleQueue[WorkerReport | None],
    prune_tracker: _ProjectionPruneTracker,
) -> None:
    while True:
        report = reports.get()
        if report is None:
            return
        prune_tracker.mark(report.runs_deactivated)
        _emit_worker_report(settings, report)


def _wait_while(
    seconds: float,
    *,
    should_stop: Callable[[], bool],
    heartbeat: Callable[[], None] = lambda: None,
) -> None:
    deadline = time.monotonic() + seconds
    while not should_stop():
        heartbeat()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.5, remaining))


def _run_startup_recovery(
    settings: Settings,
    *,
    lock_conn: Connection,
    deps: WorkerDeps,
    base_limits: WorkerLimits,
    should_stop: Callable[[], bool],
    reports: queue.SimpleQueue[WorkerReport | None],
) -> None:
    """Recover prior-owner state and drain leftovers before parse admission."""

    recovery_limits = replace(
        base_limits,
        sync=0,
        download=0,
        parse=0,
        acquisition_seconds=0,
    )
    reclaim_stale = True
    # A deactivation can commit after the maintenance thread has stopped, or
    # immediately before a process crash. The in-memory steady-state signal
    # cannot survive either boundary, so every new singleton owner performs
    # one exact prune-capable projection before admitting parse work.
    projection_recovery_pending = True
    delay = float(PARSE_COOLDOWN_BASE_SECONDS)
    while not should_stop():
        _assert_singleton_or_cancel(lock_conn)
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        try:
            report = run_once(
                recovery_limits,
                deps,
                should_stop=should_stop,
                reclaim_stale=reclaim_stale,
                # No current-process parse has been admitted yet, and this
                # process owns the singleton. Every running row therefore
                # belongs to the exited prior owner, even if it is seconds
                # old; an age threshold here would strand fresh crash runs
                # forever because steady maintenance never reclaims.
                stale_threshold_seconds=0 if reclaim_stale else None,
                run_projection=True,
                projection_prune=projection_recovery_pending,
            )
        except WorkerSingletonGuardError:
            raise
        except Exception as exc:
            traceback.print_exc()
            reports.put(
                _system_failure_report(
                    started_at=started_at,
                    duration_seconds=time.monotonic() - started_monotonic,
                    exc=exc,
                )
            )
            _wait_while(
                delay,
                should_stop=should_stop,
                heartbeat=deps.heartbeat,
            )
            delay = min(
                float(settings.worker_loop_max_interval_seconds), delay * 2
            )
            continue
        reclaim_stale = False
        reports.put(report)
        projection_failed = any(
            failure.stage == "project" for failure in report.failures
        )
        if projection_recovery_pending:
            if projection_failed:
                _wait_while(
                    delay,
                    should_stop=should_stop,
                    heartbeat=deps.heartbeat,
                )
                delay = min(
                    float(settings.worker_loop_max_interval_seconds),
                    delay * 2,
                )
                continue
            projection_recovery_pending = False
        shared_failure = (
            build_failures_indicate_outage(report.failures)
            or publish_failures_indicate_outage(report.failures)
        )
        if shared_failure:
            _wait_while(
                delay,
                should_stop=should_stop,
                heartbeat=deps.heartbeat,
            )
            delay = min(
                float(settings.worker_loop_max_interval_seconds), delay * 2
            )
            continue
        if report.built or report.published:
            delay = float(PARSE_COOLDOWN_BASE_SECONDS)
            continue
        return


def _run_maintenance_loop(
    settings: Settings,
    *,
    deps: WorkerDeps,
    base_limits: WorkerLimits,
    should_stop: Callable[[], bool],
    reports: queue.SimpleQueue[WorkerReport | None],
    prune_tracker: _ProjectionPruneTracker,
    work_available: threading.Event,
) -> None:
    """Run acquisition and derived projection without owning parse/finalize."""

    controller = _AdaptiveLoopController(
        idle_base_seconds=settings.worker_loop_interval_seconds,
        idle_max_seconds=settings.worker_loop_max_interval_seconds,
    )
    maintenance_limits = replace(
        base_limits,
        parse=0,
        build=0,
        publish=0,
    )
    while not should_stop():
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        deps.heartbeat()
        limits = controller.effective_limits(
            maintenance_limits, now=started_monotonic
        )
        prune_generation = prune_tracker.pending_generation()
        round_failed = False
        try:
            report = run_once(
                limits,
                deps,
                should_stop=should_stop,
                reclaim_stale=False,
                run_projection=True,
                projection_prune=prune_generation > 0,
            )
        except Exception as exc:
            traceback.print_exc()
            report = _system_failure_report(
                started_at=started_at,
                duration_seconds=time.monotonic() - started_monotonic,
                exc=exc,
            )
            round_failed = True
        if (
            prune_generation > 0
            and not should_stop()
            and not any(
                failure.stage == "project" for failure in report.failures
            )
        ):
            prune_tracker.acknowledge(prune_generation)
        if report.downloaded:
            work_available.set()
        reports.put(report)
        delay = (
            controller.system_error_delay()
            if round_failed
            else controller.observe(report, now=time.monotonic())
        )
        if delay > 0 and not should_stop():
            print(f"[maintenance] sleeping {round(delay, 3)}s")
            _wait_while(
                delay,
                should_stop=should_stop,
                heartbeat=deps.heartbeat,
            )


@dataclass
class _AdaptiveLoopController:
    """Acquisition-only cooldown and idle-backoff state."""

    idle_base_seconds: int
    idle_max_seconds: int
    idle_delay_seconds: int = 0
    item_failure_delay_seconds: int = SYSTEM_ERROR_BASE_SECONDS
    system_failure_delay_seconds: int = SYSTEM_ERROR_BASE_SECONDS
    sync_quota_cooldown_until: float = 0.0
    rate_limit_cooldown_until: float = 0.0
    rate_limit_cooldown_seconds: int = RATE_LIMIT_COOLDOWN_BASE_SECONDS
    provider_error_cooldown_until: float = 0.0
    quota_cooldown_seconds: int = SYNC_COOLDOWN_BASE_SECONDS
    provider_error_cooldown_seconds: int = PROVIDER_ERROR_COOLDOWN_BASE_SECONDS

    def __post_init__(self) -> None:
        self.idle_max_seconds = max(
            self.idle_base_seconds, self.idle_max_seconds
        )
        self.idle_delay_seconds = self.idle_base_seconds

    def effective_limits(self, base: WorkerLimits, *, now: float) -> WorkerLimits:
        return replace(
            base,
            sync=(
                0
                if now < max(
                    self.sync_quota_cooldown_until,
                    self.rate_limit_cooldown_until,
                    self.provider_error_cooldown_until,
                )
                else base.sync
            ),
            download=(
                0 if now < self.provider_error_cooldown_until else base.download
            ),
        )

    def observe(self, report: WorkerReport, *, now: float) -> float:
        # Reaching a report proves the round-level dependency boundary is
        # healthy again, even when the queues are empty.
        self.system_failure_delay_seconds = SYSTEM_ERROR_BASE_SECONDS
        quota_started = False
        if report.sync_rate_limited:
            self.rate_limit_cooldown_until = now + self.rate_limit_cooldown_seconds
            self.rate_limit_cooldown_seconds = min(
                RATE_LIMIT_COOLDOWN_MAX_SECONDS,
                self.rate_limit_cooldown_seconds * 2,
            )
            quota_started = True
        elif report.sync_quota_break:
            self.sync_quota_cooldown_until = now + self.quota_cooldown_seconds
            self.quota_cooldown_seconds = min(
                SYNC_COOLDOWN_MAX_SECONDS, self.quota_cooldown_seconds * 2
            )
            quota_started = True
        elif _source_infrastructure_outage(report):
            self.provider_error_cooldown_until = (
                now + self.provider_error_cooldown_seconds
            )
            self.provider_error_cooldown_seconds = min(
                self.idle_max_seconds,
                self.provider_error_cooldown_seconds * 2,
            )
        elif report.synced_companies:
            self.quota_cooldown_seconds = SYNC_COOLDOWN_BASE_SECONDS
            # Decay instead of reset: inside a long provider throttle window
            # a trickle of synced companies between 429 trips would otherwise
            # collapse the ladder to base and hammer the wall every ~90s
            # (observed ~50 throttled calls/hour); halving keeps recovery
            # fast after real relief while sustained windows stay near max.
            self.rate_limit_cooldown_seconds = max(
                RATE_LIMIT_COOLDOWN_BASE_SECONDS,
                self.rate_limit_cooldown_seconds // 2,
            )
            self.provider_error_cooldown_seconds = PROVIDER_ERROR_COOLDOWN_BASE_SECONDS
        elif report.downloaded:
            # Local/static download success proves the provider path recovered,
            # but says nothing about the quota-gated index endpoint. Do not
            # collapse its 30→60→120 minute breaker while draining local work.
            self.provider_error_cooldown_seconds = PROVIDER_ERROR_COOLDOWN_BASE_SECONDS

        if _made_progress(report):
            self._reset_success_backoff()
            return 0.0
        if quota_started:
            # Give local download/parse queues one immediate quota-free pass.
            return 0.0
        if report.failed:
            delay = self.item_failure_delay_seconds
            self.item_failure_delay_seconds = min(
                self.idle_max_seconds, self.item_failure_delay_seconds * 2
            )
            self.idle_delay_seconds = self.idle_base_seconds
        else:
            delay = self.idle_delay_seconds
            self.idle_delay_seconds = min(
                self.idle_max_seconds, self.idle_delay_seconds * 2
            )
            self.item_failure_delay_seconds = SYSTEM_ERROR_BASE_SECONDS
        return self._wake_for_cooldown(float(delay), now=now)

    def system_error_delay(self) -> float:
        delay = self.system_failure_delay_seconds
        self.system_failure_delay_seconds = min(
            self.idle_max_seconds, self.system_failure_delay_seconds * 2
        )
        return float(delay)

    def _reset_success_backoff(self) -> None:
        self.idle_delay_seconds = self.idle_base_seconds
        self.item_failure_delay_seconds = SYSTEM_ERROR_BASE_SECONDS
        self.system_failure_delay_seconds = SYSTEM_ERROR_BASE_SECONDS

    def _wake_for_cooldown(self, delay: float, *, now: float) -> float:
        remaining = [
            deadline - now
            for deadline in (
                self.sync_quota_cooldown_until,
                self.rate_limit_cooldown_until,
                self.provider_error_cooldown_until,
            )
            if deadline > now
        ]
        return min(delay, *remaining) if remaining else delay


def _made_progress(report: WorkerReport) -> bool:
    return bool(report.synced_companies or report.downloaded)


def _source_infrastructure_outage(report: WorkerReport) -> bool:
    return report.source_outage_break or any(
        failure.stage == "source"
        or (
            failure.stage in {"sync", "download"}
            and _is_provider_infrastructure_error(
                failure.error_code, failure.retryable
            )
        )
        for failure in report.failures
    )


def _system_failure_report(
    *, started_at: datetime, duration_seconds: float, exc: Exception
) -> WorkerReport:
    report = WorkerReport(started_at=started_at, duration_seconds=duration_seconds)
    report.failed = 1
    report.failures.append(
        WorkerFailure(
            stage="system", item_ref="round", error_code=type(exc).__name__
        )
    )
    return report


def _assert_singleton_lock(lock_conn: Connection) -> None:
    held = lock_conn.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_locks "
            "WHERE locktype='advisory' AND pid=pg_backend_pid() "
            "AND classid=:ns AND objid=0 AND granted)"
        ),
        {"ns": WORKER_NS},
    ).scalar_one()
    if not held:
        raise RuntimeError("worker singleton advisory lock was lost")


def _assert_singleton_or_cancel(lock_conn: Connection) -> None:
    """Fail closed and cancel live MinerU work if the lock session is lost."""

    try:
        _assert_singleton_lock(lock_conn)
    except Exception as exc:
        terminate_active_mineru_processes()
        raise WorkerSingletonGuardError(str(exc)) from exc


def assert_worker_singleton_or_cancel(lock_conn: Connection) -> None:
    """Public fail-closed guard for controlled maintenance workers."""

    _assert_singleton_or_cancel(lock_conn)


class _StopFlag:
    def __init__(self) -> None:
        self._stopped = False

    def install(self) -> None:
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        del signum, frame
        self._stopped = True
        terminate_active_mineru_processes()

    def is_set(self) -> bool:
        return self._stopped


def _sleep_interruptible(seconds: float, *, stop: _StopFlag) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not stop.is_set():
        time.sleep(min(1.0, deadline - time.monotonic()))


def _limits(settings: Settings) -> WorkerLimits:
    return WorkerLimits(
        sync=settings.worker_batch_sync,
        sync_stage_seconds=settings.worker_sync_stage_seconds,
        acquisition_seconds=settings.worker_acquisition_seconds,
        download=settings.worker_batch_download,
        parse=settings.worker_batch_parse,
        build=settings.worker_batch_build,
        publish=settings.worker_batch_publish,
    )


def _deps(
    settings: Settings,
    engine: Engine,
    *,
    admission_guard: Callable[[], None] = lambda: None,
) -> WorkerDeps:
    paths = FileStorePathBuilder(settings)
    source: CninfoSource | CninfoWebIndexSource | None = None
    parser_version: str | None = None
    parser_version_lock = threading.Lock()

    def source_factory() -> DisclosureSourcePort:
        nonlocal source
        if source is None:
            if settings.disclosure_sync_channel == "web":
                source = CninfoWebIndexSource(
                    web=CninfoWebSource(
                        max_qps=settings.cninfo_max_qps,
                        max_retries=settings.cninfo_max_retries,
                    ),
                    api_profile_source=(
                        CninfoSource(CninfoClient.from_settings(settings))
                        if settings.cninfo_access_key
                        else None
                    ),
                )
            else:
                source = CninfoSource(CninfoClient.from_settings(settings))
        return source

    def close_source() -> None:
        nonlocal source
        if source is not None:
            source.close()
            source = None

    def profile_loader_factory(source: DisclosureSourcePort):  # type: ignore[no-untyped-def]
        loader = getattr(source, "profile_for_security", None)
        if loader is None:
            return lambda _security_code: None
        return loader

    def parser_factory() -> MinerUMediumDocumentParser:
        nonlocal parser_version
        executable = settings.disclosure_mineru_bin or Path("mineru")
        process = MinerUProcess(executable=executable)
        if parser_version is None:
            with parser_version_lock:
                if parser_version is None:
                    parser_version = process.version()
        return MinerUMediumDocumentParser(
            process=process,
            parser_version=parser_version,
            server_url=settings.disclosure_mineru_server_url,
        )

    provider_source = ProviderDocumentFileSource(paths)
    return WorkerDeps(
        engine=engine,
        uow_factory=unit_of_work_factory(engine),
        path_builder=paths,
        raw_store=RawDocumentStore(paths),
        artifact_store=ArtifactStore(paths),
        provider_source=provider_source,
        source_factory=source_factory,
        profile_loader_factory=profile_loader_factory,
        parser_factory=parser_factory,
        parse_expected_seconds=settings.disclosure_parse_timeout_seconds,
        page_counter=count_pdf_pages,
        config=WorkerConfig(
            max_parse_retries=settings.disclosure_max_parse_retries,
            max_build_retries=settings.disclosure_max_build_retries,
            stale_run_threshold_seconds=settings.disclosure_stale_run_threshold_seconds,
            sync_interval_seconds=settings.disclosure_sync_interval_seconds,
            cninfo_overlap_days=settings.cninfo_overlap_days,
            cninfo_max_retries=settings.cninfo_max_retries,
            cninfo_oversized_kb=settings.cninfo_oversized_kb,
            initial_lookback_days=settings.disclosure_initial_lookback_days,
            backfill_max_pending_downloads=settings.disclosure_backfill_max_pending_downloads,
            process_scope_classes=_process_scope_classes(settings),
            parse_concurrency=settings.worker_parse_concurrency,
            parse_heavy_page_threshold=(
                settings.worker_parse_heavy_page_threshold
            ),
            parse_heavy_saturated_share=(
                settings.worker_parse_heavy_saturated_share
            ),
            parse_huge_page_threshold=(
                settings.worker_parse_huge_page_threshold
            ),
            parse_huge_saturated_share=(
                settings.worker_parse_huge_saturated_share
            ),
            parse_candidate_window=settings.worker_parse_candidate_window,
            finalize_concurrency=settings.worker_finalize_concurrency,
            parse_timeout_per_page_seconds=(
                settings.disclosure_parse_timeout_per_page_seconds
            ),
            parse_timeout_max_seconds=(
                settings.disclosure_parse_timeout_max_seconds
            ),
            parse_runaway_timeout_seconds=(
                settings.disclosure_parse_runaway_timeout_seconds
            ),
        ),
        clock=lambda: datetime.now(timezone.utc),
        parser_options=ParserOptions(
            backend="hybrid-http-client",
            effort="medium",
            image_analysis=False,
            server_url=settings.disclosure_mineru_server_url,
            http_request_concurrency=(
                settings.mineru_http_request_concurrency
            ),
            runtime_bundle_identity_sha256=(
                settings.disclosure_mineru_runtime_bundle_identity_sha256
            ),
        ),
        on_parse_runaway=lambda _document_id: _exit_wedged_worker(),
        source_close_after_round=False,
        close_source=close_source,
        admission_guard=admission_guard,
    )


def build_worker_dependencies(
    settings: Settings,
    engine: Engine,
    *,
    admission_guard: Callable[[], None] = lambda: None,
) -> WorkerDeps:
    """Public composition boundary for controlled maintenance workers."""

    return _deps(settings, engine, admission_guard=admission_guard)


def _process_scope_classes(settings: Settings) -> tuple[str, ...]:
    from disclosure_anchor.adapters.sources.cninfo.mapper import load_processing_policy

    return load_processing_policy(settings.disclosure_processing_policy_path)


def _print_version_banner(settings: Settings) -> None:
    """Log the loaded config generation at startup (batch 4, 2026-07-14).

    The resident process reads policy/env exactly once; without this banner
    "the file says X but the process runs Y" drift was undetectable from the
    logs. DB rule versions are best-effort — a down DB must not block boot.
    """

    scope = _process_scope_classes(settings)
    per_document_cap = settings.mineru_http_request_concurrency or 0
    line = (
        f"[versions] policy={settings.disclosure_processing_policy_path.name} "
        f"scope_classes={len(scope)} "
        f"builder_rules={PROVIDER_UNIT_BUILDER_VERSION} "
        f"gpu_request_cap="
        f"{settings.worker_parse_concurrency}x"
        f"{per_document_cap}"
        f"<={settings.worker_gpu_max_sequences} "
        f"parse_runaway={settings.disclosure_parse_runaway_timeout_seconds}s "
        f"resident_dispatch=continuous "
        f"report_interval={settings.worker_report_interval_seconds}s"
    )
    try:
        engine = create_db_engine(_database_url(settings))
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT rule_set, max(version) FROM "
                    "disclosure_core.classification_rule GROUP BY rule_set "
                    "ORDER BY rule_set"
                )
            ).all()
        engine.dispose()
        line += " " + " ".join(f"{kind}={version}" for kind, version in rows)
    except Exception as exc:  # pragma: no cover - depends on live DB
        line += f" (rule versions unavailable: {type(exc).__name__})"
    # flush: launchd redirects stdout to a file (block-buffered); the boot
    # banner must not sit invisible in the buffer (its whole purpose).
    print(line, flush=True)


def _maybe_alert(settings: Settings, report: WorkerReport) -> None:
    """Fire the single-operator notification channel on round-level trouble.

    Best-effort by design: a broken osascript/notify path must never take the
    worker down — the report file remains the durable record.
    """

    message = _alert_message(report)
    if message is None:
        return
    script = Path(__file__).resolve().parents[3] / "scripts" / "notify.sh"
    if not script.exists():
        return
    try:
        subprocess.run(
            [str(script), "worker round trouble", message],
            timeout=15,
            check=False,
            capture_output=True,
        )
    except Exception:
        traceback.print_exc()


def _alert_message(report: WorkerReport) -> str | None:
    system = [failure for failure in report.failures if failure.stage == "system"]
    if system:
        # A whole-round crash (DB down, unmounted volume, programming error)
        # only backs the loop off — it published no signal, so an operator
        # learned about it a day later from the sampled doctor at best.
        codes = sorted({failure.error_code for failure in system if failure.error_code})
        return f"round crashed ({', '.join(codes) or 'system'}) — worker is looping without progress"
    if report.source_outage_break:
        return "source outage break (CNINFO/credentials); acquisition paused this round"
    if report.failed >= 5:
        stages = sorted({failure.stage for failure in report.failures})
        return f"{report.failed} failures in one round (stages: {', '.join(stages)})"
    return None


def _append_reports(settings: Settings, report: WorkerReport) -> None:
    day = report.started_at.astimezone(SHANGHAI_TZ).date().isoformat()
    worker_dir = settings.disclosure_runtime_root / "reports" / "worker"
    worker_dir.mkdir(parents=True, exist_ok=True)
    with (worker_dir / f"{day}.md").open("a", encoding="utf-8") as handle:
        handle.write(render_report_section(report) + "\n")
    quality_dir = settings.disclosure_runtime_root / "reports" / "parse_quality"
    quality_dir.mkdir(parents=True, exist_ok=True)
    with (quality_dir / f"{day}.md").open("a", encoding="utf-8") as handle:
        handle.write(_render_parse_quality_section(report) + "\n")


def _render_parse_quality_section(report: WorkerReport) -> str:
    lines = [f"## run {report.started_at.isoformat()}", ""]
    if not report.build_stats:
        lines.append("- no builds this round")
    for stats in report.build_stats:
        lines.append(f"- {stats}")
    lines.append("")
    return "\n".join(lines)


def _database_url(settings: Settings) -> str:
    if settings.database_url is not None:
        return app_database_url(settings)
    return migration_database_url(settings)


def worker_database_url(settings: Settings) -> str:
    """Resolve the same write database used by the resident worker."""

    return _database_url(settings)



if __name__ == "__main__":
    raise SystemExit(main())
