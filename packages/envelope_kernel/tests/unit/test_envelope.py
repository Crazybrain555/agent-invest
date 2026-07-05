import unittest
from datetime import UTC, datetime

from pydantic import ValidationError

from envelope_kernel.envelope import CONTRACT_VERSION, DataAsset

MINIMAL_CORE = {
    "asset_id": "da_0001",
    "asset_kind": "dataset_snapshot",
    "observed_at": datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
    "source_ref": "sa_0001",
    "source_tier": "tier_1",
    "trace_level": "G1",
    "payload": {"rows": []},
}


class DataAssetMinimalCoreTests(unittest.TestCase):
    def test_minimal_core_validates(self) -> None:
        asset = DataAsset.model_validate(MINIMAL_CORE)
        self.assertEqual(asset.contract_version, CONTRACT_VERSION)
        self.assertIsNone(asset.payload_kind)

    def test_each_required_field_is_enforced(self) -> None:
        for field in ("asset_id", "asset_kind", "observed_at", "source_ref", "source_tier", "trace_level"):
            data = {k: v for k, v in MINIMAL_CORE.items() if k != field}
            with self.assertRaises(ValidationError, msg=field):
                DataAsset.model_validate(data)

    def test_payload_or_raw_asset_ref_at_least_one(self) -> None:
        data = {k: v for k, v in MINIMAL_CORE.items() if k != "payload"}
        with self.assertRaises(ValidationError):
            DataAsset.model_validate(data)
        asset = DataAsset.model_validate({**data, "raw_asset_ref": "file:sha256:abc"})
        self.assertIsNone(asset.payload)

    def test_illegal_kind_combination_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            DataAsset.model_validate({**MINIMAL_CORE, "payload_kind": "text"})
        asset = DataAsset.model_validate({**MINIMAL_CORE, "payload_kind": "recordset"})
        self.assertEqual(asset.payload_kind, "recordset")

    def test_unregistered_extension_field_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            DataAsset.model_validate({**MINIMAL_CORE, "my_new_field": 1})

    def test_frozen(self) -> None:
        asset = DataAsset.model_validate(MINIMAL_CORE)
        with self.assertRaises(ValidationError):
            asset.asset_id = "other"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
