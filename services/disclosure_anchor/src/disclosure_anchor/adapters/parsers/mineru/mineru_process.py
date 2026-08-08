"""MinerU CLI process wrapper."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.domain.errors import (
    ParserBackendOverloadedError,
    ParserCancelledError,
    ParserInvocationError,
    ParserLocalInvocationError,
    ParserTaskDeadlineError,
    ParserTaskError,
    ParserTimeoutError,
    ParserVersionProbeError,
    RemoteModelAmbiguousError,
)

LOGGER = logging.getLogger(__name__)

_ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
_CANCELLED_PROCESSES: set[subprocess.Popen[str]] = set()
_ACTIVE_PROCESSES_LOCK = threading.RLock()
_MINERU_SHUTDOWN_REQUESTED = threading.Event()
_GRACEFUL_STOP_SECONDS = 35.0

_PROBE_SUCCESS_AT: dict[str, float] = {}
_PROBE_CACHE_LOCK = threading.Lock()
_PROBE_SUCCESS_TTL_SECONDS = 60.0
_TASK_RESULT_TIMEOUT_MARKER = "Timed out waiting for result of task"
_LOCAL_API_FAILURE_MARKERS = (
    "Local mineru-api exited before becoming healthy.",
    "Timed out waiting for local mineru-api to become healthy.",
)
_BACKEND_OVERLOAD_MARKERS = (
    "429 Too Many Requests",
    "HTTP 429",
    "status_code=429",
    '"status_code": 429',
    "Unexpected status code: [429]",
    "RESOURCE_EXHAUSTED",
    "resource_exhausted",
)
_LOCAL_API_STARTUP_TIMEOUT_SECONDS = 120
_TASK_RESULT_DOWNLOAD_TIMEOUT_SECONDS = 120
# MinerU's task-result wait starts only after temporary API startup and upload.
# Keep an explicit phase budget inside the outer process SLA for startup
# (120s), its fixed submit HTTP timeouts (up to ~400s), local result download,
# shutdown/extraction and cleanup. The outer communicate() deadline remains
# the final absolute bound if a streaming phase keeps making partial progress.
_MIN_TASK_RESULT_RESERVE_SECONDS = 900
_MAX_TASK_RESULT_RESERVE_SECONDS = 1800
_CLICK_VERSION_OUTPUT = re.compile(
    r"^[^,\r\n]+, version "
    r"(?P<version>[0-9]+(?:\.[0-9]+)*(?:[A-Za-z0-9.+_-]*)?)$"
)


def _task_result_timeout_seconds(outer_timeout_seconds: int) -> int:
    """Reserve the bounded pre-task and cleanup phases from the overall SLA."""

    if outer_timeout_seconds <= 1:
        return 1
    nominal_reserve = min(
        _MAX_TASK_RESULT_RESERVE_SECONDS,
        max(
            _MIN_TASK_RESULT_RESERVE_SECONDS,
            outer_timeout_seconds // 10,
        ),
    )
    # ParserOptions remains a public/direct-call surface and historically
    # permits deliberately short SLAs. Cap phase reserve at 25% so task wait
    # retains at least 75%, continuously across the 900s nominal boundary.
    reserve = min(nominal_reserve, max(1, outer_timeout_seconds // 4))
    return max(1, outer_timeout_seconds - reserve)


def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
    return any(marker in value for marker in markers)


def _parse_cli_version_output(output: str) -> str:
    """Extract Click's version token without weakening identity equality."""

    match = _CLICK_VERSION_OUTPUT.fullmatch(output.strip())
    if match is None:
        raise ParserVersionProbeError(
            "MinerU version probe returned an unsupported output contract"
        )
    return match.group("version")


def _register_process(process: subprocess.Popen[str]) -> bool:
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.add(process)
        cancel_now = _MINERU_SHUTDOWN_REQUESTED.is_set()
        if cancel_now:
            _CANCELLED_PROCESSES.add(process)
    if cancel_now:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
    return cancel_now


def _unregister_process(process: subprocess.Popen[str]) -> bool:
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.discard(process)
        cancelled = process in _CANCELLED_PROCESSES
        _CANCELLED_PROCESSES.discard(process)
    return cancelled


