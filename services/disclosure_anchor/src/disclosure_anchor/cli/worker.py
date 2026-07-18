"""Worker CLI: singleton-locked once/loop runner (08 §2/§3)."""

from __future__ import annotations


import argparse
import subprocess
from dataclasses import dataclass, replace
import signal
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
from disclosure_anchor.adapters.parsers.mineru.parser import MinerUDocumentParser
from disclosure_anchor.adapters.sources.cninfo import CninfoClient, CninfoSource
from disclosure_anchor.adapters.sources.cninfo.source import CninfoWebIndexSource
from disclosure_anchor.adapters.sources.cninfo.web_source import CninfoWebSource
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.application.dto.worker_report import (
    WorkerFailure,
    WorkerLimits,
    WorkerReport,
)
from disclosure_anchor.application.ports.disclosure_source import DisclosureSourcePort
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.worker.locks import WORKER_NS
from disclosure_anchor.application.worker.worker import (
    WorkerConfig,
    WorkerDeps,
    _is_provider_infrastructure_error,
    build_failures_indicate_outage,
    render_report_section,
    run_once,
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
PUBLISH_COOLDOWN_BASE_SECONDS = 120
PARSER_INFRASTRUCTURE_ERRORS = frozenset(
    {
        "parse_timeout",
        "parser_invocation_failed",
        "parser_timeout",
        "ParserInvocationError",
        "ParserTimeoutError",
        "parser_version_probe_failed",
    }
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="disclosure-anchor worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("once")
    subparsers.add_parser("loop")
    args = parser.parse_args(argv)

    settings = load_settings()
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
        if args.command == "once":
            return _run_rounds(settings, rounds=1)
        return _run_loop(settings, lock_conn=lock_conn)
    finally:
        lock_conn.close()
        lock_engine.dispose()


def _run_rounds(settings: Settings, *, rounds: int | None) -> int:
    engine = create_db_engine(_database_url(settings))
    deps = _deps(settings, engine)
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


def _run_loop(settings: Settings, *, lock_conn: Connection) -> int:
    """Run continuously, draining work immediately and backing off when idle."""

    engine = create_db_engine(_database_url(settings))
    stop = _StopFlag()
    stop.install()
    controller = _AdaptiveLoopController(
        idle_base_seconds=settings.worker_loop_interval_seconds,
        idle_max_seconds=settings.worker_loop_max_interval_seconds,
    )
    base_limits = _limits(settings)
    deps = _deps(settings, engine)
    try:
        while not stop.is_set():
            _assert_singleton_lock(lock_conn)
            started_at = datetime.now(timezone.utc)
            started_monotonic = time.monotonic()
            limits = controller.effective_limits(
                base_limits, now=started_monotonic
            )
            round_failed = False
            try:
                report = run_once(limits, deps, should_stop=stop.is_set)
            except Exception as exc:
                traceback.print_exc()
                report = _system_failure_report(
                    started_at=started_at,
                    duration_seconds=time.monotonic() - started_monotonic,
                    exc=exc,
                )
                round_failed = True
            report_failed = False
            try:
                _append_reports(settings, report)
                print(render_report_section(report))
                _maybe_alert(settings, report)
            except Exception:
                # A full/unmounted report volume must not turn KeepAlive into
                # a 30-second restart storm. stderr still carries the trace.
                traceback.print_exc()
                report_failed = True
            if round_failed or report_failed:
                delay = controller.system_error_delay()
            else:
                delay = controller.observe(report, now=time.monotonic())
            if stop.is_set():
                break
            if delay > 0:
                print(f"[scheduler] sleeping {round(delay, 3)}s")
                _sleep_interruptible(delay, stop=stop)
        return 0
    finally:
        deps.close_source()
        engine.dispose()


@dataclass
class _AdaptiveLoopController:
    """Small scheduler state machine; provider/GPU cooldowns are stage-local."""

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
    parse_cooldown_until: float = 0.0
    parse_cooldown_seconds: int = PARSE_COOLDOWN_BASE_SECONDS
    build_cooldown_until: float = 0.0
    build_cooldown_seconds: int = PARSE_COOLDOWN_BASE_SECONDS
    build_probe_required: bool = False
    publish_cooldown_until: float = 0.0
    publish_cooldown_seconds: int = PUBLISH_COOLDOWN_BASE_SECONDS

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
            parse=(
                0
                if self.build_probe_required
                or now
                < max(
                    self.parse_cooldown_until,
                    self.build_cooldown_until,
                    self.publish_cooldown_until,
                )
                else base.parse
            ),
            build=0 if now < self.build_cooldown_until else base.build,
            publish=0 if now < self.publish_cooldown_until else base.publish,
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

        if _parse_infrastructure_outage(report):
            self.parse_cooldown_until = now + self.parse_cooldown_seconds
            self.parse_cooldown_seconds = min(
                self.idle_max_seconds, self.parse_cooldown_seconds * 2
            )
        elif report.parsed:
            self.parse_cooldown_seconds = PARSE_COOLDOWN_BASE_SECONDS

        build_probe_cleared = False
        if _build_infrastructure_failure(report):
            self.build_cooldown_until = now + self.build_cooldown_seconds
            self.build_cooldown_seconds = min(
                self.idle_max_seconds, self.build_cooldown_seconds * 2
            )
            self.build_probe_required = True
        elif self.build_probe_required and now >= self.build_cooldown_until:
            # This was a build-only recovery probe: either leftovers built or
            # no build work remained. Only now may parse create more runs.
            self.build_probe_required = False
            build_probe_cleared = True
            self.build_cooldown_seconds = PARSE_COOLDOWN_BASE_SECONDS

        if _publish_failure(report):
            self.publish_cooldown_until = now + self.publish_cooldown_seconds
            self.publish_cooldown_seconds = min(
                self.idle_max_seconds, self.publish_cooldown_seconds * 2
            )
        elif report.published:
            self.publish_cooldown_seconds = PUBLISH_COOLDOWN_BASE_SECONDS

        if _made_progress(report) or build_probe_cleared:
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
                self.parse_cooldown_until,
                self.build_cooldown_until,
                self.publish_cooldown_until,
            )
            if deadline > now
        ]
        return min(delay, *remaining) if remaining else delay


