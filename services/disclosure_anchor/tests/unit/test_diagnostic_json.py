"""Independent byte/depth/canonical-value boundaries for pure diagnostic JSON."""

from collections import UserDict
from decimal import Decimal
import unittest

from disclosure_anchor.application.contracts.diagnostic_json import bounded_json_bytes, bounded_json_value


class DiagnosticJsonTests(unittest.TestCase):
    def test_literal_canonical_utf8_escaping_and_supported_scalar_values(self):
        value = {"z": (True, False, None, 7, -2, 0.5), "a": "金额€\n\"\\\t\u0001"}
        expected = '{"a":"金额€\\n\\\"\\\\\\t\\u0001","z":[true,false,null,7,-2,0.5]}'.encode()
        self.assertEqual(bounded_json_bytes(value, maximum_bytes=len(expected)), expected)
        self.assertEqual(bounded_json_value(expected, maximum_bytes=len(expected)),
                         {"a": value["a"], "z": list(value["z"])})
        for raw, decoded in ((b"null", None), (b"true", True), (b"0", 0), (b'""', ""), (b"[]", []), (b"{}", {})):
            with self.subTest(raw=raw):
                self.assertEqual(bounded_json_bytes(decoded, maximum_bytes=len(raw)), raw)
                self.assertEqual(bounded_json_value(raw, maximum_bytes=len(raw)), decoded)

    def test_byte_ceiling_uses_utf8_and_escaped_bytes_at_exact_boundary(self):
        for value, expected in (("中", '"中"'.encode()), ("\n", b'"\\n"'), ("\\", b'"\\\\"')):
            with self.subTest(value=value):
                self.assertEqual(bounded_json_bytes(value, maximum_bytes=len(expected)), expected)
                self.assertEqual(bounded_json_value(expected, maximum_bytes=len(expected)), value)
                with self.assertRaises((ValueError, TypeError)):
                    bounded_json_bytes(value, maximum_bytes=len(expected) - 1)
                with self.assertRaises((ValueError, TypeError)):
                    bounded_json_value(expected, maximum_bytes=len(expected) - 1)

    def test_caps_are_positive_exact_integers_on_both_directions(self):
        for cap in (True, False, 0, -1, 4.0, "4", None):
            for call in (lambda: bounded_json_bytes(None, maximum_bytes=cap),
                         lambda: bounded_json_value(b"null", maximum_bytes=cap)):
                with self.subTest(cap=cap), self.assertRaises((ValueError, TypeError)):
                    call()

    def test_depth_64_passes_65_fails_for_encoder_and_decoder(self):
        nested = 0
        for _ in range(64):
            nested = [nested]
        expected = b"[" * 64 + b"0" + b"]" * 64
        self.assertEqual(bounded_json_bytes(nested, maximum_bytes=1024), expected)
        self.assertEqual(bounded_json_value(expected, maximum_bytes=1024), nested)
        with self.assertRaises((ValueError, TypeError)):
            bounded_json_bytes([nested], maximum_bytes=1024)
        with self.assertRaises((ValueError, TypeError)):
            bounded_json_value(b"[" + expected + b"]", maximum_bytes=1024)
        # Quoted and escaped bracket characters do not consume container depth.
        quoted = '"' + '[' * 100 + '\\"' + ']' * 100 + '"'
        self.assertEqual(bounded_json_value(quoted.encode(), maximum_bytes=1024),
                         '[' * 100 + '"' + ']' * 100)

    def test_decoder_rejects_duplicate_noncanonical_nonfinite_and_invalid_utf8(self):
        for raw in (b'{"x":1,"x":2}', b'{"outer":{"x":1,"x":2}}', b'{"x": 1}',
                    b'{"z":0,"a":1}', b"null\n", b"NaN", b"Infinity", b"-Infinity",
                    b'"\\u4e2d"', b'"\xff"', b"\xef\xbb\xbfnull", b"1e0", b"01", b""):
            with self.subTest(raw=raw), self.assertRaises((ValueError, TypeError)):
                bounded_json_value(raw, maximum_bytes=1024)

    def test_encoder_rejects_non_json_values_keys_and_nonfinite_floats(self):
        for value in (float("nan"), float("inf"), float("-inf"), {1: "integer key"},
                      {True: "boolean key"}, {None: "null key"}, {"x": b"bytes"},
                      {"x": {1, 2}}, Decimal("1"), object(), UserDict({"x": 1})):
            with self.subTest(type=type(value).__name__), self.assertRaises((ValueError, TypeError)):
                bounded_json_bytes(value, maximum_bytes=1024)

    def test_cycles_and_oversized_text_are_rejected_without_truncated_success(self):
        cyclic = []
        cyclic.append(cyclic)
        for value in (cyclic, "中" * 400_000, "\n" * 1_000_000):
            with self.subTest(type=type(value).__name__), self.assertRaises((ValueError, TypeError)):
                bounded_json_bytes(value, maximum_bytes=64)
        with self.assertRaises((ValueError, TypeError)):
            bounded_json_value(b'"' + b"x" * 1_000_000 + b'"', maximum_bytes=64)


if __name__ == "__main__":
    unittest.main()
