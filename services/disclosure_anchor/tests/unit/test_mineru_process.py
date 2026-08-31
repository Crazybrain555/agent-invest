"""Stable process and readiness mechanics for the pinned MinerU writer."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru_medium import process as mineru_process
from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import (
    PinnedArtifactReadResult,
)
from disclosure_anchor.adapters.parsers.mineru_medium.parser import (
    MinerUMediumDocumentParser,
)
from disclosure_anchor.adapters.parsers.mineru_medium.process import MinerUProcess
from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorError,
    mineru_orchestrator_incident_state,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.domain.errors import (
    ParserBackendUnavailableError,
    ParserBackendOverloadedError,
    ParserCancelledError,
    ParserLocalInvocationError,
    ParserOutputContractError,
    ParserTaskDeadlineError,
    ParserTaskError,
    ParserTimeoutError,
    ParserVersionProbeError,
)


class MinerUProcessTests(unittest.TestCase):
    def tearDown(self) -> None:
        mineru_process._MINERU_SHUTDOWN_REQUESTED.clear()
        mineru_process._PROBE_SUCCESS_AT.clear()

    def test_command_uses_the_pinned_medium_profile(self) -> None:
        command = MinerUProcess(executable=Path("/opt/mineru/bin/mineru")).command_for(
            input_pdf=Path("input.pdf"),
            output_dir=Path("out"),
            options=ParserOptions(
                api_url="http://127.0.0.1:30000",
                server_url="http://127.0.0.1:30001/v1",
                http_request_concurrency=3,
            ),
        )

        self.assertEqual(
            command[:5],
            ["/opt/mineru/bin/mineru", "-p", "input.pdf", "-o", "out"],
        )
        self.assertEqual(command[command.index("-m") + 1], "auto")
        self.assertEqual(command[command.index("-b") + 1], "hybrid-http-client")
        self.assertEqual(command[command.index("-f") + 1], "true")
        self.assertEqual(command[command.index("-t") + 1], "true")
        self.assertEqual(
            command[command.index("--api-url") + 1],
            "http://127.0.0.1:30000",
        )
        self.assertEqual(
            command[command.index("-u") + 1],
            "http://127.0.0.1:30001/v1",
        )
        self.assertNotIn("--max-concurrency", command)
        self.assertEqual(command[command.index("--image-analysis") + 1], "false")
        self.assertEqual(command[command.index("--effort") + 1], "medium")
        self.assertNotIn("-s", command)
        self.assertNotIn("-e", command)

        local_api_command = MinerUProcess(
            executable=Path("/opt/mineru/bin/mineru")
        ).command_for(
            input_pdf=Path("input.pdf"),
            output_dir=Path("out"),
            options=ParserOptions(
                server_url="http://gpu:30000",
                http_request_concurrency=3,
            ),
        )
        self.assertNotIn("--api-url", local_api_command)
        self.assertEqual(
            local_api_command[local_api_command.index("--max-concurrency") + 1],
            "3",
        )

    def test_external_api_drain_owner_is_released_on_success_and_failure(
        self,
    ) -> None:
        runner = MinerUProcess(executable=Path("/opt/mineru/bin/mineru"))
        options = ParserOptions(
            api_url="http://127.0.0.1:30002",
            api_drain_timeout_seconds=10,
        )
        active_counts: list[int] = []

        def observe_success(*_args: object, **_kwargs: object) -> object:
            active_counts.append(
                mineru_orchestrator_incident_state().drains_in_progress
            )
            return object()

        with mock.patch.object(
            mineru_process,
            "wait_for_mineru_orchestrator_idle",
            side_effect=observe_success,
        ):
            runner._drain_external_api(options)

        def observe_failure(*_args: object, **_kwargs: object) -> object:
            active_counts.append(
                mineru_orchestrator_incident_state().drains_in_progress
            )
            raise MinerUOrchestratorError("health transport failed")

        with (
            mock.patch.object(
                mineru_process,
                "wait_for_mineru_orchestrator_idle",
                side_effect=observe_failure,
            ),
            self.assertRaises(ParserBackendUnavailableError),
        ):
            runner._drain_external_api(options)

        self.assertEqual(active_counts, [1, 1])
        self.assertEqual(
            mineru_orchestrator_incident_state().drains_in_progress,
            0,
        )

    def test_medium_parser_requires_exact_version_and_server_readiness(self) -> None:
        process = mock.Mock()
        process.version.return_value = "3.4.4"
        parser = MinerUMediumDocumentParser(
            process=process,
            api_url="http://mac-api:30000",
            server_url="http://windows-vllm:30001/v1",
        )
        options = ParserOptions(runtime_bundle_identity_sha256=f"sha256:{'a' * 64}")

        self.assertEqual(parser.identity().version, "3.4.4")
        parser.readiness(options)
        self.assertEqual(
            process.probe_server.call_args_list,
            [mock.call("http://mac-api:30000")],
        )

        wrong = mock.Mock()
        wrong.version.return_value = "3.4.3"
        with self.assertRaises(ParserOutputContractError):
            MinerUMediumDocumentParser(process=wrong).identity()

        with self.assertRaises(ParserOutputContractError):
            MinerUMediumDocumentParser(
                process=process,
                parser_version="3.4.4",
            ).readiness(options)
        with self.assertRaisesRegex(
            ParserOutputContractError,
            "VLM upstream server URL",
        ):
            MinerUMediumDocumentParser(
                process=process,
                parser_version="3.4.4",
                api_url="http://mac-api:30000",
            ).readiness(options)

    def test_medium_parser_admits_document_and_location_in_one_reader_pass(
        self,
    ) -> None:
        process = mock.Mock()
        process.version.return_value = "3.4.4"
        process.run.return_value = SimpleNamespace(output_dir=Path("published-output"))
        reader = mock.Mock()
        document = mock.sentinel.provider_document
        reader.read_with_location.return_value = PinnedArtifactReadResult(
            document=document,
            artifact_root_relpath=PurePosixPath("nested/artifacts"),
        )
        parser = MinerUMediumDocumentParser(
            process=process,
            reader=reader,
            api_url="http://mac-api:30000",
            server_url="http://windows-vllm:30001/v1",
        )
        options = ParserOptions(runtime_bundle_identity_sha256=f"sha256:{'a' * 64}")

        result = parser.parse(
            input_pdf=Path("input.pdf"),
            output_dir=Path("requested-output"),
            options=options,
            source_pdf_sha256=f"sha256:{'b' * 64}",
        )

        reader.read_with_location.assert_called_once_with(
            Path("published-output"),
            source_pdf_sha256=f"sha256:{'b' * 64}",
        )
        reader.read.assert_not_called()
        reader.locate_artifact_root.assert_not_called()
        self.assertIs(result.provider_document, document)
        self.assertEqual(
            result.artifact_root,
            Path("published-output").absolute() / "nested" / "artifacts",
        )

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
            self.assertEqual(
                mineru_process._task_result_timeout_seconds(86400),
                84600,
            )

            succeeded = mock.Mock(pid=101, returncode=0)
            succeeded.communicate.return_value = ("ok", "")
            with (
                mock.patch.dict(
                    mineru_process.os.environ,
                    {
                        "DATABASE_URL": "postgresql://secret",
                        "CNINFO_PASSWORD": "secret",
                        "DISCLOSURE_ADMIN_TOKEN": "secret",
                        "MINERU_PROCESSING_WINDOW_SIZE": "16",
                        "MINERU_TABLE_MERGE_ENABLE": "0",
                        "PATH": "/usr/bin",
                    },
                    clear=True,
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
            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual(env["MINERU_PROCESSING_WINDOW_SIZE"], "16")
            self.assertNotIn("DATABASE_URL", env)
            self.assertNotIn("CNINFO_PASSWORD", env)
            self.assertNotIn("DISCLOSURE_ADMIN_TOKEN", env)
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
                    '{"task_id":"task-4","status":"failed",'
                    '"error":"Unexpected status code: [500]"}',
                    ParserBackendUnavailableError,
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

    def test_external_timeout_drains_remote_api_before_returning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            input_pdf.write_bytes(b"%PDF")
            process = mock.MagicMock(pid=43209, returncode=None)
            process.communicate.side_effect = subprocess.TimeoutExpired(
                cmd=["mineru"], timeout=1
            )
            process.wait.return_value = 0
            with (
                mock.patch.object(
                    mineru_process.subprocess,
                    "Popen",
                    return_value=process,
                ),
                mock.patch.object(mineru_process.os, "killpg"),
                mock.patch.object(
                    mineru_process,
                    "wait_for_mineru_orchestrator_idle",
                ) as drain,
                self.assertRaises(ParserTimeoutError),
            ):
                MinerUProcess(executable=Path("mineru")).run(
                    input_pdf=input_pdf,
                    output_dir=root / "out",
                    options=ParserOptions(
                        api_url="http://127.0.0.1:30002",
                        server_url="http://mineru-openai-server:30000/v1",
                        timeout_seconds=1,
                        api_drain_timeout_seconds=77,
                    ),
                )
            drain.assert_called_once_with(
                "http://127.0.0.1:30002",
                timeout_seconds=77.0,
            )

    def test_fixed_api_unknown_task_failure_is_shared_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            input_pdf.write_bytes(b"%PDF")
            process = mock.MagicMock(pid=43212, returncode=1)
            process.communicate.return_value = (
                "",
                "1 task(s) failed while processing documents: "
                '{"status":"failed","error":"GPU worker crashed unexpectedly"}',
            )
            with (
                mock.patch.object(
                    mineru_process.subprocess,
                    "Popen",
                    return_value=process,
                ),
                mock.patch.object(
                    mineru_process,
                    "wait_for_mineru_orchestrator_idle",
                ),
                self.assertRaises(ParserBackendUnavailableError),
            ):
                MinerUProcess(executable=Path("mineru")).run(
                    input_pdf=input_pdf,
                    output_dir=root / "out",
                    options=ParserOptions(
                        api_url="http://127.0.0.1:30002",
                        server_url="http://mineru-openai-server:30000/v1",
                    ),
                )

    def test_drain_failure_retains_bounded_primary_cli_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            input_pdf.write_bytes(b"%PDF")
            raw = "1 task(s) failed: GPU worker crashed token=top-secret " + "x" * 400
            process = mock.MagicMock(pid=43213, returncode=1)
            process.communicate.return_value = ("", raw)
            with (
                mock.patch.object(
                    mineru_process.subprocess,
                    "Popen",
                    return_value=process,
                ),
                mock.patch.object(
                    mineru_process,
                    "wait_for_mineru_orchestrator_idle",
                    side_effect=MinerUOrchestratorError("health transport failed"),
                ),
                self.assertRaises(ParserBackendUnavailableError) as raised,
            ):
                MinerUProcess(executable=Path("mineru")).run(
                    input_pdf=input_pdf,
                    output_dir=root / "out",
                    options=ParserOptions(
                        api_url="http://127.0.0.1:30002",
                        server_url="http://mineru-openai-server:30000/v1",
                    ),
                )

        message = str(raised.exception)
        self.assertIn("primary_type=ParserBackendUnavailableError", message)
        self.assertIn("GPU worker crashed", message)
        self.assertIn("cli_sha256=", message)
        self.assertIn("...[truncated]", message)
        self.assertIn("remote task drain could not be proved", message)
        self.assertNotIn("top-secret", message)

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
        self.assertEqual(
            opener.open.call_args.args[0].full_url,
            "http://gpu:30000/health",
        )

        mineru_process._PROBE_SUCCESS_AT.clear()
        clock = iter([200.0, 200.5])
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
            runner.probe_server("http://windows-vllm:30001/v1")
        self.assertEqual(
            opener.open.call_args.args[0].full_url,
            "http://windows-vllm:30001/health",
        )

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
        self.assertEqual(opener.open.call_count, 4)


if __name__ == "__main__":
    unittest.main()
