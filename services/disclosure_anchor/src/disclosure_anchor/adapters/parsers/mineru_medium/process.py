"""MinerU CLI process wrapper."""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorError,
    mark_mineru_orchestrator_incident,
    wait_for_mineru_orchestrator_idle,
)
from disclosure_anchor.domain.errors import (
    ParserBackendUnavailableError,
    ParserBackendOverloadedError,
    ParserCancelledError,
    ParserInvocationError,
    ParserLocalInvocationError,
    ParserTaskDeadlineError,
    ParserTaskError,
    ParserTimeoutError,
    ParserVersionProbeError,
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
_REMOTE_TASK_FAILED_MARKER = "task(s) failed while processing documents"
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
_BACKEND_UNAVAILABLE_STATUS = re.compile(r"Unexpected status code: \[(?:5[0-9]{2})\]")
_LOCAL_API_STARTUP_TIMEOUT_SECONDS = 120
_TASK_RESULT_DOWNLOAD_TIMEOUT_SECONDS = 120
_MINERU_INHERITED_ENV_KEYS = frozenset(
    {
        "CURL_CA_BUNDLE",
        "DYLD_FALLBACK_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "HOME",
        "HF_HOME",
        "LANG",
        "LD_LIBRARY_PATH",
        "MINERU_MODEL_CACHE",
        "MINERU_PROCESSING_WINDOW_SIZE",
        "MODELSCOPE_CACHE",
        "NO_COLOR",
        "PATH",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
    }
)
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


def _process_was_cancelled(process: subprocess.Popen[str]) -> bool:
    with _ACTIVE_PROCESSES_LOCK:
        return process in _CANCELLED_PROCESSES


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
            process for process in _ACTIVE_PROCESSES if process.poll() is None
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
            command.extend(["--image-analysis", str(options.image_analysis).lower()])
        if options.backend.startswith("hybrid-"):
            command.extend(["--effort", options.effort])
        if options.start_page is not None:
            command.extend(["-s", str(options.start_page)])
        if options.end_page is not None:
            command.extend(["-e", str(options.end_page)])
        if options.api_url:
            # MinerU 3.4.4 submits to an existing orchestration API when this
            # exact option is present; it does not start a per-document local
            # FastAPI process.
            command.extend(["--api-url", options.api_url])
        if options.server_url:
            # With a fixed API this value is forwarded as the VLM upstream
            # resolved by the Windows API host, not a Mac-reachable address.
            command.extend(["-u", options.server_url])
        if (
            options.backend.endswith("-http-client")
            and options.api_url is None
            and options.http_request_concurrency is not None
        ):
            if options.http_request_concurrency < 1:
                raise ValueError(
                    "http_request_concurrency must be positive when configured"
                )
            # MinerU forwards this unknown option only to a temporary local
            # mineru-api. With --api-url it is ignored rather than forwarded;
            # fixed API concurrency is configured on that service itself.
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

    def probe_server(self, server_url: str, *, timeout_seconds: float = 15.0) -> None:
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
        parsed = urllib.parse.urlsplit(server_url)
        # OpenAI-compatible upstreams conventionally use a /v1 API base while
        # their readiness endpoint remains at the server root. The fixed
        # mineru-api endpoint is already root-based.
        probe_path = parsed.path.rstrip("/")
        if probe_path == "/v1":
            probe_path = ""
        url = urllib.parse.urlunsplit(parsed._replace(path=f"{probe_path}/health"))
        request = urllib.request.Request(url, method="GET")
        # The backend lives on the LAN/tailnet: never route the probe
        # through proxy env vars (a proxy that cannot reach the private
        # address would report a healthy server as an outage — the same
        # reason _env() strips proxies for the mineru subprocess).
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
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
                self.command_for(
                    input_pdf=input_pdf, output_dir=output_dir, options=options
                ),
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
            self._drain_external_api(options)
            if cancel_at_start or _process_was_cancelled(process):
                raise ParserCancelledError(
                    "MinerU cancelled by worker shutdown"
                ) from exc
            raise ParserTimeoutError(
                f"MinerU timed out after {options.timeout_seconds}s"
            ) from exc
        except BaseException:
            _stop_process_group(process)
            self._drain_external_api(options)
            raise
        finally:
            cancelled = _unregister_process(process)
        if cancelled:
            self._drain_external_api(options)
            raise ParserCancelledError("MinerU cancelled by worker shutdown")
        if process.returncode != 0:
            self._drain_external_api(options)
            raw_detail = "\n".join(
                part.strip() for part in (stdout, stderr) if part.strip()
            )
            detail = f": {raw_detail}" if raw_detail else ""
            if _TASK_RESULT_TIMEOUT_MARKER in raw_detail:
                raise ParserTaskDeadlineError(f"MinerU task deadline exceeded{detail}")
            if _contains_any(raw_detail, _BACKEND_OVERLOAD_MARKERS):
                raise ParserBackendOverloadedError(
                    f"MinerU backend explicitly rejected capacity{detail}"
                )
            if _BACKEND_UNAVAILABLE_STATUS.search(raw_detail) is not None:
                # The remote OpenAI-compatible VLM service owns a 5xx. It is
                # shared infrastructure, not evidence that this PDF is bad.
                # Let the worker's parser-backend circuit pause admissions.
                raise ParserBackendUnavailableError(
                    f"MinerU backend failed an inference request{detail}"
                )
            if _contains_any(raw_detail, _LOCAL_API_FAILURE_MARKERS):
                raise ParserLocalInvocationError(
                    f"MinerU local API failed before task admission{detail}"
                )
            if options.api_url is not None:
                # The fixed API's aggregate task-failed output does not carry
                # a proven item-local taxonomy. It can wrap GPU worker death,
                # inference transport failure, or orchestration corruption.
                # Treat every such failure as shared infrastructure until an
                # exact upstream error contract supports a safe allow-list.
                raise ParserBackendUnavailableError(
                    "MinerU fixed API task/submit/status/result path failed"
                    f"{detail}"
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

    def _drain_external_api(self, options: ParserOptions) -> None:
        """Fence retries after local stop; MinerU exposes no cancel endpoint."""

        if options.api_url is None:
            return
        # Publish the incident before blocking. Existing worker/admin/pipeline
        # admission checkers become permanently invalid for their lifetime, so
        # another completed Future cannot refill the queue while this client
        # waits for natural drain.
        mark_mineru_orchestrator_incident()
        try:
            wait_for_mineru_orchestrator_idle(
                options.api_url,
                timeout_seconds=float(options.api_drain_timeout_seconds),
            )
        except (MinerUOrchestratorError, ValueError) as exc:
            raise ParserBackendUnavailableError(
                "MinerU API remote task drain could not be proved"
            ) from exc

    def _env(self, *, options: ParserOptions | None = None) -> dict[str, str]:
        # MinerU and its temporary fast_api are parser mechanisms, not service
        # principals.  Never pass the parent worker's DB, CNINFO, admin-token,
        # semantic-provider, or unrelated credential environment to them.
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _MINERU_INHERITED_ENV_KEYS or key.startswith("LC_")
        }
        # Local validation showed httpx can fail through proxy env unless
        # socks extras are installed. MinerU uses local model cache here.
        env["NO_PROXY"] = "*"
        env.update(self._extra_env)
        # The writer uses MinerU 3.4.4's official merge-on default. Never let
        # a stale shell/launchd/operator override silently change table shape.
        env.pop("MINERU_TABLE_MERGE_ENABLE", None)
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
        return env
