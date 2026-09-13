"""Independent pure A1 codec boundaries; no service, environment policy or IO owner."""

import dataclasses
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from disclosure_anchor.application.contracts import mineru_capacity_config as codec
from tests._mineru_capacity_config_fixture import (
    CAPACITY_BYTES,
    canonical_payload,
    capacity_payload,
)


class MineruCapacityConfigTests(unittest.TestCase):
    def invalid(self, value):
        with self.assertRaises(ValueError):
            codec.MineruCapacityConfig(**value)
        with self.assertRaises(ValueError):
            codec.decode_mineru_capacity_config(canonical_payload(value))

    def test_literal_canonical_bytes_and_sha_do_not_impose_p_times_b_or_equal_cpu(self):
        self.assertEqual(codec.CAPACITY_CONFIG_CONTRACT, "mineru.capacity-config.v1")
        value = codec.MineruCapacityConfig(**capacity_payload())
        self.assertGreater(
            value.total_nonterminal_limit * value.result_reservation_bytes,
            value.max_unacked_result_bytes,
        )
        self.assertNotEqual(value.omp_num_threads, value.mkl_num_threads)
        self.assertEqual(codec.encode_mineru_capacity_config(value), CAPACITY_BYTES)
        self.assertEqual(value.exact_bytes, CAPACITY_BYTES)
        self.assertEqual(
            value.sha256, "sha256:" + hashlib.sha256(CAPACITY_BYTES).hexdigest()
        )
        decoded = codec.decode_mineru_capacity_config(CAPACITY_BYTES)
        self.assertIs(type(decoded), codec.MineruCapacityConfig)
        self.assertEqual(dataclasses.asdict(decoded), capacity_payload())
        self.assertEqual(decoded, value)

    def test_all_required_fields_are_closed_and_no_dataclass_defaults_or_observed_fields_exist(
        self,
    ):
        expected = set(capacity_payload())
        self.assertEqual(
            {field.name for field in dataclasses.fields(codec.MineruCapacityConfig)},
            expected,
        )
        parameters = inspect.signature(codec.MineruCapacityConfig).parameters
        self.assertEqual(set(parameters), expected)
        self.assertTrue(
            all(item.default is inspect.Parameter.empty for item in parameters.values())
        )
        for name in sorted(expected):
            value = capacity_payload()
            del value[name]
            with self.subTest(missing=name):
                with self.assertRaises(TypeError):
                    codec.MineruCapacityConfig(**value)
                with self.assertRaises(ValueError):
                    codec.decode_mineru_capacity_config(canonical_payload(value))
        for name in ("observed", "sha256", "exact_bytes", "extra"):
            value = capacity_payload(**{name: 1})
            with self.subTest(extra=name):
                with self.assertRaises(TypeError):
                    codec.MineruCapacityConfig(**value)
                with self.assertRaises(ValueError):
                    codec.decode_mineru_capacity_config(canonical_payload(value))

    def test_allowed_parameter_endpoints_keep_one_schema_and_distinct_identity(self):
        variants = [
            {
                "parse_active_limit": 1,
                "total_nonterminal_limit": 1,
                "finalizer_active_limit": 1,
            },
            {
                "parse_active_limit": 128,
                "total_nonterminal_limit": 128,
                "finalizer_active_limit": 128,
            },
            {"final_http_limit_per_loop": 1},
            {"final_http_limit_per_loop": 128},
            {"processing_window_size": 1},
            {"processing_window_size": 1024},
            {
                "omp_num_threads": 256,
                "mkl_num_threads": 1,
                "openblas_num_threads": 256,
                "pdf_render_processes_requested": 256,
            },
            {
                "omp_num_threads": 1,
                "mkl_num_threads": 256,
                "openblas_num_threads": 1,
                "pdf_render_processes_requested": 1,
            },
            {"result_reservation_bytes": 1, "max_unacked_result_bytes": 1},
            {
                "result_reservation_bytes": 2**63 - 1,
                "max_unacked_result_bytes": 2**63 - 1,
            },
            *({"hybrid_batch_ratio_requested": ratio} for ratio in (1, 4, 8)),
        ]
        identities = set()
        for changes in variants:
            payload = capacity_payload(**changes)
            expected = canonical_payload(payload)
            with self.subTest(changes=changes):
                config = codec.MineruCapacityConfig(**payload)
                self.assertEqual(config.contract_version, "mineru.capacity-config.v1")
                self.assertEqual(codec.encode_mineru_capacity_config(config), expected)
                self.assertEqual(codec.decode_mineru_capacity_config(expected), config)
                self.assertEqual(
                    config.sha256, "sha256:" + hashlib.sha256(expected).hexdigest()
                )
                identities.add(config.sha256)
        self.assertEqual(len(identities), len(variants))

    def test_exact_integer_types_reject_bool_float_string_null_and_container(self):
        numeric = set(capacity_payload()) - {
            "contract_version",
            "pipeline_inference_locks",
        }
        for field in sorted(numeric):
            for wrong in (True, False, 1.0, "1", None, [], {}):
                with self.subTest(field=field, wrong=wrong):
                    self.invalid(capacity_payload(**{field: wrong}))
        for wrong in (False, 1, 0, "true", None):
            with self.subTest(locks=wrong):
                self.invalid(capacity_payload(pipeline_inference_locks=wrong))
        for wrong in (
            1,
            True,
            None,
            "mineru.capacity-config.v2",
            "mineru.capacity-config.v1\n",
        ):
            with self.subTest(version=wrong):
                self.invalid(capacity_payload(contract_version=wrong))

    def test_numeric_bounds_and_only_required_cross_field_relations_reject(self):
        maxima = {
            "parse_active_limit": 128,
            "total_nonterminal_limit": 128,
            "finalizer_active_limit": 128,
            "final_http_limit_per_loop": 128,
            "api_process_limit": 1,
            "api_event_loop_limit": 1,
            "processing_window_size": 1024,
            "omp_num_threads": 256,
            "mkl_num_threads": 256,
            "openblas_num_threads": 256,
            "pdf_render_processes_requested": 256,
            "hybrid_batch_ratio_requested": 8,
            "result_reservation_bytes": 2**63 - 1,
            "max_unacked_result_bytes": 2**63 - 1,
        }
        for field, maximum in maxima.items():
            for wrong in (-1, 0, maximum + 1):
                with self.subTest(field=field, wrong=wrong):
                    self.invalid(capacity_payload(**{field: wrong}))
        for changes in (
            {"parse_active_limit": 4},
            {"finalizer_active_limit": 4},
            {"result_reservation_bytes": 74},
            {"hybrid_batch_ratio_requested": 3},
        ):
            with self.subTest(changes=changes):
                self.invalid(capacity_payload(**changes))

    def test_decoder_rejects_noncanonical_duplicate_nonfinite_invalid_utf8_and_oversize(
        self,
    ):
        reversed_fields = dict(reversed(list(capacity_payload().items())))
        wrong = [
            b"",
            b"null",
            b"[]",
            b"1",
            b"true",
            CAPACITY_BYTES + b"\n",
            b" " + CAPACITY_BYTES,
            CAPACITY_BYTES + CAPACITY_BYTES,
            json.dumps(capacity_payload(), indent=2).encode(),
            json.dumps(reversed_fields, separators=(",", ":")).encode(),
            CAPACITY_BYTES.replace(b"mineru.capacity", b"\\u006dineru.capacity"),
            b"\xef\xbb\xbf" + CAPACITY_BYTES,
            b"\xff" + CAPACITY_BYTES,
            CAPACITY_BYTES.replace(
                b'"parse_active_limit":2',
                b'"parse_active_limit":2,"parse_active_limit":2',
            ),
            CAPACITY_BYTES.replace(
                b'"parse_active_limit":2', b'"parse_active_limit":NaN'
            ),
            CAPACITY_BYTES.replace(
                b'"parse_active_limit":2', b'"parse_active_limit":Infinity'
            ),
            CAPACITY_BYTES.replace(
                b'"parse_active_limit":2', b'"parse_active_limit":-Infinity'
            ),
            CAPACITY_BYTES + b" " * (65536 - len(CAPACITY_BYTES)),
            CAPACITY_BYTES + b" " * (65537 - len(CAPACITY_BYTES)),
        ]
        # No valid configuration reaches64KiB; exact64KiB padding is noncanonical,
        # while65537 additionally exceeds the raw bound. Neither is an acceptance oracle.
        for index, raw in enumerate(wrong):
            with self.subTest(case=index, size=len(raw)):
                with self.assertRaises(ValueError):
                    codec.decode_mineru_capacity_config(raw)

    def test_slots_frozen_exact_encoder_family_and_forged_invalid_state_reject(self):
        value = codec.MineruCapacityConfig(**capacity_payload())
        self.assertFalse(hasattr(value, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            value.parse_active_limit = 3

        class Derived(codec.MineruCapacityConfig):
            pass

        for wrong in (
            capacity_payload(),
            object(),
            None,
            Derived(**capacity_payload()),
        ):
            with self.subTest(type=type(wrong).__name__):
                with self.assertRaises((TypeError, ValueError)):
                    codec.encode_mineru_capacity_config(wrong)
        forged = codec.MineruCapacityConfig(**capacity_payload())
        object.__setattr__(forged, "parse_active_limit", True)
        with self.assertRaises(ValueError):
            codec.encode_mineru_capacity_config(forged)

    def test_identical_source_bytes_import_standalone_with_stdlib_only(self):
        raw = Path(codec.__file__).read_bytes()
        program = r"""
import importlib.abc, importlib.util, sys
from pathlib import Path
class RejectApplication(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'disclosure_anchor', 'scripts', 'tests'}:
            raise AssertionError('standalone codec imported application: ' + fullname)
sys.meta_path.insert(0, RejectApplication())
path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('standalone_mineru_capacity', path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
expected = bytes.fromhex(sys.argv[2])
value = module.decode_mineru_capacity_config(expected)
assert module.encode_mineru_capacity_config(value) == expected
assert value.exact_bytes == expected
assert value.contract_version == 'mineru.capacity-config.v1'
assert value.omp_num_threads == 4 and value.mkl_num_threads == 2
assert not any(name.startswith('disclosure_anchor') for name in sys.modules)
for name in sys.modules:
    top = name.split('.')[0]
    assert top in sys.stdlib_module_names or top in {'__main__', 'standalone_mineru_capacity'}, name
print(value.sha256)
"""
        with tempfile.TemporaryDirectory(
            prefix="mineru-capacity-standalone-"
        ) as directory:
            copy = Path(directory) / "mineru_capacity_config.py"
            copy.write_bytes(raw)
            self.assertEqual(copy.read_bytes(), raw)
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    program,
                    str(copy),
                    CAPACITY_BYTES.hex(),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env={},
                timeout=5,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(
            result.stdout.decode().strip(),
            "sha256:" + hashlib.sha256(CAPACITY_BYTES).hexdigest(),
        )
