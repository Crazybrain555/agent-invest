"""One-shot same-root registry helper for the zero-duty maintenance owner.

The Windows owner runs it once per action, by a deterministic container name,
from the exact API image already pinned by the install receipt:
``/usr/bin/python3.12 -I <helper dir>/mineru_same_root_registry_helper.py <request>``
with no network, no GPU and a read-only container file system. It imports no
MinerU module and starts no API: it loads only the packaged
``agent_task_protocol_v2.py`` copied beside it, performs one closed action and
prints exactly one JSON line. ``observe`` runs on a read-only bind of the
output root; ``apply`` is the only writer.

Exit codes: 0 done; 64 request invalid; 65 refused (this call wrote nothing);
70 unknown (a write may have happened; ``visible`` classifies the registry).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import signal
import sys
from types import FrameType, ModuleType
from typing import Any

REQUEST_SCHEMA = "mineru-same-root-helper-request.v1"
FAILURE_SCHEMA = "mineru-same-root-helper-failure.v1"
_MAX_REQUEST_BYTES = 64 * 1024
_REQUEST_FIELDS = frozenset({
    "schema", "action", "operation_id", "root", "approved_root", "expected_r0_sha256", "expected_r0_bytes",
    "expected_body_sha256", "current_root", "expected_r1_sha256", "expected_r1_bytes", "deadline_seconds",
})
_APPLY_ONLY = ("current_root", "expected_r1_sha256", "expected_r1_bytes")
EX_OK, EX_USAGE, EX_REFUSED, EX_UNKNOWN = 0, 64, 65, 70


class _Deadline(Exception):
    """The helper's own bounded lifetime expired."""


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")), flush=True)


def _failure(request: dict[str, Any] | None, *, kind: str, phase: str, visible: str | None, error: str) -> dict[str, Any]:
    return {
        "schema": FAILURE_SCHEMA,
        "action": None if request is None else request.get("action"),
        "operation_id": None if request is None else request.get("operation_id"),
        "kind": kind, "phase": phase, "visible": visible, "error": error[:2048],
    }


def read_request(path: Path) -> dict[str, Any]:
    """One closed request; values are only typed here and fully validated by the protocol."""

    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_REQUEST_BYTES:
        raise ValueError("request bytes are outside the closed envelope")

    def closed(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("request contains a duplicate field")
            value[key] = item
        return value

    def constant(value: str) -> Any:
        raise ValueError(f"request contains a non-finite value: {value}")

    request = json.loads(raw.decode("utf-8"), object_pairs_hook=closed, parse_constant=constant)
    if type(request) is not dict or set(request) != _REQUEST_FIELDS or request["schema"] != REQUEST_SCHEMA:
        raise ValueError("request fields are not closed")
    action = request["action"]
    if action not in ("observe", "apply"):
        raise ValueError("request action is unsupported")
    if type(request["root"]) is not str or not request["root"].startswith("/"):
        raise ValueError("request root must be an absolute container path")
    deadline = request["deadline_seconds"]
    if type(deadline) is not int or not 1 <= deadline <= 600:
        raise ValueError("request deadline must be 1..600 seconds")
    if action == "observe":
        if any(request[name] is not None for name in _APPLY_ONLY):
            raise ValueError("observe requests carry no apply fields")
    elif any(request[name] is None for name in (*_APPLY_ONLY, "expected_body_sha256")):
        raise ValueError("apply requests require every approved field")
    return request


def load_protocol(directory: Path) -> ModuleType:
    """Load the packaged protocol beside this file, without writing bytecode."""

    sys.dont_write_bytecode = True
    path = directory / "agent_task_protocol_v2.py"
    spec = importlib.util.spec_from_file_location("mineru_same_root_protocol", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"protocol module cannot be loaded: {path}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their module through sys.modules while the module runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run(request: dict[str, Any], protocol: ModuleType) -> dict[str, Any]:
    root = Path(request["root"])
    common = {
        "approved_root": request["approved_root"],
        "expected_r0_sha256": request["expected_r0_sha256"],
        "expected_r0_bytes": request["expected_r0_bytes"],
        "expected_body_sha256": request["expected_body_sha256"],
    }
    if request["action"] == "observe":
        return protocol.observe_same_root_registry(root, **common)
    return protocol.apply_same_root_registry(
        root, operation_id=request["operation_id"], current_root=request["current_root"],
        expected_r1_sha256=request["expected_r1_sha256"], expected_r1_bytes=request["expected_r1_bytes"],
        **common,
    )


def main(argv: list[str] | None = None, *, protocol: ModuleType | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    request: dict[str, Any] | None = None
    if len(arguments) != 1:
        _emit(_failure(None, kind="invalid", phase="request", visible=None, error="exactly one request path is required"))
        return EX_USAGE
    try:
        request = read_request(Path(arguments[0]))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _emit(_failure(None, kind="invalid", phase="request", visible=None, error=f"{type(exc).__name__}: {exc}"))
        return EX_USAGE
    try:
        if protocol is None:
            protocol = load_protocol(Path(__file__).resolve().parent)
    except BaseException as exc:  # noqa: BLE001 - nothing ran; the owner sees the exact load failure
        _emit(_failure(request, kind="refused", phase="load", visible=None, error=f"{type(exc).__name__}: {exc}"))
        return EX_REFUSED
    if not protocol.same_root_operation_id_valid(request["operation_id"]):
        _emit(_failure(None, kind="invalid", phase="request", visible=None, error="request operation id is invalid"))
        return EX_USAGE

    def expire(_signum: int, _frame: FrameType | None) -> None:
        raise _Deadline(f"helper deadline of {request['deadline_seconds']} seconds expired")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(request["deadline_seconds"])
    try:
        result = run(request, protocol)
    except protocol.SameRootOutcomeUnknown as exc:
        _emit(_failure(request, kind="unknown", phase=exc.phase, visible=exc.visible, error=str(exc)))
        return EX_UNKNOWN
    except protocol.SameRootRefused as exc:
        _emit(_failure(request, kind="refused", phase="precondition", visible=None, error=str(exc)))
        return EX_REFUSED
    except BaseException as exc:  # noqa: BLE001 - any other end of a writer is reported, never success
        kind = "refused" if request["action"] == "observe" and not isinstance(exc, _Deadline) else "unknown"
        _emit(_failure(request, kind=kind, phase="execution", visible=None, error=f"{type(exc).__name__}: {exc}"))
        return EX_REFUSED if kind == "refused" else EX_UNKNOWN
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    _emit(result)
    return EX_OK


if __name__ == "__main__":
    sys.exit(main())
