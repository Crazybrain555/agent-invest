"""MinerU CLI process wrapper."""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.domain.errors import (
    ParserInvocationError,
    ParserTimeoutError,
    ParserVersionProbeError,
)


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


@dataclass(frozen=True)
class MinerUProcessResult:
    output_dir: Path
    stdout: str
    stderr: str


class MinerUProcess:
    """Run the local MinerU CLI in a controlled subprocess."""

    def __init__(
        self,
        *,
        executable: Path,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._executable = executable
        self._extra_env = extra_env or {}

    def command_for(
        self, *, input_pdf: Path, output_dir: Path, options: ParserOptions
    ) -> list[str]:
        command = [
            str(self._executable),
            "-p",
            str(input_pdf),
            "-o",
            str(output_dir),
            "-m",
            options.method,
            "-b",
            options.backend,
            "-l",
            options.language,
            "-f",
            str(options.formula).lower(),
            "-t",
            str(options.table).lower(),
        ]
        if options.start_page is not None:
            command.extend(["-s", str(options.start_page)])
        if options.end_page is not None:
            command.extend(["-e", str(options.end_page)])
        if options.server_url:
            # *-http-client backends offload VLM inference to a remote
            # mineru-openai-server (GPU box); mineru ignores -u otherwise.
            command.extend(["-u", options.server_url])
        return command

    def version(self) -> str:
        try:
            completed = subprocess.run(
                [str(self._executable), "-v"],
                check=True,
                capture_output=True,
                text=True,
                env=self._env(),
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ParserVersionProbeError(
                f"MinerU version probe failed: {self._executable}"
            ) from exc
        output = (completed.stdout or completed.stderr).strip()
        if not output:
            raise ParserVersionProbeError(
                f"MinerU version probe returned no output: {self._executable}"
            )
        return output

    def run(
        self, *, input_pdf: Path, output_dir: Path, options: ParserOptions
    ) -> MinerUProcessResult:
        if not input_pdf.is_file():
            raise ParserInvocationError(f"parser input PDF is missing: {input_pdf}")
        output_dir.mkdir(parents=True, exist_ok=True)
        # MinerU 3.4 spawns a local fast_api backend; killing only the direct
        # child on timeout leaves that orphan resident (observed 1.8GB). Run
        # the CLI as its own process group and clean up the whole group (08 §2).
        try:
            process = subprocess.Popen(
                self.command_for(input_pdf=input_pdf, output_dir=output_dir, options=options),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._env(),
                start_new_session=True,
            )
        except OSError as exc:
            raise ParserInvocationError(f"MinerU parse failed: {exc}") from exc
        try:
            stdout, stderr = process.communicate(timeout=options.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(process)
            raise ParserTimeoutError(
                f"MinerU timed out after {options.timeout_seconds}s"
            ) from exc
        except BaseException:
            _kill_process_group(process)
            raise
        if process.returncode != 0:
            detail = f": {stderr.strip()}" if stderr else ""
            raise ParserInvocationError(f"MinerU parse failed{detail}")
        return MinerUProcessResult(
            output_dir=output_dir,
            stdout=stdout,
            stderr=stderr,
        )

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Local Phase 00 validation showed httpx can fail through proxy env
        # unless socks extras are installed. MinerU uses local model cache here.
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env.pop(key, None)
            env.pop(key.lower(), None)
        env["NO_PROXY"] = "*"
        env.update(self._extra_env)
        return env
