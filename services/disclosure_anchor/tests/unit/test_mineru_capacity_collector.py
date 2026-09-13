"""Actual collector probe projection with bounded independent external IO."""

from copy import deepcopy
import hashlib
import unittest

from tests._mineru_admission_health_fixture import canonical
from tests._mineru_capacity_collector_fixture import CapacityCollectorIO
from tests._mineru_capacity_config_fixture import capacity_payload
from tests._mineru_capacity_v11_fixture import idle_health


class CapacityCollectorTests(unittest.TestCase):
    def test_real_probe_preserves_single_serving_sample_and_four_actual_source_hashes(self):
        for config in (capacity_payload(), capacity_payload(parse_active_limit=3,
                        total_nonterminal_limit=7, finalizer_active_limit=2,
                        final_http_limit_per_loop=11, processing_window_size=32)):
            with self.subTest(config=config):
                health = idle_health(config)
                health["capacity_observation"]["framework_limits"]["torch_intraop_threads"]["value"] = 17
                health["capacity_observation"]["observed_at"].update(started_ns=501, completed_ns=509)
                harness = CapacityCollectorIO(health, config)
                result = harness.run()
                self.assertEqual(result["serving_health"], health)
                self.assertEqual(len(result["serving_health"]), 17)
                self.assertNotIn("capacity_runtime", result)
                self.assertEqual(result["capacity_sources_actual_sha256"], {
                    key: "sha256:" + hashlib.sha256(raw).hexdigest()
                    for key, raw in harness.source_bytes.items()})
                self.assertEqual(result["capacity_config_file"], {
                    "path": "/usr/local/etc/mineru/capacity.json",
                    "sha256": "sha256:" + hashlib.sha256(canonical(config)).hexdigest(),
                    "byte_count": len(canonical(config)),
                })
                self.assertEqual(result["task_result_reservation_bytes"], config["result_reservation_bytes"])
                self.assertEqual(result["max_unacked_result_bytes"], config["max_unacked_result_bytes"])
                self.assertEqual(result["max_pending_tasks_effective"], config["total_nonterminal_limit"])
                self.assertEqual(harness.open_calls, [("http://127.0.0.1:8000/health", 10)])
                self.assertEqual(harness.read_sizes, [65537])
                self.assertTrue(harness.response_closed)
                self.assertEqual(harness.getter_calls, 1)  # Configuration accessor only, never framework getter.
                self.assertEqual(harness.config_reads, [(
                    "/usr/local/etc/mineru/capacity.json", harness.expected_sha, 0)])
                self.assertCountEqual(harness.source_closed, harness.source_bytes)
                self.assertEqual(harness.source_read_sizes,
                                 [(key, 1048577) for key in harness.source_open_calls])

    def test_raw_health_bound_duplicate_nonfinite_and_identity_drift_never_emit_success(self):
        config = capacity_payload()
        health = idle_health(config)
        valid = canonical(health)
        mutations = [
            b" " * 65537, b"\xff", b"{", b"[]",
            valid.replace(b'"queued_tasks":0', b'"queued_tasks":0,"queued_tasks":0'),
            valid.replace(b'"completed_ns":120', b'"completed_ns":NaN'),
        ]
        for section, field, value in (
            ("task_protocol_runtime", "capacity_config_sha256", "sha256:" + "0" * 64),
            ("capacity_observation", "capacity_config_sha256", "sha256:" + "0" * 64),
            ("task_protocol_runtime", "schema", "mineru-task-runtime.v2"),
            ("task_protocol_runtime", "task_result_reservation_bytes", 31.0),
            ("task_admission", "ingress_tasks", True),
        ):
            changed = deepcopy(health)
            changed[section][field] = value
            mutations.append(canonical(changed))
        for raw in mutations:
            with self.subTest(raw=raw[:100]):
                self.assertNotEqual(raw, valid)
                harness = CapacityCollectorIO(raw, config)
                with self.assertRaises((ValueError, RuntimeError, TypeError, KeyError)):
                    harness.run()
                self.assertTrue(harness.response_closed)
                self.assertEqual(harness.read_sizes, [65537])
                self.assertEqual(harness.stdout, "")

    def test_config_anchor_and_exact_byte_drift_fail_after_closed_health(self):
        config = capacity_payload()
        for kind in ("caller_sha", "missing", "same_length_bytes", "newline", "io_error"):
            with self.subTest(kind=kind):
                harness = CapacityCollectorIO(idle_health(config), config)
                original = OSError("independent original config read failure")
                if kind == "caller_sha":
                    harness.expected_sha = "sha256:" + "0" * 64
                elif kind == "missing":
                    harness.capacity = None
                elif kind == "same_length_bytes":
                    harness.config_raw = harness.config_raw.replace(b'"parse_active_limit":2', b'"parse_active_limit":1')
                elif kind == "newline":
                    harness.config_raw += b"\n"
                else:
                    harness.config_error = original
                with self.assertRaises((RuntimeError, OSError)) as caught:
                    harness.run()
                if kind == "io_error":
                    self.assertIs(caught.exception, original)
                self.assertTrue(harness.response_closed)
                self.assertEqual(harness.stdout, "")
                self.assertEqual(harness.source_open_calls, [])

    def test_new_source_bound_and_original_io_error_close_acquired_streams(self):
        config = capacity_payload()
        for kind in ("source_limit", "source_error", "health_error"):
            with self.subTest(kind=kind):
                harness = CapacityCollectorIO(idle_health(config), config)
                original = OSError("independent original read failure")
                if kind == "source_limit":
                    harness.source_bytes[next(iter(harness.source_bytes))] = b"x" * 1048577
                elif kind == "source_error":
                    harness.source_error = original
                else:
                    harness.health_error = original
                with self.assertRaises((RuntimeError, OSError)) as caught:
                    harness.run()
                if kind != "source_limit":
                    self.assertIs(caught.exception, original)
                self.assertTrue(harness.response_closed)
                self.assertEqual(harness.stdout, "")
                self.assertEqual(harness.source_closed, harness.source_open_calls)
                if kind == "health_error":
                    self.assertEqual(harness.config_reads, [])
                else:
                    self.assertEqual(len(harness.source_open_calls), 1)
                    self.assertEqual(harness.source_read_sizes[0][1], 1048577)


if __name__ == "__main__":
    unittest.main()
