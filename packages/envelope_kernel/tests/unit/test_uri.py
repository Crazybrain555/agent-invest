import unittest

from envelope_kernel.kinds import AssetKind
from envelope_kernel.uri import AssetUri, build_asset_uri, parse_asset_uri


class AssetUriTests(unittest.TestCase):
    def test_build_matches_protocol_example(self) -> None:
        uri = build_asset_uri("disclosure_anchor", 1, AssetKind.DOCUMENT_UNIT, "da_0001")
        self.assertEqual(uri, "asset://disclosure_anchor/v1/document_unit/da_0001")

    def test_round_trip(self) -> None:
        original = AssetUri("asset_intake", 1, AssetKind.DATASET_SNAPSHOT, "snap 01/α")
        parsed = parse_asset_uri(str(original))
        self.assertEqual(parsed, original)

    def test_slash_in_stable_id_is_encoded_not_hierarchy(self) -> None:
        uri = build_asset_uri("svc", 1, AssetKind.TOOL_RESULT, "a/b")
        self.assertEqual(uri, "asset://svc/v1/tool_result/a%2Fb")
        self.assertEqual(parse_asset_uri(uri).stable_id, "a/b")

    def test_reject_bad_inputs(self) -> None:
        with self.assertRaises(ValueError):
            build_asset_uri("", 1, AssetKind.DOCUMENT_UNIT, "x")
        with self.assertRaises(ValueError):
            build_asset_uri("svc", 0, AssetKind.DOCUMENT_UNIT, "x")
        with self.assertRaises(ValueError):
            build_asset_uri("svc", 1, AssetKind.DOCUMENT_UNIT, "")

    def test_reject_malformed_uris(self) -> None:
        for bad in (
            "ev://evidence/v1/record/r1",  # wrong scheme
            "asset://svc/v1/document_unit",  # missing segment
            "asset://svc/v1/document_unit/x/y",  # extra segment
            "asset://svc/1/document_unit/x",  # bad version form
            "asset://svc/v1/not_a_kind/x",  # unknown kind
            "asset://svc/v1//x",  # empty segment
        ):
            with self.assertRaises(ValueError, msg=bad):
                parse_asset_uri(bad)


if __name__ == "__main__":
    unittest.main()