def _made_progress(report: WorkerReport) -> bool:
    return any(
        (
            report.synced_companies,
            report.downloaded,
            report.parsed,
            report.built,
            report.published,
        )
    )


def _parse_infrastructure_outage(report: WorkerReport) -> bool:
    infrastructure_failures = sum(
        1
        for failure in report.failures
        if failure.stage == "parse"
        and failure.error_code in PARSER_INFRASTRUCTURE_ERRORS
    )
    # One failed item beside successful work can be a document-local poison,
    # and a couple of infrastructure-shaped failures inside a mostly
    # successful batch are load blips, not an outage — the successes prove
    # the backend lives (rate-based breaker, not absolute counts).  Failures
    # that dominate the batch are real evidence even when early documents
    # completed before the GPU/service went down.
    if infrastructure_failures == 0:
        return False
    return infrastructure_failures >= max(2, report.parsed) or (
        report.parsed == 0 and infrastructure_failures == 1
    )


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


def _build_infrastructure_failure(report: WorkerReport) -> bool:
    return build_failures_indicate_outage(report.failures)


def _publish_failure(report: WorkerReport) -> bool:
    return any(failure.stage == "publish" for failure in report.failures)


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
        download=settings.worker_batch_download,
        parse=settings.worker_batch_parse,
        build=settings.worker_batch_build,
        publish=settings.worker_batch_publish,
    )


def _deps(settings: Settings, engine: Engine) -> WorkerDeps:
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

    def parser_factory() -> MinerUDocumentParser:
        nonlocal parser_version
        executable = settings.disclosure_mineru_bin or Path("mineru")
        process = MinerUProcess(executable=executable)
        if parser_version is None:
            with parser_version_lock:
                if parser_version is None:
                    parser_version = process.version()
        return MinerUDocumentParser(
            process=process,
            parser_version=parser_version,
            server_url=settings.disclosure_mineru_server_url,
        )

    return WorkerDeps(
        engine=engine,
        uow_factory=unit_of_work_factory(engine),
        path_builder=paths,
        raw_store=RawDocumentStore(paths),
        artifact_store=ArtifactStore(paths),
        source_factory=source_factory,
        profile_loader_factory=profile_loader_factory,
        parser_factory=parser_factory,
        parse_timeout_seconds=settings.disclosure_parse_timeout_seconds,
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
        ),
        clock=lambda: datetime.now(timezone.utc),
        parser_options=ParserOptions(
            backend=settings.disclosure_mineru_backend,
            server_url=settings.disclosure_mineru_server_url,
        ),
        source_close_after_round=False,
        close_source=close_source,
    )


def _process_scope_classes(settings: Settings) -> tuple[str, ...]:
    from disclosure_anchor.adapters.sources.cninfo.mapper import load_processing_policy

    return load_processing_policy(settings.disclosure_processing_policy_path)


def _print_version_banner(settings: Settings) -> None:
    """Log the loaded config generation at startup (batch 4, 2026-07-14).

    The resident process reads policy/env exactly once; without this banner
    "the file says X but the process runs Y" drift was undetectable from the
    logs. DB rule versions are best-effort — a down DB must not block boot.
    """

    from disclosure_anchor.adapters.unit_builder import rules as builder_rules

    scope = _process_scope_classes(settings)
    line = (
        f"[versions] policy={settings.disclosure_processing_policy_path.name} "
        f"scope_classes={len(scope)} builder_rules={builder_rules.RULES_VERSION}"
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



if __name__ == "__main__":
    raise SystemExit(main())