def terminate_active_mineru_processes(
    *, grace_seconds: float = _GRACEFUL_STOP_SECONDS
) -> int:
    """Gracefully stop active MinerU groups, then force only true stragglers.

    Registry marking lets the parse thread distinguish an operator restart
    from an item failure, so a deploy never consumes the PDF's retry budget.
    SIGINT is intentional: MinerU's official client catches it and stops its
    separate-session temporary API before removing the temporary directory.
    The process-lifetime latch closes the snapshot/register race.
    """

    _MINERU_SHUTDOWN_REQUESTED.set()
    with _ACTIVE_PROCESSES_LOCK:
        processes = tuple(
            process
            for process in _ACTIVE_PROCESSES
            if process.poll() is None
        )
        _CANCELLED_PROCESSES.update(processes)
    terminated = 0
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            with _ACTIVE_PROCESSES_LOCK:
                _CANCELLED_PROCESSES.discard(process)
            continue
        terminated += 1
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while (
        any(process.poll() is None for process in processes)
        and time.monotonic() < deadline
    ):
        time.sleep(max(0.0, min(0.05, deadline - time.monotonic())))
    for process in processes:
        if process.poll() is not None:
            continue
        LOGGER.warning(
            "MinerU PID %s exceeded graceful shutdown; forcing exit",
            process.pid,
        )
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return terminated


def _stop_process_group(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float = _GRACEFUL_STOP_SECONDS,
) -> None:
    """Give MinerU's official cleanup path time before a forced kill."""

    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=max(0.0, grace_seconds))
        return
    except subprocess.TimeoutExpired:
        LOGGER.warning(
            "MinerU PID %s exceeded graceful cleanup; forcing exit",
            process.pid,
        )
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    process.wait()


@dataclass(frozen=True)
class MinerUProcessResult:
    output_dir: Path
    stdout: str
    stderr: str


@dataclass(frozen=True)
class MinerURuntimeHelperResult:
    returncode: int
    stdout: str
    stderr: str


