"""Single-document pipeline CLI."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any

from pydantic import ValidationError

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
    migration_database_url,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.adapters.parsers.mineru.parser import MinerUDocumentParser
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.application.use_cases.build_units import (
    BuildUnits,
    BuildUnitsCommand,
)
from disclosure_anchor.application.use_cases.parse_document import (
    ParseDocument,
    ParseDocumentCommand,
)
from disclosure_anchor.application.use_cases.publish_run import (
    PublishRun,
    PublishRunCommand,
)
from disclosure_anchor.application.use_cases.register_local_pdf import (
    RegisterLocalPdf,
    RegisterLocalPdfCommand,
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

    process = subparsers.add_parser("process")
    process.add_argument("--document-id", required=True)
    process.add_argument("--allow-empty", action="store_true")
    process.add_argument("--reason")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
        deps = _Deps(settings)
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


def _database_url(settings: Settings) -> str:
    if settings.database_url is not None:
        return app_database_url(settings)
    return migration_database_url(settings)


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
        provider_document_id=args.provider_document_id or args.file.stem,
        provider=args.provider,
        report_period=report_period,
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
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
