"""Single-document pipeline CLI."""

from __future__ import annotations

import argparse
import csv
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
    migration_database_url,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.adapters.parsers.mineru.parser import MinerUDocumentParser
from disclosure_anchor.adapters.sources.cninfo import CninfoClient, CninfoSource
from disclosure_anchor.adapters.sources.cninfo.web_source import CninfoWebSource
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
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
from disclosure_anchor.application.use_cases.rebuild_units import (
    RebuildUnits,
    RebuildUnitsCommand,
)
from disclosure_anchor.application.use_cases.track_companies import (
    TrackCompanies,
    TrackCompaniesCommand,
    TrackEntry,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.use_cases.register_local_pdf import (
    RegisterLocalPdf,
    RegisterLocalPdfCommand,
)
from disclosure_anchor.application.use_cases.sync_disclosure_index import (
    INDEX_INTERFACE,
    WEB_INDEX_INTERFACE,
    SyncDisclosureIndex,
    SyncDisclosureIndexCommand,
)
from disclosure_anchor.domain.errors import BuildUnitsError, ConfigurationError, PublishRunError
from disclosure_anchor.domain.value_objects import ReportPeriod
from disclosure_anchor.settings import Settings, load_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="disclosure-anchor pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register")
    register.add_argument("--file", required=True, type=Path)
    register.add_argument("--provider", required=True)
    register.add_argument("--security-code", required=True)
    register.add_argument("--exchange", required=True)
    register.add_argument("--filing-type", required=True)
    register.add_argument("--title", required=True)
    register.add_argument("--announcement-date", required=True)
    register.add_argument("--report-period")
    register.add_argument("--provider-document-id")
    register.add_argument("--company-legal-name")

    parse = subparsers.add_parser("parse")
    parse.add_argument("--document-id", required=True)

    build_units = subparsers.add_parser("build-units")
    build_units.add_argument("--document-id", required=True)

    publish = subparsers.add_parser("publish")
    publish.add_argument("--processing-run-id", required=True)
    publish.add_argument("--allow-empty", action="store_true")
    publish.add_argument("--reason")

    track = subparsers.add_parser(
        "track",
        help="upsert the company watchlist into tracked_company (offline, idempotent)",
    )
    track.add_argument("--file", help="watchlist CSV (default: config/watchlist.csv)")
    track.add_argument("--codes", help="comma-separated security codes (ad-hoc adds)")
    track.add_argument(
        "--prune-drift",
        action="store_true",
        help="pause tracked companies missing from the watchlist (default: report only)",
    )

    track_status = subparsers.add_parser(
        "track-status", help="read-only watchlist status (config + sync progress)"
    )
    del track_status  # no arguments

    rebuild = subparsers.add_parser(
        "rebuild-units",
        help="rebuild units from the latest succeeded parse run (no MinerU re-parse)",
    )
    rebuild.add_argument("--document-id", required=True)
    rebuild.add_argument("--allow-empty", action="store_true")
    rebuild.add_argument("--reason")

    process = subparsers.add_parser("process")
    process.add_argument("--document-id", required=True)
    process.add_argument("--allow-empty", action="store_true")
    process.add_argument("--reason")

    sync = subparsers.add_parser("sync")
    sync.add_argument("--company", required=True)
    sync.add_argument("--window", type=int)
    sync.add_argument(
        "--channel",
        choices=("api", "web"),
        default="api",
        help="api = WebAPI p_info3015 (credentialed); web = credential-free public fallback",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
        deps = _Deps(settings)
        result: Any
        if args.command == "register":
            result = deps.register().execute(_register_command(args))
        elif args.command == "parse":
            result = deps.parse().execute(ParseDocumentCommand(document_id=args.document_id))
            if not _stage_succeeded("parse", result):
                _print_failed_stage("parse", result)
                return 1
        elif args.command == "build-units":
            result = deps.build_units().execute(
                BuildUnitsCommand(document_id=args.document_id)
            )
            if not _stage_succeeded("build-units", result):
                _print_failed_stage("build-units", result)
                return 1
        elif args.command == "publish":
            result = deps.publish().execute(
                PublishRunCommand(
                    processing_run_id=args.processing_run_id,
                    allow_empty=args.allow_empty,
                    reason=args.reason,
                )
            )
            if not _stage_succeeded("publish", result):
                _print_failed_stage("publish", result)
                return 1
        elif args.command == "track":
            entries = _track_entries(args)
            # File-driven runs reconcile (CSV is the source of truth); --codes
            # ad-hoc adds do not, and are flagged as future drift.
            file_driven = not args.codes
            result = deps.track().execute(
                TrackCompaniesCommand(
                    entries=entries,
                    reconcile=file_driven,
                    prune_drift=file_driven and args.prune_drift,
                )
            )
            if args.codes:
                print(
                    "[note] --codes upserts are NOT written to config/watchlist.csv; "
                    "the next file-driven `make track` will report them as drift",
                    file=sys.stderr,
                )
        elif args.command == "track-status":
            result = deps.track_status()
        elif args.command == "rebuild-units":
            rebuild_result = deps.rebuild_units().execute(
                RebuildUnitsCommand(document_id=args.document_id)
            )
            build_result = deps.build_units().execute(
                BuildUnitsCommand(processing_run_id=rebuild_result.processing_run_id)
            )
            if not _stage_succeeded("build-units", build_result):
                _print_failed_stage("build-units", build_result)
                return 1
            publish_result = deps.publish().execute(
                PublishRunCommand(
                    processing_run_id=rebuild_result.processing_run_id,
                    allow_empty=args.allow_empty,
                    reason=args.reason,
                )
            )
            if not _stage_succeeded("publish", publish_result):
                _print_failed_stage("publish", publish_result)
                return 1
            result = {
                "rebuild": _jsonable(rebuild_result),
                "build_units": _jsonable(build_result),
                "publish": _jsonable(publish_result),
            }
        elif args.command == "process":
            parse_result = deps.parse().execute(
                ParseDocumentCommand(document_id=args.document_id)
            )
            if not _stage_succeeded("parse", parse_result):
                _print_failed_stage("parse", parse_result)
                return 1
            build_result = deps.build_units().execute(
                BuildUnitsCommand(processing_run_id=parse_result.processing_run_id)
            )
            if not _stage_succeeded("build-units", build_result):
                _print_failed_stage("build-units", build_result)
                return 1
            publish_result = deps.publish().execute(
                PublishRunCommand(
                    processing_run_id=parse_result.processing_run_id,
                    allow_empty=args.allow_empty,
                    reason=args.reason,
                )
            )
            if not _stage_succeeded("publish", publish_result):
                _print_failed_stage("publish", publish_result)
                return 1
            result = {
                "parse": _jsonable(parse_result),
                "build_units": _jsonable(build_result),
                "publish": _jsonable(publish_result),
            }
        elif args.command == "sync":
            result = deps.sync(args)
        else:
            raise AssertionError(f"unhandled command: {args.command}")
    except (BuildUnitsError, PublishRunError) as exc:
        print(json.dumps(exc.error, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    except (ConfigurationError, ValidationError, ValueError) as exc:
        print(f"[FAIL] pipeline: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(_jsonable(result), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


class _Deps:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.paths = FileStorePathBuilder(settings)
        self.artifacts = ArtifactStore(self.paths)
        self.engine = create_db_engine(_database_url(settings))
        self.uow_factory = unit_of_work_factory(self.engine)

    def register(self) -> RegisterLocalPdf:
        return RegisterLocalPdf(
            raw_store=RawDocumentStore(self.paths),
            uow_factory=self.uow_factory,
        )

    def parse(self) -> ParseDocument:
        executable = self.settings.disclosure_mineru_bin or Path("mineru")
        parser = MinerUDocumentParser(process=MinerUProcess(executable=executable))
        return ParseDocument(
            parser=parser,
            path_builder=self.paths,
            raw_store=RawDocumentStore(self.paths),
            artifact_store=self.artifacts,
            uow_factory=self.uow_factory,
            default_timeout_seconds=self.settings.disclosure_parse_timeout_seconds,
        )

    def build_units(self) -> BuildUnits:
        return BuildUnits(
            path_builder=self.paths,
            artifact_store=self.artifacts,
            uow_factory=self.uow_factory,
        )

    def publish(self) -> PublishRun:
        return PublishRun(uow_factory=self.uow_factory)

    def rebuild_units(self) -> RebuildUnits:
        return RebuildUnits(uow_factory=self.uow_factory)

    def track(self) -> TrackCompanies:
        return TrackCompanies(uow_factory=self.uow_factory)

    def track_status(self) -> list[dict[str, Any]]:
        """Read-only pool status: tracked config + checkpoint + pending counts."""

        from sqlalchemy import text as sql_text

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql_text(
                    """
                    SELECT s.security_code, s.exchange, tc.status,
                           tc.lookback, tc.filing_categories, tc.sync_frequency,
                           sc.cursor->>'window_end' AS synced_through,
                           sc.updated_at AS last_synced_at,
                           (SELECT count(*) FROM disclosure_core.document d
                             WHERE d.company_id = tc.company_id) AS documents,
                           (SELECT count(*) FROM disclosure_core.document d
                             WHERE d.company_id = tc.company_id
                               AND d.status = 'published') AS published
                      FROM disclosure_core.tracked_company tc
                      LEFT JOIN disclosure_core.security s
                        ON s.security_id = tc.security_id
                      LEFT JOIN disclosure_core.source_checkpoint sc
                        ON sc.provider = 'cninfo'
                       AND sc.scope_key = tc.company_id || '\:p_info3015'
                     ORDER BY s.security_code
                    """
                )
            ).mappings()
            return [dict(row) for row in rows]

    def sync(self, args: argparse.Namespace) -> dict[str, object]:
        company = args.company
        exchange = _exchange_for_scode(company)
        today = datetime_today_shanghai()
        window_start, window_end = _sync_window(
            uow_factory=self.uow_factory,
            company=company,
            exchange=exchange,
            explicit_window_days=args.window,
            today=today,
            overlap_days=self.settings.cninfo_overlap_days,
            initial_lookback_days=self.settings.disclosure_initial_lookback_days,
        )
        channel = getattr(args, "channel", "api")
        if channel == "web":
            source: CninfoSource | CninfoWebSource = CninfoWebSource(
                max_qps=self.settings.cninfo_max_qps,
                max_retries=self.settings.cninfo_max_retries,
            )
            index_interface = WEB_INDEX_INTERFACE
        else:
            source = CninfoSource(CninfoClient.from_settings(self.settings))
            index_interface = INDEX_INTERFACE
        try:
            sync_use_case = SyncDisclosureIndex(
                source=source,
                profile_loader=source.profile_for_security,
                uow_factory=self.uow_factory,
                index_interface=index_interface,
            )
            sync_result = sync_use_case.execute(
                SyncDisclosureIndexCommand(
                    security_code=company,
                    exchange=exchange,
                    window_start=window_start,
                    window_end=window_end,
                )
            )
            downloader = DownloadDocument(
                source=source,
                raw_store=RawDocumentStore(self.paths),
                path_builder=self.paths,
                uow_factory=self.uow_factory,
            )
            pending = [
                candidate
                for candidate in downloader.list_pending_candidates(
                    max_retries=self.settings.cninfo_max_retries,
                    overlap_start=today - timedelta(days=self.settings.cninfo_overlap_days),
                )
                if candidate.get("security_code") == company
            ]
            downloads = [
                downloader.execute(
                    DownloadDocumentCommand(
                        candidate=candidate,
                        oversized_kb=self.settings.cninfo_oversized_kb,
                    )
                )
                for candidate in pending
            ]
        finally:
            source.close()
        return {
            "company": company,
            "window": {
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
            },
            "sync": _jsonable(sync_result),
            "download_count": len(downloads),
            "downloads": _jsonable(downloads),
        }


def _database_url(settings: Settings) -> str:
    if settings.database_url is not None:
        return app_database_url(settings)
    return migration_database_url(settings)


def datetime_today_shanghai() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _sync_window(
    *,
    uow_factory: Callable[[], UnitOfWork],
    company: str,
    exchange: str,
    explicit_window_days: int | None,
    today: date,
    overlap_days: int,
    initial_lookback_days: int = 1095,
) -> tuple[date, date]:
    if explicit_window_days is not None:
        if explicit_window_days < 0:
            raise ValueError("--window must be non-negative")
        return today - timedelta(days=explicit_window_days), today
    with uow_factory() as uow:
        security = uow.securities.get_by_code_exchange(company, exchange)
        if security is None:
            # First contact: default historical backfill (user decision
            # 2026-07-06, 三年是底线); --window stays the explicit override.
            return today - timedelta(days=initial_lookback_days), today
        checkpoint = uow.source_checkpoints.get_by_scope(
            "cninfo", f"{security.company_id}:p_info3015"
        )
        if checkpoint is None or not checkpoint.cursor:
            tracked = uow.tracked_companies.get_by_company_id(security.company_id)
            days = initial_lookback_days
            if tracked and isinstance(tracked.lookback, dict):
                override = tracked.lookback.get("days")
                if isinstance(override, int) and override >= 0:
                    days = override
            return today - timedelta(days=days), today
        window_end = checkpoint.cursor.get("window_end")
        if not isinstance(window_end, str):
            raise ValueError("checkpoint cursor missing window_end")
        return date.fromisoformat(window_end) - timedelta(days=overlap_days), today


def _track_entries(args: argparse.Namespace) -> tuple[TrackEntry, ...]:
    entries: list[TrackEntry] = []
    if args.codes:
        for code in str(args.codes).split(","):
            code = code.strip()
            if code:
                entries.append(
                    TrackEntry(security_code=code, exchange=_exchange_for_scode(code))
                )
    if args.file or not entries:
        path = Path(args.file or "config/watchlist.csv")
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(
                line for line in handle if not line.lstrip().startswith("#")
            ):
                code = (row.get("security_code") or "").strip()
                if not code:
                    continue
                lookback_raw = (row.get("lookback_days") or "").strip()
                frequency = (row.get("sync_frequency") or "").strip() or None
                status = (row.get("status") or "").strip() or "active"
                categories_raw = (row.get("filing_categories") or "").strip()
                categories = (
                    tuple(
                        seg.strip()
                        for seg in categories_raw.split(";")
                        if seg.strip()
                    )
                    or None
                )
                entries.append(
                    TrackEntry(
                        security_code=code,
                        exchange=(row.get("exchange") or "").strip()
                        or _exchange_for_scode(code),
                        lookback_days=int(lookback_raw) if lookback_raw else None,
                        sync_frequency=frequency,
                        filing_categories=categories,
                        status=status,
                    )
                )
    if not entries:
        raise ValueError("watchlist is empty: nothing to track")
    return tuple(entries)


def _exchange_for_scode(scode: str) -> str:
    return "SSE" if scode.startswith("6") else "SZSE"


def _register_command(args: argparse.Namespace) -> RegisterLocalPdfCommand:
    report_period = (
        ReportPeriod.parse(args.report_period) if args.report_period else None
    )
    return RegisterLocalPdfCommand(
        file_path=args.file,
        company_legal_name=args.company_legal_name or args.title,
        security_code=args.security_code,
        exchange=args.exchange,
        filing_type=args.filing_type,
        title=args.title,
        announcement_date=date.fromisoformat(args.announcement_date),
        provider_document_id=args.provider_document_id
        or _default_provider_document_id(args.file),
        provider=args.provider,
        report_period=report_period,
    )


def _default_provider_document_id(file_path: Path) -> str:
    stem = file_path.stem
    suffix = stem.rsplit("__", 1)[-1]
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", suffix):
        return suffix
    digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:16]
    return f"local-{digest}"


def _stage_succeeded(stage: str, result: Any) -> bool:
    expected = "published" if stage == "publish" else "succeeded"
    return getattr(result, "status", None) == expected


def _print_failed_stage(stage: str, result: Any) -> None:
    payload = {
        "stage": stage,
        "status": getattr(result, "status", None),
        "result": _jsonable(result),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


if __name__ == "__main__":
    raise SystemExit(main())