class MinerUProcess:
    """Run the local MinerU CLI in a controlled subprocess."""

    def __init__(
        self,
        *,
        executable: Path,
        extra_env: dict[str, str] | None = None,
        version_timeout_seconds: float = 10.0,
    ) -> None:
        self._executable = executable
        self._extra_env = extra_env or {}
        self._version_timeout_seconds = version_timeout_seconds

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
        if options.backend != "pipeline":
            command.extend(
                ["--image-analysis", str(options.image_analysis).lower()]
            )
        if options.backend.startswith("hybrid-"):
            command.extend(["--effort", options.effort])
        if options.start_page is not None:
            command.extend(["-s", str(options.start_page)])
        if options.end_page is not None:
            command.extend(["-e", str(options.end_page)])
        if options.server_url:
            # *-http-client backends offload VLM inference to a remote
            # mineru-openai-server (GPU box); mineru ignores -u otherwise.
            command.extend(["-u", options.server_url])
        if (
            options.backend.endswith("-http-client")
            and options.http_request_concurrency is not None
        ):
            if options.http_request_concurrency < 1:
                raise ValueError(
                    "http_request_concurrency must be positive when configured"
                )
            # MinerU 3.4 deliberately accepts unknown CLI options and forwards
            # them to its temporary local mineru-api. The API normalizes this
            # option to max_concurrency for the remote HTTP client backend.
            command.extend(
                [
                    "--max-concurrency",
                    str(options.http_request_concurrency),
                ]
            )
        return command

    @property
    def runtime_python(self) -> Path:
        """Return the venv sibling Python without resolving its symlink.

        Resolving this path escapes the MinerU virtualenv to its base
        interpreter, where ``mineru_vl_utils`` is not installed.
        """

        return self._executable.parent / "python"

    def run_runtime_helper(
        self,
        *,
        script: Path,
        input_payload: str,
        options: ParserOptions,
    ) -> MinerURuntimeHelperResult:
        """Run one bounded helper inside the attested MinerU environment."""

        runtime_python = self.runtime_python
        if not runtime_python.is_file():
            raise ParserLocalInvocationError(
                f"MinerU runtime Python is missing: {runtime_python}"
            )
        if not script.is_file():
            raise ParserLocalInvocationError(
                f"MinerU runtime helper is missing: {script}"
            )
        try:
            process = subprocess.Popen(
                [str(runtime_python), str(script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._env(options=options),
                start_new_session=True,
            )
        except OSError as exc:
            raise ParserLocalInvocationError(
                f"MinerU runtime helper failed to start: {exc}"
            ) from exc
        cancel_at_start = _register_process(process)
        cancelled = False
        timeout = options.timeout_seconds or 600
        if cancel_at_start:
            timeout = min(timeout, int(_GRACEFUL_STOP_SECONDS))
        try:
            stdout, stderr = process.communicate(
                input=input_payload,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            _stop_process_group(process)
            if cancel_at_start:
                raise ParserCancelledError(
                    "MinerU runtime helper cancelled by worker shutdown"
                ) from exc
            raise ParserTimeoutError(
                f"MinerU runtime helper timed out after {timeout}s"
            ) from exc
        except BaseException:
            _stop_process_group(process)
            raise
        finally:
            cancelled = _unregister_process(process)
        if cancelled:
            raise ParserCancelledError(
                "MinerU runtime helper cancelled by worker shutdown"
            )
        return MinerURuntimeHelperResult(
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def resolve_server_model(
        self, server_url: str, *, timeout_seconds: float = 15.0
    ) -> str:
        """Return the single model the OpenAI-compatible backend serves.

        The remote model is part of the parse target identity: it must be
        known before a run is created and unchanged when the run finishes.
        Transport failures are an infrastructure outage; anything other
        than exactly one served model is an operator configuration state
        and fails closed without retry.
        """

        url = server_url.rstrip("/") + "/v1/models"
        request = urllib.request.Request(url, method="GET")
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
        ) as exc:
            raise ParserVersionProbeError(
                f"MinerU backend model listing unavailable: {server_url}"
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        models: list[str] | None = None
        if isinstance(data, list):
            models = [
                model_id
                for item in data
                if isinstance(item, dict)
                and isinstance(model_id := item.get("id"), str)
                and model_id.strip()
            ]
        if models is None or len(models) != 1:
            raise RemoteModelAmbiguousError(
                "MinerU backend must serve exactly one model, got "
                f"{sorted(models) if models else models}: {server_url}"
            )
        return models[0]

    def probe_server(
        self, server_url: str, *, timeout_seconds: float = 15.0
    ) -> None:
        """Fail loudly when the remote VLM backend is unreachable.

        The remote server is part of the parser stack for *-http-client
        backends: dispatching a batch against a dead server would burn one
        parse-retry per document for an infrastructure condition.  Any HTTP
        answer below 500 proves a listening server (a 404 on /health is
        still a live server); connection errors, timeouts, and 5xx are an
        outage.

        A success is cached briefly and probes are generous with time:
        readiness can run before each admission wave, and a server busy with
        continuous batching answers /health slowly — repeated tight probes
        manufacture outage evidence from our own load (k8s/Envoy convention:
        lenient probes, rate-based breakers).
        """

        with _PROBE_CACHE_LOCK:
            cached_at = _PROBE_SUCCESS_AT.get(server_url)
            if (
                cached_at is not None
                and time.monotonic() - cached_at < _PROBE_SUCCESS_TTL_SECONDS
            ):
                return
        url = server_url.rstrip("/") + "/health"
        request = urllib.request.Request(url, method="GET")
        # The backend lives on the LAN/tailnet: never route the probe
        # through proxy env vars (a proxy that cannot reach the private
        # address would report a healthy server as an outage — the same
        # reason _env() strips proxies for the mineru subprocess).
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )
        try:
            with opener.open(request, timeout=timeout_seconds):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise ParserVersionProbeError(
                    f"MinerU backend server unhealthy ({exc.code}): {server_url}"
                ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ParserVersionProbeError(
                f"MinerU backend server unreachable: {server_url}"
            ) from exc
        with _PROBE_CACHE_LOCK:
            _PROBE_SUCCESS_AT[server_url] = time.monotonic()

    def version(self) -> str:
        try:
            process = subprocess.Popen(
                [str(self._executable), "-v"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._env(),
                start_new_session=True,
            )
        except OSError as exc:
            raise ParserVersionProbeError(
                f"MinerU version probe failed: {self._executable}"
            ) from exc
        cancel_at_start = _register_process(process)
        cancelled = False
        try:
            stdout, stderr = process.communicate(
                timeout=(
                    min(self._version_timeout_seconds, _GRACEFUL_STOP_SECONDS)
                    if cancel_at_start
                    else self._version_timeout_seconds
                )
            )
        except subprocess.TimeoutExpired as exc:
            _stop_process_group(process)
            if cancel_at_start:
                raise ParserVersionProbeError(
                    "MinerU version probe cancelled by worker shutdown"
                ) from exc
            raise ParserVersionProbeError(
                "MinerU version probe timed out after "
                f"{self._version_timeout_seconds}s: {self._executable}"
            ) from exc
        except BaseException:
            _stop_process_group(process)
            raise
        finally:
            cancelled = _unregister_process(process)
        if cancelled:
            raise ParserVersionProbeError(
                "MinerU version probe cancelled by worker shutdown"
            )
        if process.returncode != 0:
            raise ParserVersionProbeError(
                f"MinerU version probe failed: {self._executable}"
            )
        output = (stdout or stderr).strip()
        if not output:
            raise ParserVersionProbeError(
                f"MinerU version probe returned no output: {self._executable}"
            )
        return _parse_cli_version_output(output)

    def run(
        self, *, input_pdf: Path, output_dir: Path, options: ParserOptions
    ) -> MinerUProcessResult:
        if not input_pdf.is_file():
            raise ParserInvocationError(f"parser input PDF is missing: {input_pdf}")
        output_dir.mkdir(parents=True, exist_ok=True)
        # MinerU 3.4 may put its temporary fast_api in a separate session.
        # SIGINT reaches the official CLI cleanup path, which stops that API;
        # the outer process group is only the bounded fallback for the CLI
        # and its same-session descendants.
        try:
            process = subprocess.Popen(
                self.command_for(input_pdf=input_pdf, output_dir=output_dir, options=options),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._env(options=options),
                start_new_session=True,
            )
        except OSError as exc:
            raise ParserLocalInvocationError(
                f"MinerU local process failed to start: {exc}"
            ) from exc
        cancel_at_start = _register_process(process)
        cancelled = False
        communicate_timeout: float | None = options.timeout_seconds
        if cancel_at_start:
            communicate_timeout = min(
                communicate_timeout or _GRACEFUL_STOP_SECONDS,
                _GRACEFUL_STOP_SECONDS,
            )
        try:
            stdout, stderr = process.communicate(timeout=communicate_timeout)
        except subprocess.TimeoutExpired as exc:
            _stop_process_group(process)
            if cancel_at_start:
                raise ParserCancelledError(
                    "MinerU cancelled by worker shutdown"
                ) from exc
            raise ParserTimeoutError(
                f"MinerU timed out after {options.timeout_seconds}s"
            ) from exc
        except BaseException:
            _stop_process_group(process)
            raise
        finally:
            cancelled = _unregister_process(process)
        if cancelled:
            raise ParserCancelledError("MinerU cancelled by worker shutdown")
        if process.returncode != 0:
            raw_detail = "\n".join(
                part.strip() for part in (stdout, stderr) if part.strip()
            )
            detail = f": {raw_detail}" if raw_detail else ""
            if _TASK_RESULT_TIMEOUT_MARKER in raw_detail:
                raise ParserTaskDeadlineError(
                    f"MinerU task deadline exceeded{detail}"
                )
            if _contains_any(raw_detail, _BACKEND_OVERLOAD_MARKERS):
                raise ParserBackendOverloadedError(
                    f"MinerU backend explicitly rejected capacity{detail}"
                )
            if _contains_any(raw_detail, _LOCAL_API_FAILURE_MARKERS):
                raise ParserLocalInvocationError(
                    f"MinerU local API failed before task admission{detail}"
                )
            # Unknown CLI failures default to the item failure domain. Several
            # legitimate post-admission failures (status polling, result ZIP
            # download/extraction) do not include a JSON "task_id" key; using
            # its presence as an admission oracle would halt unrelated work.
            raise ParserTaskError(f"MinerU task failed{detail}")
        return MinerUProcessResult(
            output_dir=output_dir,
            stdout=stdout,
            stderr=stderr,
        )

    def _env(self, *, options: ParserOptions | None = None) -> dict[str, str]:
        env = dict(os.environ)
        # Local Phase 00 validation showed httpx can fail through proxy env
        # unless socks extras are installed. MinerU uses local model cache here.
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env.pop(key, None)
            env.pop(key.lower(), None)
        env["NO_PROXY"] = "*"
        env.update(self._extra_env)
        if options is not None and options.timeout_seconds is not None:
            env["MINERU_LOCAL_API_STARTUP_TIMEOUT_SECONDS"] = str(
                _LOCAL_API_STARTUP_TIMEOUT_SECONDS
            )
            env["MINERU_TASK_RESULT_TIMEOUT_SECONDS"] = str(
                _task_result_timeout_seconds(options.timeout_seconds)
            )
            env["MINERU_TASK_RESULT_DOWNLOAD_TIMEOUT_SECONDS"] = str(
                _TASK_RESULT_DOWNLOAD_TIMEOUT_SECONDS
            )
        # MinerU's default cross-page table merge rewrites the leading
        # table HTML and empties continuation-page carriers.  Physical-page
        # tables are the canonical evidence boundary here. Cross-page
        # semantic relations, if needed, belong to retrieval and cannot
        # rewrite the source table payload.
        env["MINERU_TABLE_MERGE_ENABLE"] = "0"
        return env
