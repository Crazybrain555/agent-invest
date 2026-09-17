"""R20 independent literal projection oracle, shared host/API byte boundary.

No model, Docker, DB, source package, or environmental default supplies expectations.
The release-build/verification cases are added after the public interface freezes.
"""

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.application.contracts import mineru_capacity_config as codec
from tests._mineru_capacity_bootstrap_fixture import CapacityBootstrapFixture
from tests._mineru_capacity_config_fixture import capacity_payload


LITERAL_ENV = {
    "MINERU_API_MAX_CONCURRENT_REQUESTS": "7",
    "MINERU_API_MAX_PENDING_TASKS": "9",
    "MINERU_API_FINALIZER_SLOTS": "2",
    "MINERU_PROCESSING_WINDOW_SIZE": "16",
    "OMP_NUM_THREADS": "4",
    "MKL_NUM_THREADS": "2",
    "OPENBLAS_NUM_THREADS": "1",
    "MINERU_PDF_RENDER_THREADS": "3",
    "MINERU_HYBRID_BATCH_RATIO": "2",
    "MINERU_ENABLE_PIPELINE_INFERENCE_LOCKS": "1",
    "MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES": "268435456",
    "MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES": "2147483648",
}


def _capacity():
    return codec.MineruCapacityConfig(**capacity_payload(
        parse_active_limit=7, total_nonterminal_limit=9, finalizer_active_limit=2,
        final_http_limit_per_loop=18, result_reservation_bytes=268435456,
        max_unacked_result_bytes=2147483648,
    ))


class MineruReleaseProjectionIndependentTests(unittest.TestCase):
    def test_literal_projection_keeps_h_global_and_does_not_invent_p_times_b_rule(self):
        config = _capacity()
        self.assertGreater(9 * 268435456, 2147483648)
        with patch.dict(os.environ, {name: "999" for name in LITERAL_ENV}):
            actual = codec.capacity_environment(config)
            argv = codec.capacity_http_arguments(config)
        self.assertEqual(actual, LITERAL_ENV)
        self.assertEqual(tuple(argv), ("--max-concurrency", "18"))
        actual["MINERU_API_MAX_PENDING_TASKS"] = "corrupted caller copy"
        self.assertEqual(codec.capacity_environment(config), LITERAL_ENV)

    def test_each_field_changes_its_own_consumer_including_previously_missed_h_and_l(self):
        cases = (
            ("parse_active_limit", 8, "MINERU_API_MAX_CONCURRENT_REQUESTS", "8"),
            ("total_nonterminal_limit", 10, "MINERU_API_MAX_PENDING_TASKS", "10"),
            ("finalizer_active_limit", 3, "MINERU_API_FINALIZER_SLOTS", "3"),
            ("processing_window_size", 32, "MINERU_PROCESSING_WINDOW_SIZE", "32"),
            ("omp_num_threads", 2, "OMP_NUM_THREADS", "2"),
            ("mkl_num_threads", 4, "MKL_NUM_THREADS", "4"),
            ("openblas_num_threads", 2, "OPENBLAS_NUM_THREADS", "2"),
            ("pdf_render_processes_requested", 4, "MINERU_PDF_RENDER_THREADS", "4"),
            ("hybrid_batch_ratio_requested", 4, "MINERU_HYBRID_BATCH_RATIO", "4"),
            ("result_reservation_bytes", 134217728,
             "MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES", "134217728"),
            ("max_unacked_result_bytes", 3221225472,
             "MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES", "3221225472"),
        )
        # Codec supports W32; the *current-runtime release builder* must reject it.
        for field, value, consumer, expected in cases:
            with self.subTest(field=field):
                config = replace(_capacity(), **{field: value})
                self.assertEqual(
                    codec.capacity_environment(config), {**LITERAL_ENV, consumer: expected},
                )
                self.assertEqual(tuple(codec.capacity_http_arguments(config)),
                                 ("--max-concurrency", "18"))
        config = replace(_capacity(), final_http_limit_per_loop=22)
        self.assertEqual(codec.capacity_environment(config), LITERAL_ENV)
        self.assertEqual(tuple(codec.capacity_http_arguments(config)),
                         ("--max-concurrency", "22"))

    def test_standalone_byte_copy_exports_same_projection_and_bootstrap_forwards_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CapacityBootstrapFixture(Path(temporary))
            try:
                standalone = fixture.codec.decode_mineru_capacity_config(_capacity().exact_bytes)
                self.assertEqual(fixture.codec.capacity_environment(standalone), LITERAL_ENV)
                self.assertEqual(tuple(fixture.codec.capacity_http_arguments(standalone)),
                                 ("--max-concurrency", "18"))
                self.assertIs(fixture.bootstrap.capacity_environment,
                              fixture.codec.capacity_environment)
                self.assertIsNone(fixture.bootstrap.verify_http_capacity(standalone, 18))
                with self.assertRaises(ValueError):
                    fixture.bootstrap.verify_http_capacity(standalone, 126)
                for original, copied, raw in fixture.originals.values():
                    self.assertEqual(original.read_bytes(), raw)
                    self.assertEqual(copied.read_bytes(), raw)
            finally:
                fixture.close()

    def test_projection_revalidates_exact_contract_instead_of_trusting_forged_object(self):
        config = _capacity()
        object.__setattr__(config, "pipeline_inference_locks", False)
        for function in (codec.capacity_environment, codec.capacity_http_arguments):
            for invalid in (config, {}, None):
                with self.subTest(function=function.__name__, invalid=type(invalid).__name__):
                    with self.assertRaises(ValueError):
                        function(invalid)
