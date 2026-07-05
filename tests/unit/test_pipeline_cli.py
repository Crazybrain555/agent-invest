"""Pipeline CLI parser tests."""

from __future__ import annotations

from pathlib import Path
import unittest

from disclosure_anchor.cli import pipeline


class PipelineCliTests(unittest.TestCase):
    def test_subcommands_are_registered(self) -> None:
        parser = pipeline._parser()
        cases = {
            "parse": ["parse", "--document-id", "doc_1"],
            "build-units": ["build-units", "--document-id", "doc_1"],
            "publish": ["publish", "--processing-run-id", "run_1"],
            "process": ["process", "--document-id", "doc_1"],
        }
        for command, argv in cases.items():
            with self.subTest(command=command):
                self.assertEqual(parser.parse_args(argv).command, command)

    def test_register_command_defaults_provider_document_id_to_file_stem(self) -> None:
        args = pipeline._parser().parse_args(
            [
                "register",
                "--file",
                "sample.pdf",
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

        self.assertEqual(command.file_path, Path("sample.pdf"))
        self.assertEqual(command.provider_document_id, "sample")
        self.assertEqual(str(command.report_period), "2025A")
        self.assertEqual(command.company_legal_name, command.title)


if __name__ == "__main__":
    unittest.main()
