"""Positive regression for the attached pre-write divergence reproduction."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock


def load_protocol():
    source = Path(os.environ["M6_PROTOCOL_MODULE"]).resolve()
    spec = importlib.util.spec_from_file_location("m6_p1_prewrite_regression", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("protocol module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return source, module


def main() -> int:
    source, protocol = load_protocol()
    injection_hits = 0
    with tempfile.TemporaryDirectory(prefix="m6-p1-prewrite-regression-") as directory:
        root = Path(directory)
        registry_path = root / "registry.json"
        registry = protocol.DurableTaskRegistry(
            registry_path,
            max_unacked_result_bytes=100,
            output_root=root,
        )
        registry.reconcile_or_create(
            idempotency_key="key",
            task_id="task-key",
            attempt_identity="attempt",
            fence_identity="fence",
        )
        registry.transition("key", "processing")
        registry.transition("key", "finalizing")
        registry.reserve_finalizer("key", byte_budget=100)
        before_bytes = registry_path.read_bytes()
        result_bytes = b"valid-regression-result"
        result = root / "result.zip"
        result.write_bytes(result_bytes)
        digest = hashlib.sha256(result_bytes).hexdigest()
        owner = hashlib.sha256(
            f"task-key\0{digest}\0{len(result_bytes)}".encode()
        ).hexdigest()

        def fail_before_write() -> None:
            nonlocal injection_hits
            injection_hits += 1
            raise OSError("injected-before-registry-write")

        with mock.patch.object(registry, "_persist", side_effect=fail_before_write):
            try:
                registry.complete(
                    "key",
                    result_path=result,
                    result_sha256=digest,
                    result_bytes=len(result_bytes),
                    result_owner=owner,
                )
            except OSError as exc:
                observed_error = str(exc)
            else:
                raise AssertionError("pre-write injection was not propagated")

        memory = registry.get("key")
        disk = json.loads(registry_path.read_text(encoding="utf-8"))["records"][0]
        assert injection_hits == 1
        assert registry_path.read_bytes() == before_bytes
        assert memory is not None and memory.state == "finalizing"
        assert memory.reserved_result_bytes == 100
        assert disk["state"] == "finalizing"
        assert disk["reserved_result_bytes"] == 100

        registry.complete(
            "key",
            result_path=result,
            result_sha256=digest,
            result_bytes=len(result_bytes),
            result_owner=owner,
        )
        restarted = protocol.DurableTaskRegistry(
            registry_path,
            max_unacked_result_bytes=100,
            output_root=root,
        )
        recovered = restarted.get("key")
        assert recovered is not None and recovered.state == "completed"
        assert recovered.result_sha256 == digest
        assert recovered.result_owner == owner
        assert restarted.persistence_status()["state"] == "healthy"

        output = {
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "injection": "before _persist writes any bytes",
            "injection_hits": injection_hits,
            "observed_error": observed_error,
            "rolled_back_state": memory.state,
            "rolled_back_reserved_bytes": memory.reserved_result_bytes,
            "retry_state": recovered.state,
            "retry_result_sha256": recovered.result_sha256,
            "scope": "synthetic system-temp registry; no PDF/GPU/PG/runtime",
        }
        print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
