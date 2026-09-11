"""Positive regression for an actual replace followed by an injected error."""

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
    spec = importlib.util.spec_from_file_location("m6_p1_postreplace_regression", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("protocol module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return source, module


def main() -> int:
    source, protocol = load_protocol()
    injection_hits = 0
    with tempfile.TemporaryDirectory(prefix="m6-p1-postreplace-regression-") as directory:
        root = Path(directory)
        registry_path = root / "registry.json"
        registry = protocol.DurableTaskRegistry(
            registry_path,
            max_unacked_result_bytes=100,
            output_root=root,
        )
        registry.reconcile_or_create(
            idempotency_key="first",
            task_id="first-task",
            attempt_identity="first-attempt",
            fence_identity="first-fence",
        )
        original_replace = registry._replace_registry_file

        def replace_then_raise(source_path: Path, destination: Path) -> None:
            nonlocal injection_hits
            injection_hits += 1
            original_replace(source_path, destination)
            raise OSError("injected-after-actual-replace")

        with mock.patch.object(
            registry,
            "_replace_registry_file",
            side_effect=replace_then_raise,
        ):
            record, created = registry.reconcile_or_create(
                idempotency_key="second",
                task_id="second-task",
                attempt_identity="second-attempt",
                fence_identity="second-fence",
            )

        assert injection_hits == 1
        assert created and record.idempotency_key == "second"
        visible = registry.get("second")
        assert visible is not None and visible.task_id == "second-task"
        disk = json.loads(registry_path.read_text(encoding="utf-8"))["records"]
        assert any(row["idempotency_key"] == "second" for row in disk)
        status = registry.persistence_status()
        assert status["state"] == "degraded"
        assert status["last_event"]["outcome"] == "committed_after_recovery"
        assert status["last_event"]["cause_type"] == "OSError"

        restarted = protocol.DurableTaskRegistry(
            registry_path,
            max_unacked_result_bytes=100,
            output_root=root,
        )
        recovered = restarted.get("second")
        assert recovered is not None
        assert recovered.attempt_identity == "second-attempt"
        assert recovered.fence_identity == "second-fence"

        output = {
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "injection": "actual os.replace followed by OSError",
            "injection_hits": injection_hits,
            "memory_has_second": visible is not None,
            "disk_has_second": True,
            "recovery_outcome": status["last_event"]["outcome"],
            "scope": "synthetic system-temp registry; no PDF/GPU/PG/runtime",
        }
        print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
