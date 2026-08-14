"""Single-document pipeline CLI."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import csv
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
import io
import json
from pathlib import Path
import re
import shutil
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
from disclosure_anchor.adapters.parsers.mineru_medium.parser import (
    MinerUMediumDocumentParser,
)
from disclosure_anchor.adapters.parsers.mineru_medium.process import MinerUProcess
from disclosure_anchor.adapters.semantics.runtime import build_semantic_runtime
from disclosure_anchor.adapters.sources.cninfo import CninfoClient, CninfoSource
from disclosure_anchor.adapters.sources.cninfo.web_source import CninfoWebSource
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.provider_document_source import (
    ProviderDocumentFileSource,
)
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.subject_resolver import (
    PENDING_LEGAL_NAME_PREFIX,
)
from disclosure_anchor.application.services.provider_document_admission import (
    ProviderDocumentAdmission,
)
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
    ProviderDocumentPublicationGuard,
    PublishRun,
    PublishRunCommand,
)
from disclosure_anchor.application.use_cases.rebuild_units import (
    RebuildUnits,
    RebuildUnitsCommand,
)
from disclosure_anchor.application.use_cases.track_companies import (
    ProfileResolution,
    ResolveTrackedProfiles,
    TrackCompanies,
    TrackCompaniesCommand,
    TrackEntry,
    UntrackCompanies,
)
from disclosure_anchor.application.use_cases.register_local_pdf import (
    RegisterLocalPdf,
    RegisterLocalPdfCommand,
)
from disclosure_anchor.application.use_cases.sync_disclosure_index import (
    INDEX_INTERFACE,
    WEB_INDEX_INTERFACE,
    CompanyNotTrackedError,
    SyncDisclosureIndex,
    SyncDisclosureIndexCommand,
    compute_sync_window,
)
from disclosure_anchor.application.worker.locks import (
    WorkerBusyError,
    exclusive_worker_admission,
)
from disclosure_anchor.domain.errors import BuildUnitsError, ConfigurationError, PublishRunError
from disclosure_anchor.domain.value_objects import ReportPeriod
from disclosure_anchor.domain.value_objects import (
    canonical_security_identity,
    infer_mainland_exchange,
)
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
        help="import companies into tracked_company (DB is the source of truth; "
        "CSV is the import/seed format, idempotent)",
    )
    track.add_argument("--file", help="watchlist CSV (default: config/watchlist.csv)")
    track.add_argument("--codes", help="comma-separated security codes (direct adds)")
    track.add_argument(
        "--prune-drift",
        action="store_true",
        help="pause tracked companies missing from the import file (full restore)",
    )
    track.add_argument(
        "--dry-run",
        action="store_true",
        help="print the reconcile plan (create/update/pause) without writing",
    )

    track_status = subparsers.add_parser(
        "track-status", help="read-only pool status (config + sync progress)"
    )
    del track_status  # no arguments

    track_export = subparsers.add_parser(
        "track-export",
        help="export tracked_company to watchlist CSV (git snapshot / backup; "
        "round-trips with `track --file`)",
    )
    track_export.add_argument(
        "--out",
        help="write CSV to this path (default: stdout)",
    )

    untrack = subparsers.add_parser(
        "untrack",
        help="remove companies from the pool (deletes the tracked row; "
        "company/documents stay — reversible stop is `status=paused` instead)",
    )
    untrack.add_argument("--codes", required=True, help="comma-separated security codes")

    purge = subparsers.add_parser(
        "purge-company",
        help="TEST-PHASE ONLY: cascade-delete ONE company (tracked row, "
        "security, documents, runs, units, events, files) under exclusive "
        "corpus admission",
    )
    purge.add_argument("--code", required=True, help="security code")
    purge.add_argument("--exchange", help="exchange (default: inferred from code)")
    purge.add_argument(
        "--yes",
        action="store_true",
        help="confirm the destructive cascade delete",
    )

    rebuild = subparsers.add_parser(
        "rebuild-units",
        help="rebuild units from the latest succeeded parse run (no MinerU re-parse)",
    )
    rebuild.add_argument("--document-id", required=True)
    rebuild.add_argument("--allow-empty", action="store_true")
    rebuild.add_argument("--reason")

    rebuild_projection = subparsers.add_parser(
        "rebuild-search-projection",
        help="rebuild the 06R derived search projection (delta by default; "
        "both modes prune orphans; --all recomputes every active-run unit)",
    )
    rebuild_projection.add_argument(
        "--all",
        dest="full",
        action="store_true",
        help="full rebuild (default: incremental — missing/stale rows only)",
    )

    process = subparsers.add_parser("process")
    process.add_argument("--document-id", required=True)
    process.add_argument("--allow-empty", action="store_true")
    process.add_argument("--reason")

    sync = subparsers.add_parser("sync")
    sync.add_argument("--company", required=True)
    sync.add_argument(
        "--window",
        type=int,
        help="relative window: sync [today-N, today] (mutually exclusive with --from/--to)",
    )
    sync.add_argument(
        "--from",
        dest="window_from",
        help="backfill window start YYYY-MM-DD (explicit historical range; "
        "requires --to or defaults it to today)",
    )
    sync.add_argument(
        "--to",
        dest="window_to",
        help="backfill window end YYYY-MM-DD (default: today when --from is given)",
    )
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
            legal_name = _resolve_register_legal_name(args, deps.uow_factory)
            if legal_name is None:
                print(
                    "[FAIL] register: --company-legal-name is required"
                    "（公司法定名尚未解析，请显式提供）",
                    file=sys.stderr,
                )
                return 2
            result = deps.register().execute(_register_command(args, legal_name))
        elif args.command == "parse":
            with exclusive_worker_admission(deps.engine):
                result = deps.parse().execute(
                    ParseDocumentCommand(
                        document_id=args.document_id,
                        options=deps.parser_options(),
                    )
                )
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
            # File-driven runs reconcile against the import file (restore
            # semantics) with full-row upserts; a pure --codes shortcut only
            # ensures membership and must not wipe curated overrides or flip
            # status on rows that already exist (round23).
            file_driven = not args.codes
            codes_only = bool(args.codes) and not args.file
            result = deps.track().execute(
                TrackCompaniesCommand(
                    entries=entries,
                    reconcile=file_driven,
                    prune_drift=file_driven and args.prune_drift,
                    dry_run=args.dry_run,
                    mode="ensure" if codes_only else "full_row",
                )
            )
            for res in result.results:
                if res.cleared_overrides:
                    print(
                        f"[warn] track {res.security_code}: cleared overrides "
                        f"{list(res.cleared_overrides)} (full-row upsert; blank "
                        "cell = inherit global default)",
                        file=sys.stderr,
                    )
                if res.status_change:
                    print(
                        f"[warn] track {res.security_code}: status "
                        f"{res.status_change}",
                        file=sys.stderr,
                    )
            if args.codes:
                print(
                    "[note] DB updated; run `make track-export` when you want "
                    "a fresh config/watchlist.csv git snapshot",
                    file=sys.stderr,
                )
            if not args.dry_run:
                # On-add metadata fetch (Miniflux pattern): resolve pending
                # legal names now when credentials allow; sync remains the
                # fallback healer.
                for res in deps.resolve_profiles(
                    tuple((r.security_code, r.exchange) for r in result.results)
                ):
                    note = (
                        f"resolved {res.security_code} -> {res.legal_name}"
                        if res.resolved
                        else f"legal name for {res.security_code} still pending "
                        "(profile unavailable; first sync will heal it)"
                    )
                    print(f"[note] {note}", file=sys.stderr)
        elif args.command == "track-status":
            result = deps.track_status()
        elif args.command == "untrack":
            codes = tuple(
                (code.strip(), _exchange_for_scode(code.strip()))
                for code in str(args.codes).split(",")
                if code.strip()
            )
            if not codes:
                raise ValueError("untrack: no security codes given")
            result = deps.untrack().execute(codes)
        elif args.command == "purge-company":
            if not args.yes:
                print(
                    "refusing: purge-company cascades DB rows AND files; "
                    "re-run with --yes (test-phase only)",
                    file=sys.stderr,
                )
                return 2
            result = deps.purge_company(
                code=args.code,
                exchange=args.exchange or _exchange_for_scode(args.code),
            )
        elif args.command == "track-export":
            csv_text, exported, skipped = deps.track_export()
            for line in skipped:
                print(f"[warn] {line}", file=sys.stderr)
            if args.out:
                Path(args.out).write_text(csv_text, encoding="utf-8")
                result = {"companies": exported, "written_to": args.out}
            else:
                sys.stdout.write(csv_text)
                return 0
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
        elif args.command == "rebuild-search-projection":
            result = _jsonable(
                deps.build_search_projection().execute(
                    BuildSearchProjectionCommand(full=args.full)
                )
            )
        elif args.command == "process":
            with exclusive_worker_admission(deps.engine):
                parse_result = deps.parse().execute(
                    ParseDocumentCommand(
                        document_id=args.document_id,
                        options=deps.parser_options(),
                    )
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
    except CompanyNotTrackedError as exc:
        print(f"[FAIL] sync: {exc}", file=sys.stderr)
        return 2
    except (
        ConfigurationError,
        ValidationError,
        ValueError,
        WorkerBusyError,
    ) as exc:
        print(f"[FAIL] pipeline: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(_jsonable(result), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


class _Deps:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.paths = FileStorePathBuilder(settings)
        self.artifacts = ArtifactStore(self.paths)
        self.provider_source = ProviderDocumentFileSource(self.paths)
        self.engine = create_db_engine(_database_url(settings))
        self.uow_factory = unit_of_work_factory(self.engine)

    def register(self) -> RegisterLocalPdf:
        return RegisterLocalPdf(
            raw_store=RawDocumentStore(self.paths),
            uow_factory=self.uow_factory,
        )

    def parser_options(self) -> ParserOptions:
        return ParserOptions(
            backend="hybrid-http-client",
            effort="medium",
            image_analysis=False,
            server_url=self.settings.disclosure_mineru_server_url,
            http_request_concurrency=(
                self.settings.mineru_http_request_concurrency
            ),
            runtime_bundle_identity_sha256=(
                self.settings.disclosure_mineru_runtime_bundle_identity_sha256
            ),
        )

    def parse(self) -> ParseDocument:
        executable = self.settings.disclosure_mineru_bin or Path("mineru")
        parser = MinerUMediumDocumentParser(
            process=MinerUProcess(executable=executable),
            server_url=self.settings.disclosure_mineru_server_url,
        )
        return ParseDocument(
            parser=parser,
            provider_source=self.provider_source,
            path_builder=self.paths,
            raw_store=RawDocumentStore(self.paths),
            artifact_store=self.artifacts,
            uow_factory=self.uow_factory,
            default_timeout_seconds=(
                self.settings.disclosure_parse_runaway_timeout_seconds
            ),
        )

    def build_units(self) -> BuildUnits:
        semantic = build_semantic_runtime(
            settings=self.settings,
            paths=self.paths,
            artifacts=self.artifacts,
        )
        return BuildUnits(
            path_builder=self.paths,
            artifact_store=self.artifacts,
            uow_factory=self.uow_factory,
            admission=ProviderDocumentAdmission(
                path_builder=self.paths,
                source=self.provider_source,
            ),
            semantic_router=semantic.router,
            semantic_receipts=semantic.receipts,
        )

    def publish(self) -> PublishRun:
        semantic = build_semantic_runtime(
            settings=self.settings,
            paths=self.paths,
            artifacts=self.artifacts,
        )
        return PublishRun(
            uow_factory=self.uow_factory,
            publication_guard=ProviderDocumentPublicationGuard(
                ProviderDocumentAdmission(
                    path_builder=self.paths,
                    source=self.provider_source,
                ),
                semantic_router=semantic.router,
                semantic_receipts=semantic.receipts,
            ),
        )

    def rebuild_units(self) -> RebuildUnits:
        return RebuildUnits(uow_factory=self.uow_factory)

    def build_search_projection(self) -> BuildSearchProjection:
        return BuildSearchProjection(engine=self.engine)

    def track(self) -> TrackCompanies:
        return TrackCompanies(uow_factory=self.uow_factory)

    def untrack(self) -> UntrackCompanies:
        return UntrackCompanies(uow_factory=self.uow_factory)

    def resolve_profiles(
        self, codes: tuple[tuple[str, str], ...]
    ) -> tuple[ProfileResolution, ...]:
        settings = self.settings
        if not (settings.cninfo_access_key and settings.cninfo_access_secret):
            return ()
        source = CninfoSource(CninfoClient.from_settings(settings))
        return ResolveTrackedProfiles(
            uow_factory=self.uow_factory,
            profile_loader=source.profile_for_security,
        ).execute(codes)

    def purge_company(self, *, code: str, exchange: str) -> dict[str, Any]:
        """TEST-PHASE corpus-gated cascade delete of one company.

        File removal is best-effort (missing files are fine). The company
        ledger row goes too — this is for undoing mistakes/test residue, not
        an operations path.
        """

        from disclosure_anchor.application.worker.locks import (
            exclusive_corpus_mutation,
        )

        with exclusive_corpus_mutation(self.engine):
            return self._purge_company_exclusive(code=code, exchange=exchange)

    def _purge_company_exclusive(
        self, *, code: str, exchange: str
    ) -> dict[str, Any]:
        from sqlalchemy import text as sql_text

        removed_files = 0
        with self.engine.begin() as conn:
            company_id = conn.execute(
                sql_text(
                    "SELECT company_id FROM disclosure_core.security "
                    "WHERE security_code = :code AND exchange = :exchange"
                ),
                {"code": code, "exchange": exchange},
            ).scalar()
            if company_id is None:
                raise ValueError(f"no security {code}.{exchange}")
            relpaths = [
                row[0]
                for row in conn.execute(
                    sql_text(
                        "SELECT raw_file_relpath FROM disclosure_core.document "
                        "WHERE company_id = :cid AND raw_file_relpath IS NOT NULL"
                    ),
                    {"cid": company_id},
                )
            ]
            for row in conn.execute(
                sql_text(
                    "SELECT parser_artifact_relpath, normalized_ir_relpath, "
                    "provider_document_relpath, document_units_relpath "
                    "FROM disclosure_core.processing_run r "
                    "JOIN disclosure_core.document d ON d.document_id = r.document_id "
                    "WHERE d.company_id = :cid"
                ),
                {"cid": company_id},
            ):
                relpaths.extend(p for p in row if p)
            counts: dict[str, int] = {}
            conn.execute(
                sql_text(
                    "UPDATE disclosure_core.document "
                    "SET current_processing_run_id = NULL WHERE company_id = :cid"
                ),
                {"cid": company_id},
            )
            for label, sql in (
                (
                    "outbox_events",
                    "DELETE FROM disclosure_ops.outbox_event WHERE document_id IN "
                    "(SELECT document_id FROM disclosure_core.document "
                    " WHERE company_id = :cid)",
                ),
                (
                    "document_units",
                    "DELETE FROM disclosure_core.document_unit WHERE document_id IN "
                    "(SELECT document_id FROM disclosure_core.document "
                    " WHERE company_id = :cid)",
                ),
                (
                    "processing_runs",
                    "DELETE FROM disclosure_core.processing_run WHERE document_id IN "
                    "(SELECT document_id FROM disclosure_core.document "
                    " WHERE company_id = :cid)",
                ),
                (
                    "documents",
                    "DELETE FROM disclosure_core.document WHERE company_id = :cid",
                ),
                (
                    "source_accesses",
                    "DELETE FROM disclosure_core.source_access "
                    "WHERE company_id = :cid",
                ),
                (
                    # Profile fetches are recorded BEFORE subject resolution,
                    # so their rows carry no company_id (Codex acceptance P1:
                    # cninfo:p_stock2100 residue keyed only by query scode).
                    "source_accesses_unlinked",
                    "DELETE FROM disclosure_core.source_access "
                    "WHERE company_id IS NULL "
                    "AND (query_params->>'scode' = :code "
                    "     OR query_params->>'security_code' = :code)",
                ),
                (
                    "source_checkpoints",
                    "DELETE FROM disclosure_core.source_checkpoint "
                    "WHERE scope_key LIKE :cid_scope",
                ),
                (
                    "tracked_companies",
                    "DELETE FROM disclosure_core.tracked_company "
                    "WHERE company_id = :cid",
                ),
                (
                    "company_identifiers",
                    "DELETE FROM disclosure_core.company_identifier "
                    "WHERE company_id = :cid",
                ),
                (
                    "securities",
                    "DELETE FROM disclosure_core.security WHERE company_id = :cid",
                ),
                (
                    "companies",
                    "DELETE FROM disclosure_core.company WHERE company_id = :cid",
                ),
            ):
                counts[label] = conn.execute(
                    sql_text(sql),
                    {
                        "cid": company_id,
                        "cid_scope": f"{company_id}:%",
                        "code": code,
                    },
                ).rowcount
        data_dir = self.settings.disclosure_data_root / "data"
        for relpath in relpaths:
            target = data_dir / relpath
            if target.is_file():
                target.unlink()
                removed_files += 1
            elif target.is_dir():
                shutil.rmtree(target)
                removed_files += 1
        return {
            "company_id": company_id,
            "security": f"{code}.{exchange}",
            "deleted": counts,
            "removed_files": removed_files,
        }

    def track_status(self) -> list[dict[str, Any]]:
        """Read-only pool status: tracked config + checkpoint + pending counts."""

        from sqlalchemy import text as sql_text

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql_text(
                    """
                    SELECT s.security_code, s.exchange, tc.status,
                           tc.lookback, tc.process_classes, tc.sync_frequency,
                           CASE WHEN tc.process_classes IS NOT NULL
                                THEN 'company' ELSE 'global(policy)' END
                               AS process_classes_source,
                           CASE WHEN tc.lookback IS NOT NULL
                                THEN 'company' ELSE 'global(env)' END
                               AS lookback_source,
                           CASE WHEN tc.sync_frequency IS NOT NULL
                                THEN 'company' ELSE 'global(env)' END
                               AS sync_frequency_source,
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
                       AND sc.scope_key = tc.company_id || chr(58) || 'p_info3015'
                     ORDER BY s.security_code
                    """
                )
            ).mappings()
            return [dict(row) for row in rows]

    def track_export(self) -> tuple[str, int, list[str]]:
        """DB -> watchlist CSV snapshot (the OPML-export analog).

        The DB is the source of truth; this dump round-trips with
        `track --file`. joined_date derives from created_at, note from
        legal_name — both are ignored by import, kept for the human reader.
        """

        from sqlalchemy import text as sql_text

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql_text(
                    """
                    SELECT s.security_code, s.exchange, tc.status,
                           tc.created_at::date AS joined_date,
                           tc.lookback->>'days' AS lookback_days,
                           tc.sync_frequency, tc.process_classes,
                           c.legal_name
                      FROM disclosure_core.tracked_company tc
                      JOIN disclosure_core.company c ON c.company_id = tc.company_id
                      LEFT JOIN disclosure_core.security s
                        ON s.security_id = tc.security_id
                     ORDER BY s.security_code
                    """
                )
            ).mappings().all()
        return _render_watchlist_csv([dict(row) for row in rows])

    def sync(self, args: argparse.Namespace) -> dict[str, object]:
        company = args.company
        exchange = _exchange_for_scode(company)
        today = datetime_today_shanghai()
        window_from = getattr(args, "window_from", None)
        window_to = getattr(args, "window_to", None)
        explicit_start = date.fromisoformat(window_from) if window_from else None
        explicit_end = (
            date.fromisoformat(window_to)
            if window_to
            else (today if explicit_start else None)
        )
        window_start, window_end = _sync_window(
            uow_factory=self.uow_factory,
            company=company,
            exchange=exchange,
            explicit_window_days=args.window,
            today=today,
            overlap_days=self.settings.cninfo_overlap_days,
            initial_lookback_days=self.settings.disclosure_initial_lookback_days,
            explicit_window_start=explicit_start,
            explicit_window_end=explicit_end,
        )
        if explicit_start is not None:
            print(
                f"[note] backfill window [{window_start} .. {window_end}]: the "
                "index is synced now; candidates outside the recent overlap "
                "drain through worker rounds at provider pace",
                file=sys.stderr,
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
            # Same queue predicates as the worker (scope classes, carrier
            # guard, title_noise exclusion, active tracked row) narrowed to
            # this company in SQL — the CLI must not be a gate bypass that
            # downloads register_only/noise announcements (round23).
            from disclosure_anchor.adapters.sources.cninfo.mapper import (
                load_processing_policy,
            )
            from disclosure_anchor.application.worker import queries as worker_queries

            with self.engine.connect() as conn:
                pending_rows = worker_queries.pending_downloads(
                    conn,
                    max_retries=self.settings.cninfo_max_retries,
                    limit=1_000_000,
                    scope_classes=load_processing_policy(
                        self.settings.disclosure_processing_policy_path
                    ),
                    security_code=company,
                    min_announcement_date=today
                    - timedelta(days=self.settings.cninfo_overlap_days),
                )
            downloads = [
                downloader.execute(
                    DownloadDocumentCommand(candidate=row["candidate"])
                )
                for row in pending_rows
                if isinstance(row.get("candidate"), dict)
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


# Shared with the on-demand admin sync endpoint; single definition lives in
# the use-case module.
_sync_window = compute_sync_window


def _render_watchlist_csv(
    rows: list[dict[str, Any]],
) -> tuple[str, int, list[str]]:
    """DB rows -> watchlist CSV text (round-trips with `track --file`).

    Uses ``csv.writer`` (QUOTE_MINIMAL) so a ``legal_name``/note containing a
    comma or quote is quoted rather than splitting the row and corrupting the
    tracked columns. The comment banner and column header are emitted verbatim
    (byte-identical to prior exports); ``\\n`` line endings match the header.
    Blank cells still mean 'inherit the global default'.
    """

    skipped = [
        f"tracked company without security skipped: {row['legal_name']}"
        for row in rows
        if not row["security_code"]
    ]
    header = [
        "# disclosure_anchor tracking-pool snapshot — exported from the DB"
        " (source of truth) by `make track-export`.",
        "# Restore with: make track [PRUNE_DRIFT=YES]. Mutate the pool via"
        " PUT /v1/admin/tracked-companies, `make track CODES=...`,",
        "# or edit this file and re-import; blank optional cells mean"
        " 'inherit the global default'.",
        "security_code,exchange,status,joined_date,lookback_days,"
        "sync_frequency,process_classes,note",
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    exported = 0
    for row in rows:
        if not row["security_code"]:
            continue
        classes = row["process_classes"]
        writer.writerow(
            [
                row["security_code"],
                row["exchange"] or "",
                row["status"],
                row["joined_date"].isoformat(),
                row["lookback_days"] or "",
                row["sync_frequency"] or "",
                ";".join(classes) if classes else "",
                row["legal_name"],
            ]
        )
        exported += 1
    return "\n".join(header) + "\n" + buffer.getvalue(), exported, skipped


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
                classes_raw = (row.get("process_classes") or "").strip()
                process_classes = (
                    tuple(
                        seg.strip()
                        for seg in classes_raw.split(";")
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
                        process_classes=process_classes,
                        status=status,
                    )
                )
    if not entries:
        raise ValueError("watchlist is empty: nothing to track")
    return tuple(entries)


def _exchange_for_scode(scode: str) -> str:
    return infer_mainland_exchange(scode)


def _resolve_register_legal_name(
    args: argparse.Namespace, uow_factory: Callable[[], UnitOfWork]
) -> str | None:
    """Company legal name for `register`, never poisoned by the title.

    An explicit ``--company-legal-name`` wins. Otherwise reuse the ledger's
    already-resolved name for the security; refuse (return ``None``) when the
    security is unknown or only a PENDING_LEGAL_NAME placeholder exists, so the
    caller must supply the real name. The announcement title is NEVER used as a
    company legal name (that poisons the company ledger).
    """

    if args.company_legal_name:
        return str(args.company_legal_name)
    security_code, exchange = canonical_security_identity(
        args.security_code, args.exchange
    )
    with uow_factory() as uow:
        security = uow.securities.get_by_code_exchange(security_code, exchange)
        company = (
            uow.companies.get(security.company_id)
            if security is not None and security.company_id
            else None
        )
        if company is not None and not company.legal_name.startswith(
            PENDING_LEGAL_NAME_PREFIX
        ):
            return company.legal_name
    return None


def _register_command(
    args: argparse.Namespace, company_legal_name: str
) -> RegisterLocalPdfCommand:
    report_period = (
        ReportPeriod.parse(args.report_period) if args.report_period else None
    )
    return RegisterLocalPdfCommand(
        file_path=args.file,
        company_legal_name=company_legal_name,
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
    if re.fullmatch(r"[0-9]{1,128}", suffix, re.ASCII):
        return suffix
    raise ValueError(
        "cninfo provider_document_id is required unless the PDF name ends "
        "with '__<numeric TEXTID>.pdf'"
    )


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
