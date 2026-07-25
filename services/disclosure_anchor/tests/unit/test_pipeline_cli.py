"""Pipeline CLI parser tests."""

from __future__ import annotations

import argparse
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.cli import pipeline
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.services.subject_resolver import (
    PENDING_LEGAL_NAME_PREFIX,
)
from disclosure_anchor.application.use_cases.build_units import BuildUnitsResult
from disclosure_anchor.application.use_cases.parse_document import ParseDocumentResult
from disclosure_anchor.application.use_cases.register_local_pdf import (
    RegisterLocalPdfResult,
)
from disclosure_anchor.domain import entities as e

from tests.unit._fakes import FakeUnitOfWork


class PipelineCliTests(unittest.TestCase):
    def test_subcommands_are_registered(self) -> None:
        parser = pipeline._parser()
        cases = {
            "parse": ["parse", "--document-id", "doc_1"],
            "build-units": ["build-units", "--document-id", "doc_1"],
            "publish": ["publish", "--processing-run-id", "run_1"],
            "process": ["process", "--document-id", "doc_1"],
            "sync": ["sync", "--company", "000001", "--window", "7"],
        }
        for command, argv in cases.items():
            with self.subTest(command=command):
                self.assertEqual(parser.parse_args(argv).command, command)

    def test_register_command_defaults_provider_document_id_to_safe_suffix(self) -> None:
        args = pipeline._parser().parse_args(
            [
                "register",
                "--file",
                "2026-04-10__periodic__002484__江海股份：2025年年度报告__1225087169.pdf",
                "--provider",
                "cninfo",
                "--security-code",
                "002484",
                "--exchange",
                "szse",
                "--filing-type",
                "annual_report",
                "--title",
                "南通江海电容器股份有限公司2025年年度报告",
                "--announcement-date",
                "2026-04-10",
                "--report-period",
                "2025A",
            ]
        )

        command = pipeline._register_command(args, "南通江海电容器股份有限公司")

        self.assertEqual(
            command.file_path,
            Path("2026-04-10__periodic__002484__江海股份：2025年年度报告__1225087169.pdf"),
        )
        self.assertEqual(command.provider_document_id, "1225087169")
        self.assertEqual(str(command.report_period), "2025A")
        # The resolved legal name is used verbatim; the announcement title is
        # never repurposed as a company legal name (it poisons the ledger).
        self.assertEqual(command.company_legal_name, "南通江海电容器股份有限公司")
        self.assertNotEqual(command.company_legal_name, command.title)

    def test_register_command_rejects_filename_without_numeric_textid(self) -> None:
        args = pipeline._parser().parse_args(
            [
                "register",
                "--file",
                "样本公告.pdf",
                "--provider",
                "cninfo",
                "--security-code",
                "002484",
                "--exchange",
                "szse",
                "--filing-type",
                "other",
                "--title",
                "样本公告",
                "--announcement-date",
                "2026-04-10",
            ]
        )

        with self.assertRaisesRegex(ValueError, "numeric TEXTID"):
            pipeline._register_command(args, "样本公司股份有限公司")

    def test_exchange_inference_covers_b_shares_and_bse(self) -> None:
        cases = {
            "600519": "SSE",
            "900901": "SSE",
            "000001": "SZSE",
            "200771": "SZSE",
            "300750": "SZSE",
            "920047": "BSE",
            "430047": "BSE",
            "830799": "BSE",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(pipeline._exchange_for_scode(code), expected)

        with self.assertRaisesRegex(ValueError, "cannot infer exchange"):
            pipeline._exchange_for_scode("700001")

    def test_parse_failed_result_returns_nonzero(self) -> None:
        deps = _deps_type(
            parse_result=ParseDocumentResult(
                processing_run_id="run_1",
                status="failed",
                error={"error_code": "parser_invocation_failed"},
            )
        )

        code, stdout, stderr = _run_main(["parse", "--document-id", "doc_1"], deps)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["stage"], "parse")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["result"]["error"]["error_code"], "parser_invocation_failed")

    def test_build_units_failed_result_returns_nonzero(self) -> None:
        deps = _deps_type(
            build_result=BuildUnitsResult(
                processing_run_id="run_1",
                status="failed",
                error={"error_code": "ARTIFACT_WRITE_FAILED"},
            )
        )

        code, stdout, stderr = _run_main(
            ["build-units", "--document-id", "doc_1"],
            deps,
        )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["stage"], "build-units")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["result"]["error"]["error_code"], "ARTIFACT_WRITE_FAILED")

    def test_process_stops_after_parse_failed_result(self) -> None:
        deps = _deps_type(
            parse_result=ParseDocumentResult(
                processing_run_id="run_1",
                status="failed",
                error={"error_code": "parser_invocation_failed"},
            ),
            build_result=AssertionError("build-units must not run"),
            publish_result=AssertionError("publish must not run"),
        )

        code, stdout, stderr = _run_main(["process", "--document-id", "doc_1"], deps)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["stage"], "parse")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["result"]["error"]["error_code"], "parser_invocation_failed")

    def test_process_stops_after_build_failed_result(self) -> None:
        deps = _deps_type(
            parse_result=ParseDocumentResult(
                processing_run_id="run_1",
                status="succeeded",
            ),
            build_result=BuildUnitsResult(
                processing_run_id="run_1",
                status="failed",
                error={"error_code": "DB_WRITE_FAILED"},
            ),
            publish_result=AssertionError("publish must not run"),
        )

        code, stdout, stderr = _run_main(["process", "--document-id", "doc_1"], deps)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["stage"], "build-units")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["result"]["error"]["error_code"], "DB_WRITE_FAILED")

    def test_sync_command_prints_json_result(self) -> None:
        deps = _deps_type(sync_result={"company": "000001", "download_count": 0})

        code, stdout, stderr = _run_main(["sync", "--company", "000001", "--window", "7"], deps)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["company"], "000001")

    def test_sync_command_missing_checkpoint_returns_exit_2(self) -> None:
        deps = _deps_type(sync_result=ValueError("first sync requires explicit --window"))

        code, stdout, stderr = _run_main(["sync", "--company", "000001"], deps)

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("first sync requires explicit --window", stderr)

    def test_makefile_sync_target_has_required_usage(self) -> None:
        makefile = Path("Makefile").read_text(encoding="utf-8")

        self.assertIn(".PHONY:", makefile)
        self.assertIn("sync", makefile)
        self.assertIn("usage: make sync COMPANY=<scode> [WINDOW=N]", makefile)
        self.assertIn("disclosure_anchor.cli.pipeline sync --company $(COMPANY)", makefile)

    def test_register_reuses_resolved_ledger_legal_name(self) -> None:
        # No --company-legal-name given: reuse the ledger's real name for the
        # security rather than the announcement title.
        uow = FakeUnitOfWork()
        uow.companies.add(
            e.Company(company_id="co_1", legal_name="南通江海电容器股份有限公司")
        )
        uow.securities.add(
            e.Security(
                security_id="sec_1",
                company_id="co_1",
                security_code="002484",
                exchange="SZSE",
            )
        )
        capture: dict[str, object] = {}
        deps = _register_deps(uow, capture=capture, register_result=_register_result())

        code, stdout, stderr = _run_main(_register_argv(), deps)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("doc_1", stdout)
        command = capture["command"]
        self.assertEqual(
            getattr(command, "company_legal_name"), "南通江海电容器股份有限公司"
        )
        self.assertNotEqual(
            getattr(command, "company_legal_name"), getattr(command, "title")
        )

    def test_register_refuses_when_security_unknown(self) -> None:
        deps = _register_deps(FakeUnitOfWork())

        code, stdout, stderr = _run_main(_register_argv(), deps)

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--company-legal-name is required", stderr)

    def test_register_refuses_when_only_placeholder_name(self) -> None:
        uow = FakeUnitOfWork()
        uow.companies.add(
            e.Company(
                company_id="co_1",
                legal_name=f"{PENDING_LEGAL_NAME_PREFIX}002484.SZSE",
            )
        )
        uow.securities.add(
            e.Security(
                security_id="sec_1",
                company_id="co_1",
                security_code="002484",
                exchange="SZSE",
            )
        )
        deps = _register_deps(uow)

        code, stdout, stderr = _run_main(_register_argv(), deps)

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--company-legal-name is required", stderr)

    def test_register_uses_explicit_legal_name_over_placeholder(self) -> None:
        uow = FakeUnitOfWork()
        uow.companies.add(
            e.Company(
                company_id="co_1",
                legal_name=f"{PENDING_LEGAL_NAME_PREFIX}002484.SZSE",
            )
        )
        uow.securities.add(
            e.Security(
                security_id="sec_1",
                company_id="co_1",
                security_code="002484",
                exchange="SZSE",
            )
        )
        capture: dict[str, object] = {}
        deps = _register_deps(uow, capture=capture, register_result=_register_result())

        code, stdout, stderr = _run_main(
            _register_argv(legal_name="贵州茅台酒股份有限公司"), deps
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("doc_1", stdout)
        self.assertEqual(
            getattr(capture["command"], "company_legal_name"),
            "贵州茅台酒股份有限公司",
        )

    def test_track_export_csv_round_trips_names_with_commas(self) -> None:
        rows: list[dict[str, object]] = [
            {
                "security_code": "600519",
                "exchange": "SSE",
                "status": "active",
                "joined_date": date(2026, 1, 2),
                "lookback_days": "30",
                "sync_frequency": "daily",
                "process_classes": ["dividend", "meeting_resolution"],
                "legal_name": 'Acme, "Best" Holdings, Ltd.',
            }
        ]

        csv_text, exported, skipped = pipeline._render_watchlist_csv(rows)

        self.assertEqual(exported, 1)
        self.assertEqual(skipped, [])
        # Comment banner + column header stay byte-identical.
        self.assertTrue(
            csv_text.startswith(
                "# disclosure_anchor tracking-pool snapshot — exported from the"
                " DB (source of truth) by `make track-export`.\n"
            )
        )
        self.assertIn(
            "security_code,exchange,status,joined_date,lookback_days,"
            "sync_frequency,process_classes,note\n",
            csv_text,
        )
        # The comma/quote-bearing note is quoted, not split across columns.
        self.assertIn('"Acme, ""Best"" Holdings, Ltd."', csv_text)

        with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        ) as handle:
            handle.write(csv_text)
            path = handle.name
        try:
            entries = pipeline._track_entries(
                argparse.Namespace(codes=None, file=path)
            )
        finally:
            Path(path).unlink()

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.security_code, "600519")
        self.assertEqual(entry.exchange, "SSE")
        self.assertEqual(entry.status, "active")
        self.assertEqual(entry.lookback_days, 30)
        self.assertEqual(entry.sync_frequency, "daily")
        self.assertEqual(entry.process_classes, ("dividend", "meeting_resolution"))


