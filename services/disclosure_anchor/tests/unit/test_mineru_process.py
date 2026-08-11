"""Stable process and readiness mechanics for the pinned MinerU writer."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru_medium import process as mineru_process
from disclosure_anchor.adapters.parsers.mineru_medium.parser import (
    MinerUMediumDocumentParser,
)
from disclosure_anchor.adapters.parsers.mineru_medium.process import MinerUProcess
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.domain.errors import (
    ParserBackendOverloadedError,
    ParserCancelledError,
    ParserLocalInvocationError,
    ParserOutputContractError,
    ParserTaskDeadlineError,
    ParserTaskError,
    ParserVersionProbeError,
)


class MinerUProcessTests(unittest.TestCase):
    def tearDown(self) -> None:
        mineru_process._MINERU_SHUTDOWN_REQUESTED.clear()
        mineru_process._PROBE_SUCCESS_AT.clear()

    def test_command_uses_the_pinned_medium_profile(self) -> None:
        command = MinerUProcess(
            executable=Path("/opt/mineru/bin/mineru")
        ).command_for(
            input_pdf=Path("input.pdf"),
            output_dir=Path("out"),
            options=ParserOptions(
                server_url="http://gpu:30000",
                http_request_concurrency=3,
            ),
        )

        self.assertEqual(
            command[:5],
            ["/opt/mineru/bin/mineru", "-p", "input.pdf", "-o", "out"],
        )
        self.assertEqual(command[command.index("-m") + 1], "auto")
        self.assertEqual(
            command[command.index("-b") + 1], "hybrid-http-client"
        )
        self.assertEqual(command[command.index("-f") + 1], "true")
        self.assertEqual(command[command.index("-t") + 1], "true")
        self.assertEqual(command[command.index("-u") + 1], "http://gpu:30000")
        self.assertEqual(command[command.index("--max-concurrency") + 1], "3")
        self.assertEqual(command[command.index("--image-analysis") + 1], "false")
        self.assertEqual(command[command.index("--effort") + 1], "medium")
        self.assertNotIn("-s", command)
        self.assertNotIn("-e", command)

    def test_medium_parser_requires_exact_version_and_server_readiness(self) -> None:
        process = mock.Mock()
        process.version.return_value = "3.4.4"
        parser = MinerUMediumDocumentParser(
            process=process,
            server_url="http://gpu:30000",
        )
        options = ParserOptions(
            runtime_bundle_identity_sha256=f"sha256:{'a' * 64}"
        )

        self.assertEqual(parser.identity().version, "3.4.4")
        parser.readiness(options)
        process.probe_server.assert_called_once_with("http://gpu:30000")

        wrong = mock.Mock()
        wrong.version.return_value = "3.4.3"
        with self.assertRaises(ParserOutputContractError):
            MinerUMediumDocumentParser(process=wrong).identity()

        with self.assertRaises(ParserOutputContractError):
            MinerUMediumDocumentParser(
                process=process,
                parser_version="3.4.4",
            ).readiness(options)

    def test_run_aligns_deadline_and_classifies_process_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            input_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            options = ParserOptions(timeout_seconds=3600)
            runner = MinerUProcess(
                executable=Path("mineru"),
                extra_env={"MINERU_TABLE_MERGE_ENABLE": "0"},
            )
            self.assertEqual(mineru_process._task_result_timeout_seconds(600), 450)
            self.assertEqual(mineru_process._task_result_timeout_seconds(900), 675)
            self.assertEqual(mineru_process._task_result_timeout_seconds(901), 676)

            succeeded = mock.Mock(pid=101, returncode=0)
            succeeded.communicate.return_value = ("ok", "")
            with (
                mock.patch.dict(
                    mineru_process.os.environ,
                    {"MINERU_TABLE_MERGE_ENABLE": "0"},
                ),
                mock.patch.object(
                    mineru_process.subprocess,
                    "Popen",
                    return_value=succeeded,
                ) as popen,
            ):
                runner.run(
                    input_pdf=input_pdf,
                    output_dir=root / "success",
                    options=options,
                )
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["MINERU_TASK_RESULT_TIMEOUT_SECONDS"], "2700")
            self.assertNotIn("MINERU_TABLE_MERGE_ENABLE", env)

            with mock.patch.object(
                mineru_process.subprocess,
                "Popen",
                side_effect=OSError("not executable"),
            ):
                with self.assertRaises(ParserLocalInvocationError):
                    runner.run(
                        input_pdf=input_pdf,
                        output_dir=root / "spawn-error",
                        options=options,
                    )

            failures = (
                (
                    "Error: Timed out waiting for result of task task-1 for input.pdf",
                    ParserTaskDeadlineError,
                ),
                ('{"task_id":"task-2","status":"failed","error":""}', ParserTaskError),
                (
                    '{"task_id":"task-3","status":"failed",'
                    '"error":"HTTP 429 Too Many Requests"}',
                    ParserBackendOverloadedError,
                ),
                (
                    "Local mineru-api exited before becoming healthy.",
                    ParserLocalInvocationError,
                ),
            )
            for stderr, expected_error in failures:
                with self.subTest(expected_error=expected_error.__name__):
                    failed = mock.Mock(pid=102, returncode=1)
                    failed.communicate.return_value = ("", stderr)
                    with mock.patch.object(
                        mineru_process.subprocess,
                        "Popen",
                        return_value=failed,
                    ):
                        with self.assertRaises(expected_error):
                            runner.run(
                                input_pdf=input_pdf,
                                output_dir=root / expected_error.__name__,
                                options=options,
                            )

    def test_shutdown_kills_every_registered_process_group(self) -> None:
        process = mock.MagicMock(pid=43210)
        process.poll.return_value = None
        mineru_process._register_process(process)
        try:
            with mock.patch.object(mineru_process.os, "killpg") as killpg:
                terminated = mineru_process.terminate_active_mineru_processes(
                    grace_seconds=0
                )
        finally:
            mineru_process._unregister_process(process)

        self.assertEqual(terminated, 1)
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(43210, mineru_process.signal.SIGINT),
                mock.call(43210, mineru_process.signal.SIGKILL),
            ],
        )

    def test_worker_shutdown_is_not_classified_as_task_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            input_pdf.write_bytes(b"%PDF")
            process = mock.MagicMock(pid=43211, returncode=-9)
            process.poll.return_value = None

            def cancel_during_wait(*, timeout: float) -> tuple[str, str]:
                del timeout
                mineru_process.terminate_active_mineru_processes(grace_seconds=0)
                return "", ""

            process.communicate.side_effect = cancel_during_wait
            with (
                mock.patch.object(
                    mineru_process.subprocess,
                    "Popen",
                    return_value=process,
                ),
                mock.patch.object(mineru_process.os, "killpg"),
            ):
                with self.assertRaises(ParserCancelledError):
                    MinerUProcess(executable=Path("mineru")).run(
                        input_pdf=input_pdf,
                        output_dir=root / "out",
                        options=ParserOptions(timeout_seconds=60),
                    )

    def test_version_probe_is_strict_and_cleans_up_timeout(self) -> None:
        succeeded = mock.MagicMock(pid=54320, returncode=0)
        succeeded.communicate.return_value = ("mineru, version 3.4.4\n", "")
        with mock.patch.object(
            mineru_process.subprocess,
            "Popen",
            return_value=succeeded,
        ):
            self.assertEqual(
                MinerUProcess(executable=Path("mineru")).version(),
                "3.4.4",
            )

        malformed = mock.MagicMock(pid=54320, returncode=0)
        malformed.communicate.return_value = ("MinerU release 3.4.4", "")
        with mock.patch.object(
            mineru_process.subprocess,
            "Popen",
            return_value=malformed,
        ):
            with self.assertRaisesRegex(
                ParserVersionProbeError,
                "unsupported output contract",
            ):
                MinerUProcess(executable=Path("mineru")).version()

        process = mock.MagicMock(pid=54321, returncode=None)
        process.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["mineru", "-v"], timeout=0.01
        )
        with (
            mock.patch.object(
                mineru_process.subprocess,
                "Popen",
                return_value=process,
            ),
            mock.patch.object(mineru_process.os, "killpg") as killpg,
        ):
            with self.assertRaises(ParserVersionProbeError):
                MinerUProcess(
                    executable=Path("mineru"),
                    version_timeout_seconds=0.01,
                ).version()
        killpg.assert_called_once_with(54321, mineru_process.signal.SIGINT)

    def test_late_process_registration_is_cancelled_immediately(self) -> None:
        mineru_process._MINERU_SHUTDOWN_REQUESTED.set()
        process = mock.MagicMock(pid=54322)
        with mock.patch.object(mineru_process.os, "killpg") as killpg:
            cancelled = mineru_process._register_process(process)
        try:
            self.assertTrue(cancelled)
            killpg.assert_called_once_with(54322, mineru_process.signal.SIGINT)
            self.assertIn(process, mineru_process._CANCELLED_PROCESSES)
        finally:
            mineru_process._unregister_process(process)

    def test_probe_success_is_cached_but_failure_is_not(self) -> None:
        runner = MinerUProcess(executable=Path("mineru"))
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=None)
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        clock = iter([100.0, 100.5, 130.0])
        with (
            mock.patch.object(
                mineru_process.urllib.request,
                "build_opener",
                return_value=opener,
            ),
            mock.patch.object(
                mineru_process.time,
                "monotonic",
                side_effect=clock,
            ),
        ):
            runner.probe_server("http://gpu:30000")
            runner.probe_server("http://gpu:30000")
        self.assertEqual(opener.open.call_count, 1)

        mineru_process._PROBE_SUCCESS_AT.clear()
        opener.open.side_effect = OSError("connection refused")
        with mock.patch.object(
            mineru_process.urllib.request,
            "build_opener",
            return_value=opener,
        ):
            for _ in range(2):
                with self.assertRaises(ParserVersionProbeError):
                    runner.probe_server("http://gpu:30000")
        self.assertEqual(opener.open.call_count, 3)


if __name__ == "__main__":
    unittest.main()
