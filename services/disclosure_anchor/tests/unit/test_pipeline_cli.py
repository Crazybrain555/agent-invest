"""Pipeline CLI parser tests."""

from __future__ import annotations

import argparse
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import date
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from disclosure_anchor.cli import pipeline
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    MinerUDeploymentChecker,
    MinerUDeploymentGateError,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.services.subject_resolver import (
    PENDING_LEGAL_NAME_PREFIX,
)
from disclosure_anchor.application.use_cases.build_units import BuildUnitsResult
from disclosure_anchor.application.use_cases.parse_document import ParseDocumentResult
from disclosure_anchor.application.use_cases.register_local_pdf import (
    RegisterLocalPdfResult,
)
from disclosure_anchor.application.use_cases.track_companies import (
    TrackCompaniesResult,
    TrackEntryResult,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import ConfigurationError

from tests.unit._fakes import FakeUnitOfWork


class PipelineCliTests(unittest.TestCase):
    def test_production_parser_composition_keeps_api_and_upstream_distinct(
        self,
    ) -> None:
        deps = pipeline._Deps.__new__(pipeline._Deps)
        deps.settings = SimpleNamespace(
            disclosure_mineru_bin=Path("/opt/mineru/bin/mineru"),
            disclosure_mineru_api_url="http://127.0.0.1:30000",
            disclosure_mineru_inference_upstream_url=("http://127.0.0.1:30001/v1"),
            mineru_http_request_concurrency=7,
            disclosure_mineru_runtime_bundle_identity_sha256=("sha256:" + "a" * 64),
            disclosure_parse_runaway_timeout_seconds=86400,
        )
        deps.provider_source = MagicMock()
        deps.paths = MagicMock()
        deps.artifacts = MagicMock()
        deps.uow_factory = MagicMock()

        options = deps.parser_options()
        self.assertEqual(options.api_url, "http://127.0.0.1:30000")
        self.assertEqual(options.server_url, "http://127.0.0.1:30001/v1")

        with (
            patch.object(pipeline, "MinerUProcess") as process,
            patch.object(pipeline, "MinerUMediumDocumentParser") as parser,
            patch.object(pipeline, "ParseDocument") as parse_use_case,
            patch.object(pipeline, "RawDocumentStore"),
        ):
            deps.parse()

        process.assert_called_once_with(executable=Path("/opt/mineru/bin/mineru"))
        parser.assert_called_once_with(
            process=process.return_value,
            api_url="http://127.0.0.1:30000",
            server_url="http://127.0.0.1:30001/v1",
        )
        parse_use_case.assert_called_once()

    def test_parse_gate_failure_precedes_dependency_and_database_composition(
        self,
    ) -> None:
        checker = MagicMock(spec=MinerUDeploymentChecker)
        checker.assert_admission.side_effect = MinerUDeploymentGateError(
            "GPU identity unavailable"
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch.object(pipeline, "load_settings", return_value=object()),
            patch.object(
                pipeline,
                "MinerUDeploymentChecker",
                return_value=checker,
            ),
            patch.object(pipeline, "_Deps") as deps,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = pipeline.main(["parse", "--document-id", "doc_1"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("GPU identity unavailable", stderr.getvalue())
        deps.assert_not_called()

    def test_track_file_and_codes_are_mutually_exclusive(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pipeline._parser().parse_args(
                ["track", "--file", "watchlist.csv", "--codes", "600519"]
            )

    def test_destructive_company_purge_is_not_a_cli_command(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pipeline._parser().parse_args(
                ["purge-company", "--code", "600519", "--yes"]
            )

    def test_pipeline_database_url_never_falls_back_to_migration_owner(self) -> None:
        settings = MagicMock(
            database_url=None,
            disclosure_migration_database_url="postgresql+psycopg://owner/db",
        )

        with self.assertRaisesRegex(ConfigurationError, "DATABASE_URL"):
            pipeline._database_url(settings)

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

    def test_register_command_defaults_provider_document_id_to_safe_suffix(
        self,
    ) -> None:
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
            Path(
                "2026-04-10__periodic__002484__江海股份：2025年年度报告__1225087169.pdf"
            ),
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
        self.assertEqual(
            payload["result"]["error"]["error_code"], "parser_invocation_failed"
        )

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
        self.assertEqual(
            payload["result"]["error"]["error_code"], "ARTIFACT_WRITE_FAILED"
        )

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
        self.assertEqual(
            payload["result"]["error"]["error_code"], "parser_invocation_failed"
        )

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

        code, stdout, stderr = _run_main(
            ["sync", "--company", "000001", "--window", "7"], deps
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["company"], "000001")

    def test_sync_command_missing_checkpoint_returns_exit_2(self) -> None:
        deps = _deps_type(
            sync_result=ValueError("first sync requires explicit --window")
        )

        code, stdout, stderr = _run_main(["sync", "--company", "000001"], deps)

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("first sync requires explicit --window", stderr)

    def test_makefile_sync_target_has_required_usage(self) -> None:
        makefile = Path("Makefile").read_text(encoding="utf-8")

        self.assertIn(".PHONY:", makefile)
        self.assertIn("sync", makefile)
        self.assertIn("usage: make sync COMPANY=<scode> [WINDOW=N]", makefile)
        self.assertIn(
            "disclosure_anchor.cli.pipeline sync --company $(COMPANY)", makefile
        )

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
            entries = pipeline._track_entries(argparse.Namespace(codes=None, file=path))
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

    def test_track_parses_the_same_snapshot_it_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watchlist.csv"
            path.write_text(
                "security_code,exchange,status,joined_date,process_classes\n"
                "600519,SSE,active,2026-08-23,\n",
                encoding="utf-8",
            )

            def mutate_after_snapshot(*_args: object, **_kwargs: object) -> list[str]:
                path.write_text(
                    "security_code,exchange,status,joined_date,process_classes\n"
                    "000001,SZSE,active,2026-08-23,\n",
                    encoding="utf-8",
                )
                return []

            with patch.object(
                pipeline,
                "validate_watchlist_snapshot",
                side_effect=mutate_after_snapshot,
            ):
                entries = pipeline._track_entries(
                    argparse.Namespace(
                        codes=None,
                        file=path,
                        screen_manifest=None,
                    )
                )

        self.assertEqual(
            [(item.security_code, item.exchange) for item in entries],
            [("600519", "SSE")],
        )

    def test_invalid_track_file_fails_before_database_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watchlist.csv"
            path.write_text(
                "security_code,exchange,status,joined_date,process_classes\n"
                "600519,SZSE,active,2026-08-23,\n",
                encoding="utf-8",
            )

            class ForbiddenDeps:
                def __init__(self, _settings: object) -> None:
                    raise AssertionError("DB dependencies must not be built")

            code, stdout, stderr = _run_main(
                ["track", "--file", str(path)],
                ForbiddenDeps,
            )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("belongs to SSE, not SZSE", stderr)

    def test_file_import_skips_post_commit_profile_resolution(self) -> None:
        result = _track_result(1)
        calls: list[tuple[tuple[str, str], ...]] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watchlist.csv"
            path.write_text(
                "security_code,exchange,status,joined_date,process_classes\n"
                "600519,SSE,active,2026-08-23,\n",
                encoding="utf-8",
            )
            code, _stdout, stderr = _run_main(
                ["track", "--file", str(path)],
                _track_deps(result, calls),
            )

        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertIn("audited first sync", stderr)

    def test_direct_code_profile_compatibility_is_capped_at_twenty(self) -> None:
        for count, expected_calls in ((20, 1), (21, 0)):
            with self.subTest(count=count):
                result = _track_result(count)
                calls: list[tuple[tuple[str, str], ...]] = []
                codes = ",".join(f"{600000 + index:06d}" for index in range(count))

                code, _stdout, _stderr = _run_main(
                    ["track", "--codes", codes],
                    _track_deps(result, calls),
                )

                self.assertEqual(code, 0)
                self.assertEqual(len(calls), expected_calls)

    def test_makefile_boolean_tokens_are_closed_allowlists(self) -> None:
        makefile = Path("Makefile").read_text(encoding="utf-8")

        self.assertIn('case "$(PRUNE_DRIFT)" in ""|YES)', makefile)
        self.assertIn('case "$(DRY_RUN)" in ""|1)', makefile)
        self.assertIn(
            'case "$(SKIP_PROFILE_RESOLUTION)" in ""|YES)',
            makefile,
        )
        self.assertEqual(
            makefile.count('case "$(ALLOW_EMPTY)" in ""|YES)'),
            3,
        )
        self.assertEqual(
            makefile.count("$(if $(filter YES,$(ALLOW_EMPTY)),--allow-empty)"),
            3,
        )
        self.assertIn(
            "usage: make track-export OUT=/timestamped/path/watchlist.csv",
            makefile,
        )
        self.assertNotIn("$(or $(OUT),config/watchlist.csv)", makefile)
        self.assertNotIn("purge-company", makefile)
        self.assertNotIn('case "$(strip $(PRUNE_DRIFT))"', makefile)

    def test_make_track_rejects_invalid_tokens_and_conflicting_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watchlist.csv"
            path.write_text(
                "security_code,exchange,status,joined_date,process_classes\n"
                "600519,SSE,active,2026-08-23,\n",
                encoding="utf-8",
            )
            clean_env = os.environ.copy()
            for name in (
                "PRUNE_DRIFT",
                "DRY_RUN",
                "SKIP_PROFILE_RESOLUTION",
                "FILE",
                "CODES",
                "SCREEN_MANIFEST",
            ):
                clean_env.pop(name, None)
            cases = (
                ([f"FILE={path}", "PRUNE_DRIFT=1"], "PRUNE_DRIFT"),
                ([f"FILE={path}", "DRY_RUN=YES"], "DRY_RUN"),
                (
                    [f"FILE={path}", "SKIP_PROFILE_RESOLUTION=1"],
                    "SKIP_PROFILE_RESOLUTION",
                ),
                ([f"FILE={path}", "CODES=600519"], "mutually exclusive"),
            )
            for variables, message in cases:
                with self.subTest(variables=variables):
                    completed = subprocess.run(
                        ["make", "track", *variables],
                        cwd=Path(__file__).resolve().parents[2],
                        env=clean_env,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 2, completed.stderr)
                    self.assertIn(message, completed.stderr)


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


def _track_result(count: int) -> TrackCompaniesResult:
    return TrackCompaniesResult(
        results=tuple(
            TrackEntryResult(
                security_code=f"{600000 + index:06d}",
                exchange="SSE",
                tracked_company_id=f"tc_{index}",
                company_id=f"co_{index}",
                created=True,
            )
            for index in range(count)
        )
    )


def _track_deps(
    result: TrackCompaniesResult,
    profile_calls: list[tuple[tuple[str, str], ...]],
):
    class _TrackDeps:
        def __init__(self, settings: object) -> None:
            self.settings = settings
            self.engine = object()

        def track(self) -> _UseCase:
            return _UseCase(result)

        def resolve_profiles(self, codes: tuple[tuple[str, str], ...]) -> tuple[()]:
            profile_calls.append(codes)
            return ()

    return _TrackDeps


def _run_main(argv: list[str], deps) -> tuple[int, str, str]:  # noqa: ANN001
    stdout = io.StringIO()
    stderr = io.StringIO()
    checker = MagicMock(spec=MinerUDeploymentChecker)
    with (
        patch.object(pipeline, "load_settings", return_value=object()),
        patch.object(
            pipeline,
            "MinerUDeploymentChecker",
            return_value=checker,
        ),
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
