"""Worker CLI: singleton-locked once/loop runner (08 §2/§3)."""

from __future__ import annotations

import json
from importlib import resources

import argparse
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

import sqlalchemy

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
    migration_database_url,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.adapters.parsers.mineru.parser import MinerUDocumentParser
from disclosure_anchor.adapters.sources.cninfo import CninfoClient, CninfoSource
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.application.dto.worker_report import WorkerLimits, WorkerReport
from disclosure_anchor.application.ports.disclosure_source import DisclosureSourcePort
from disclosure_anchor.application.worker.locks import WORKER_NS
from disclosure_anchor.application.worker.worker import (
    WorkerConfig,
    WorkerDeps,
    render_report_section,
    run_once,
)
from disclosure_anchor.settings import Settings, load_settings

SKIP_MESSAGE = "[skip] another worker holds the singleton lock"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="disclosure-anchor worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("once")
    subparsers.add_parser("loop")
    args = parser.parse_args(argv)

    settings = load_settings()
    # Singleton lock on a dedicated NullPool connection: a pooled connection
    # would leak the session lock back into the pool on release (08 §2 E6).
    lock_engine = sqlalchemy.create_engine(_database_url(settings), poolclass=NullPool)
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
        return _run_loop(settings)
    finally:
        lock_conn.close()
        lock_engine.dispose()


def _run_rounds(settings: Settings, *, rounds: int | None) -> int:
    engine = create_db_engine(_database_url(settings))
    stop = _StopFlag()
    stop.install()
    try:
        completed = 0
        while rounds is None or completed < rounds:
            report = run_once(
                _limits(settings),
                _deps(settings, engine),
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
        engine.dispose()


def _run_loop(settings: Settings) -> int:
    return _run_rounds(settings, rounds=None)


class _StopFlag:
    def __init__(self) -> None:
        self._stopped = False

    def install(self) -> None:
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        del signum, frame
        self._stopped = True

    def is_set(self) -> bool:
        return self._stopped


def _sleep_interruptible(seconds: int, *, stop: _StopFlag) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not stop.is_set():
        time.sleep(min(1.0, deadline - time.monotonic()))


def _limits(settings: Settings) -> WorkerLimits:
    return WorkerLimits(
        sync=settings.worker_batch_sync,
        download=settings.worker_batch_download,
        parse=settings.worker_batch_parse,
        build=settings.worker_batch_build,
        publish=settings.worker_batch_publish,
    )


def _deps(settings: Settings, engine: Engine) -> WorkerDeps:
    paths = FileStorePathBuilder(settings)

    def source_factory() -> DisclosureSourcePort:
        return CninfoSource(CninfoClient.from_settings(settings))

    def profile_loader_factory(source: DisclosureSourcePort):  # type: ignore[no-untyped-def]
        loader = getattr(source, "profile_for_security", None)
        if loader is None:
            return lambda _security_code: None
        return loader

    def parser_factory() -> MinerUDocumentParser:
        executable = settings.disclosure_mineru_bin or Path("mineru")
        return MinerUDocumentParser(process=MinerUProcess(executable=executable))

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
            parse_scope_topics=_parse_scope_topics(settings),
        ),
        clock=lambda: datetime.now(timezone.utc),
    )


def _parse_scope_topics(settings: Settings) -> tuple[str, ...] | None:
    """None = parse everything; tuple = 'core' scope topics for 'other' docs."""

    if settings.disclosure_parse_scope == "all":
        return None
    payload = json.loads(
        resources.files("disclosure_anchor.application.worker")
        .joinpath("parse_scope.json")
        .read_text(encoding="utf-8")
    )
    return tuple(str(t) for t in payload["core_topics"])


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
