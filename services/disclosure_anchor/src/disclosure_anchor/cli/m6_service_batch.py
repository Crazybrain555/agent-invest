"""Run a frozen functional service batch, with explicit recovery only.

The request contains the original Mac clock identity and absolute deadline.
This command never generates a fresh deadline on resume or exports M6 scores.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import stat
import sys
from threading import Event
from types import FrameType
from typing import Any

from disclosure_anchor.adapters.runtime.m6_continuous_clock import diagnostic_continuous_clock
from disclosure_anchor.adapters.runtime.m6_service_batch import (
    ServiceBatchFailure, ServiceBatchInput, run_service_batch,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_store import _canonical
from disclosure_anchor.adapters.runtime.mineru_diagnostic_resources import _stream
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.ports.parser import ParserOptions


def read_batch_request(path: Path) -> dict[str, Any]:
    """Read one bounded original request, rejecting replacement and open fields."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with _stream(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or not 0 < before.st_size <= 1024 * 1024):
            raise ValueError("batch request must be an owned bounded regular file")
        raw = stream.read(1024 * 1024 + 1)
        after, named = os.fstat(stream.fileno()), path.stat(follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, key) != getattr(after, key) or getattr(before, key) != getattr(named, key)
               for key in fields):
            raise ValueError("batch request changed while reading")
    value = strict_json_loads(raw.decode("utf-8"))
    required = {"contract_version", "batch_id", "inputs", "api_url", "server_url", "options",
                "journal_root", "clock_identity_sha256", "deadline_ns", "max_in_flight", "credits_limit"}
    if (type(value) is not dict or set(value) != required or _canonical(value) != raw
            or value["contract_version"] != "m6.service-batch-request.v1"
            or type(value["inputs"]) is not list or type(value["options"]) is not dict
            or type(value["credits_limit"]) is not dict):
        raise ValueError("batch request requires exact canonical fields and version")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    request = read_batch_request(args.request)
    clock = diagnostic_continuous_clock()
    if (request["clock_identity_sha256"] != clock.identity_sha256
            or type(request["deadline_ns"]) is not int or clock.now_ns() >= request["deadline_ns"]):
        raise ValueError("batch original Mac clock/deadline no longer permits execution")
    inputs = tuple(ServiceBatchInput(**{**item, "input_pdf": Path(item["input_pdf"])})
                   for item in request["inputs"])
    options = ParserOptions(**request["options"])
    credits = ResourceCreditVector(**request["credits_limit"])
    stopped = Event()

    def stop(_signal: int, _frame: FrameType | None) -> None:
        stopped.set()

    def before_submit() -> None:
        if stopped.is_set() or clock.now_ns() >= request["deadline_ns"]:
            raise RuntimeError("batch POST admission is closed by stop or original deadline")

    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        for number in previous:
            signal.signal(number, stop)
        try:
            result = run_service_batch(
                batch_id=request["batch_id"], inputs=inputs, api_url=request["api_url"],
                server_url=request["server_url"], options=options, journal_root=Path(request["journal_root"]),
                clock_identity_sha256=clock.identity_sha256, deadline_ns=request["deadline_ns"],
                continuous_ns=clock.now_ns, max_in_flight=request["max_in_flight"], credits_limit=credits,
                stop_requested=stopped.is_set, before_submit=before_submit, resume=args.resume,
            )
        except ServiceBatchFailure as exc:
            # Keep the original grouped exception visible after its machine-readable
            # partial result. No suppressed error or successful closure is emitted.
            print(json.dumps({"contract_version": "m6.service-batch-result.v1", "result": asdict(exc.result)},
                             sort_keys=True), file=sys.stderr, flush=True)
            raise
        print(json.dumps({"contract_version": "m6.service-batch-result.v1", "result": asdict(result)},
                         sort_keys=True), flush=True)
        return 0 if not result.not_dispatched and not result.unreconciled else 2
    finally:
        cleanup_errors: list[BaseException] = []
        for number, handler in previous.items():
            try:
                signal.signal(number, handler)
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            primary = sys.exception()
            raise BaseExceptionGroup("batch operation and signal restoration failures", (
                ([primary] if primary is not None else []) + cleanup_errors
            )) from None


if __name__ == "__main__":
    raise SystemExit(main())
