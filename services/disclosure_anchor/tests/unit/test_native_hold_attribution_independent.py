"""Independent lost-reply test: recover operator attribution from native storage.

Disposable registry only. The caller's successful response and local receipt
are deliberately unavailable; the existing native responsibility must suffice.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from collections.abc import Iterator

from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    decode_task_failure_cause_v2,
)
from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as native
from tests.unit.test_mineru_result_storage import PLENTY, StorageLab


def _objects(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)
    elif isinstance(value, str) and value.startswith("{"):
        try:
            decoded = json.loads(value)
        except ValueError:
            return
        yield from _objects(decoded)


class NativeHoldAttributionIndependentTests(unittest.TestCase):
    def test_restart_without_caller_receipt_recovers_exact_decision_and_mac_can_read_it(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        key, task = "original-attribution-key", "original-attribution-task"
        root = lab.accept(registry, key, task)
        registry.reserve_source_growth(
            key, live_free_bytes=PLENTY, outstanding_promise_bytes=0,
            completion_head_waiting=False,
        )
        registry.transition(key, "processing")
        material = root / "accepted-material.bin"
        material.write_bytes(b"original material stays until ordinary ACK")
        registry.block_storage(key, reason="tree_integrity")
        preview = registry.storage_hold_preview(key, runtime_identity_sha256="sha256:" + "a" * 64)
        canonical = {
            "schema": native.STORAGE_HOLD_DECISION_SCHEMA,
            "preview_sha256": preview["preview_sha256"],
            "decided_by": "independent-operator-unique",
            "reason": "confirmed original tree cannot continue safely",
            "fixed_by": "explicit terminal disposition; no repair claimed",
        }
        registry.decide_storage_hold(
            key, runtime_identity_sha256="sha256:" + "a" * 64,
            expected_preview_sha256=str(preview["preview_sha256"]),
            decided_by=str(canonical["decided_by"]), reason=str(canonical["reason"]),
            fixed_by=str(canonical["fixed_by"]),
        )  # Model a lost response: deliberately do not retain its return.
        del registry, preview
        reopened = lab.open()
        record = reopened.get(key)
        self.assertIsNotNone(record)
        assert record is not None and record.failure_cause is not None
        cause = record.failure_cause
        self.assertEqual((record.state, record.task_id), ("failed", task))
        self.assertTrue(material.is_file())
        self.assertIn(
            canonical,
            [{k: obj[k] for k in canonical} for obj in _objects(cause) if canonical.keys() <= obj.keys()],
            "durable failure has a decision digest but no recoverable canonical operator attribution",
        )
        exact = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(cause["decision_sha256"], "sha256:" + hashlib.sha256(exact).hexdigest())
        cause_bytes = json.dumps(cause, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        decoded = decode_task_failure_cause_v2(
            cause, task_id=task, response_sha256="sha256:" + hashlib.sha256(cause_bytes).hexdigest(),
            response_byte_count=len(cause_bytes),
        )
        self.assertEqual(decoded.code, "storage_hold_terminated")
