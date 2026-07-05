"""Pipeline CLI parser tests."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from disclosure_anchor.cli import pipeline
from disclosure_anchor.application.use_cases.build_units import BuildUnitsResult
from disclosure_anchor.application.use_cases.parse_document import ParseDocumentResult


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

        command = pipeline._register_command(args)

        self.assertEqual(
            command.file_path,
            Path("2026-04-10__periodic__002484__江海股份：2025年年度报告__1225087169.pdf"),
        )
        self.assertEqual(command.provider_document_id, "1225087169")
        self.assertEqual(str(command.report_period), "2025A")
        self.assertEqual(command.company_legal_name, command.title)

    def test_register_command_defaults_provider_document_id_to_hash_fallback(self) -> None:
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

        command = pipeline._register_command(args)

        self.assertRegex(command.provider_document_id, r"^local-[a-f0-9]{16}$")

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
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        code = pipeline.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