def _register_argv(*, legal_name: str | None = None) -> list[str]:
    argv = [
        "register",
        "--file",
        "2026-04-10__periodic__002484__标题__1225087169.pdf",
        "--provider",
        "cninfo",
        "--security-code",
        "002484",
        "--exchange",
        "szse",
        "--filing-type",
        "other",
        "--title",
        "某公司2025年年度报告",
        "--announcement-date",
        "2026-04-10",
    ]
    if legal_name is not None:
        argv += ["--company-legal-name", legal_name]
    return argv


def _register_result() -> RegisterLocalPdfResult:
    return RegisterLocalPdfResult(
        document_id="doc_1",
        raw_file_relpath=None,
        raw_file_hash=None,
        source_access_id=None,
        outbox_event_id=None,
    )


def _register_deps(
    uow: FakeUnitOfWork,
    *,
    capture: dict[str, object] | None = None,
    register_result: RegisterLocalPdfResult | None = None,
):
    class _FakeRegisterUseCase:
        def execute(self, command):  # noqa: ANN001
            if capture is not None:
                capture["command"] = command
            return register_result

    class _FakeDeps:
        def __init__(self, settings) -> None:  # noqa: ANN001
            self.settings = settings
            self.engine = object()
            self.uow_factory = lambda: uow

        def register(self) -> _FakeRegisterUseCase:
            if capture is None:
                raise AssertionError("register must not run")
            return _FakeRegisterUseCase()

    return _FakeDeps


class _UseCase:
    def __init__(self, result: object) -> None:
        self.result = result

    def execute(self, command):  # noqa: ANN001
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _deps_type(
    *,
    parse_result: object | None = None,
    build_result: object | None = None,
    publish_result: object | None = None,
    sync_result: object | None = None,
):
    class _FakeDeps:
        def __init__(self, settings) -> None:  # noqa: ANN001
            self.settings = settings
            self.engine = object()

        def parser_options(self) -> ParserOptions:
            return ParserOptions()

        def parse(self) -> _UseCase:
            return _UseCase(parse_result)

        def build_units(self) -> _UseCase:
            return _UseCase(build_result)

        def publish(self) -> _UseCase:
            return _UseCase(publish_result)

        def sync(self, args):  # noqa: ANN001
            if isinstance(sync_result, Exception):
                raise sync_result
            return sync_result

    return _FakeDeps


def _run_main(argv: list[str], deps) -> tuple[int, str, str]:  # noqa: ANN001
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.object(pipeline, "load_settings", return_value=object()),
        patch.object(pipeline, "_Deps", deps),
        patch.object(
            pipeline,
            "exclusive_worker_admission",
            return_value=nullcontext(),
        ),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        code = pipeline.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
