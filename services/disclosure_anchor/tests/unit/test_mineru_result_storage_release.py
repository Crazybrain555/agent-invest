"""Release inputs and binding ceilings for a result-storage (v2) capacity.

Synthetic, file-local release inputs only; the complete Git-tree release build
and the Windows installer/collector are not exercised here.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from disclosure_anchor.adapters.runtime.mineru_release_binding import _require_storage_ceilings
from disclosure_anchor.adapters.runtime.mineru_release_package import ReleaseIdentityError, load_release_inputs
from disclosure_anchor.application.contracts import mineru_capacity_config as codec
from scripts.attest_mineru_remote_runtime import API_ENV_KEYS, _api_environment
from tests.unit.test_mineru_materialize_grant_v5 import MIB, STORAGE_POLICY
from tests.unit.test_mineru_release_package_independent import LOCAL
from tests.unit.test_mineru_release_compose_independent import DEPLOYMENT
from tests.unit.test_mineru_release_projection_independent import _capacity


def storage_capacity() -> codec.MineruCapacityConfigV2:
    legacy = asdict(_capacity())
    for name in ("contract_version", "result_reservation_bytes", "max_unacked_result_bytes"):
        del legacy[name]
    return codec.MineruCapacityConfigV2(
        contract_version="mineru.capacity-config.v2", result_storage=STORAGE_POLICY, **legacy,
    )


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class StorageReleaseInputTests(unittest.TestCase):
    def test_attester_requires_exact_environment_for_each_capacity_version(self) -> None:
        v1 = _capacity()
        v2 = storage_capacity()
        common = {name: "configured" for name in API_ENV_KEYS}
        for capacity in (v1, v2):
            environment = {**common, **codec.capacity_environment(capacity), "MINERU_DEVICE_MODE": "cuda:0"}
            with self.subTest(version=capacity.contract_version):
                self.assertEqual(
                    _api_environment(
                        environment, expected_capacity=capacity, device_keys={"MINERU_DEVICE_MODE"}
                    ),
                    environment,
                )
                missing_finalizer = dict(environment)
                del missing_finalizer["MINERU_API_FINALIZER_SLOTS"]
                with self.assertRaisesRegex(ValueError, "remote environment observation is invalid"):
                    _api_environment(
                        missing_finalizer, expected_capacity=capacity, device_keys={"MINERU_DEVICE_MODE"}
                    )
                for legacy_name in (
                    "MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES",
                    "MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES",
                ):
                    modified = dict(environment)
                    if capacity is v2:
                        modified[legacy_name] = "268435456"
                    else:
                        del modified[legacy_name]
                    with self.assertRaisesRegex(ValueError, "remote environment observation is invalid"):
                        _api_environment(
                            modified, expected_capacity=capacity, device_keys={"MINERU_DEVICE_MODE"}
                        )

    def test_release_inputs_decode_v2_and_project_no_legacy_budgets(self) -> None:
        capacity = storage_capacity()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "capacity.json").write_bytes(capacity.exact_bytes)
            (root / "deployment.json").write_bytes(canonical(DEPLOYMENT))
            (root / "local.json").write_bytes(canonical(LOCAL))
            inputs = load_release_inputs(root / "capacity.json", root / "deployment.json", root / "local.json")
        self.assertEqual(inputs.capacity, capacity)
        environment = codec.capacity_environment(inputs.capacity)
        self.assertNotIn("MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES", environment)
        self.assertNotIn("MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES", environment)
        legacy = codec.capacity_environment(_capacity())
        self.assertEqual(
            environment,
            {name: value for name, value in legacy.items() if "TASK_PROTOCOL_V2" not in name},
        )

    def test_mac_ceilings_must_cash_one_maximal_storage_grant(self) -> None:
        capacity = storage_capacity()
        policy = capacity.result_storage
        enough = SimpleNamespace(
            source_pdf_bytes_limit=policy.source_pdf_bytes_limit,
            temporary_disk_bytes_limit=codec.mac_document_disk_upper_bound(
                policy, policy.native_result_hard_limit_bytes, policy.native_source_single_limit_bytes,
            ),
            decoded_payload_bytes_limit=policy.mac_decode_working_set_budget_bytes,
            terminal_output_bytes_limit=policy.native_source_single_limit_bytes
            + policy.mac_decode_working_set_budget_bytes,
        )
        _require_storage_ceilings(capacity, enough)
        for field, value in (
            ("source_pdf_bytes_limit", policy.source_pdf_bytes_limit + MIB),
            ("temporary_disk_bytes_limit", enough.temporary_disk_bytes_limit - 1),
            ("decoded_payload_bytes_limit", enough.decoded_payload_bytes_limit - 1),
            ("terminal_output_bytes_limit", enough.terminal_output_bytes_limit - 1),
            ("temporary_disk_bytes_limit", policy.mac_work_disk_limit_bytes + 1),  # Above D.
        ):
            short = SimpleNamespace(**{**vars(enough), field: value})
            with self.subTest(field=field), self.assertRaises(ReleaseIdentityError):
                _require_storage_ceilings(capacity, short)


if __name__ == "__main__":
    unittest.main()
