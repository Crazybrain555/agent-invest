"""Bounded physical source observation before a diagnostic submission.

Reuse the existing read-only PDF observer child without inventing a production
admission candidate. The child has no subprocess descendants; its exact process
and both output streams are reaped before returning or propagating a failure.
"""

from __future__ import annotations

import os
import selectors
import subprocess
import sys

from disclosure_anchor.adapters.runtime.mineru_diagnostic_resources import DiagnosticResources

_MAX_OUTPUT_BYTES = 8192
_REAP_TIMEOUT_SECONDS = 1.0


class DiagnosticSourceChildUnresolved(RuntimeError):
    """Keep exact child ownership available; its source intent stays unclosed."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        super().__init__(f"diagnostic source child {process.pid} has no verified exit; retain source probe intent")
        self.process = process


def observe_diagnostic_source(resources: DiagnosticResources, *, source_pdf_sha256: str,
                              source_byte_count: int) -> bytes:
    resources.checkpoint()
    command = [sys.executable, "-B", "-m", "disclosure_anchor.adapters.parsers.pdf_source_observation_process",
               "--root", str(resources.path), "--relpath", "source.pdf", "--byte-limit", str(source_byte_count),
               "--expected-sha256", source_pdf_sha256, "--expected-byte-count", str(source_byte_count)]
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "PYTHONPATH", "PYTHONHOME", "LANG", "LC_ALL"}}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=environment, start_new_session=True)
    assert process.stdout is not None and process.stderr is not None
    stdout, stderr = bytearray(), bytearray()
    errors: list[BaseException] = []
    try:
        with selectors.DefaultSelector() as selector:
            for stream, destination in ((process.stdout, stdout), (process.stderr, stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, destination)
            while selector.get_map() or process.poll() is None:
                timeout = min(0.05, resources.checkpoint())
                for key, _ in selector.select(timeout):
                    chunk = os.read(key.fd, _MAX_OUTPUT_BYTES + 1 - len(stdout) - len(stderr))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.extend(chunk)
                    if len(stdout) + len(stderr) > _MAX_OUTPUT_BYTES:
                        raise RuntimeError("diagnostic source observer output exceeds bounded receipt")
                resources.checkpoint()
        if process.returncode != 0:
            raise RuntimeError(f"diagnostic source observer failed ({process.returncode}): "
                               + bytes(stderr).decode("utf-8", errors="replace"))
        resources.checkpoint()
    except BaseException as primary:
        errors.append(primary)
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                # The exact child exited between poll and kill; wait still
                # establishes its termination before resource release.
                pass
            except BaseException as cleanup:
                errors.append(cleanup)
        try:
            # A failed kill, or an uninterruptible child, must not turn the
            # cleanup path into an unbounded wait. This is a reap allowance,
            # never a fresh budget for observation or another submission.
            process.wait(timeout=_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as cleanup:
            errors.extend((cleanup, DiagnosticSourceChildUnresolved(process)))
        except BaseException as cleanup:
            errors.append(cleanup)
            if process.returncode is None:
                errors.append(DiagnosticSourceChildUnresolved(process))
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except BaseException as cleanup:
                errors.append(cleanup)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("diagnostic source observation and child closure failed", errors) from None
    return bytes(stdout)
