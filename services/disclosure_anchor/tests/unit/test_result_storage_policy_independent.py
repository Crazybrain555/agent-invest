"""Independent result-storage contract checks with bounded stdlib ZIP witnesses."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from disclosure_anchor.application.contracts import mineru_capacity_config as storage
from disclosure_anchor.application.contracts.provider_document_envelope import (
    provider_document_envelope_to_bytes,
)
from tests._mineru_capacity_config_fixture import CAPACITY_BYTES, capacity_payload
from tests.unit.test_provider_document_envelope import _envelope


MIB = 1024 * 1024


def policy_values(**changes: object) -> dict[str, object]:
    """One synthetic, viable volume allocation; no machine observations."""

    values: dict[str, object] = {
        "contract_version": "mineru.result-storage-policy.v1",
        "native_volume_identity": "scratch-native-volume",
        "native_volume_total_bytes": 100 * MIB,
        "native_work_disk_limit_bytes": 12 * MIB,
        "native_free_floor_bytes": 20 * MIB,
        "native_source_pool_bytes": 6 * MIB,
        "native_completion_escrow_bytes": 4 * MIB,
        "native_metadata_reserve_bytes": MIB,
        "native_source_single_limit_bytes": 2 * MIB,
        "native_growing_producer_limit": 1,
        "native_result_hard_limit_bytes": 3 * MIB,
        "native_normal_unacked_target_bytes": 2 * MIB,
        "initial_result_estimate_bytes": MIB,
        "native_allocation_unit_bytes": 4096,
        "native_file_overhead_bytes": 4096,
        "source_pdf_bytes_limit": MIB,
        "mac_volume_identity": "scratch-mac-volume",
        "mac_volume_total_bytes": 100 * MIB,
        "mac_work_disk_limit_bytes": 16 * MIB,
        "mac_free_floor_bytes": 20 * MIB,
        "mac_normal_output_target_bytes": 2 * MIB,
        "mac_decode_input_limit_bytes": MIB // 4,
        "mac_decode_working_set_budget_bytes": 2 * MIB,
        "mac_decode_expansion_factor": 4,
        "mac_decode_stage_seconds": 300,
        "max_members": 5,
        "max_name_bytes": 128,
        "max_inventory_bytes": 64 * 1024,
        "transfer_logical_deadline_seconds": 60,
        "progress_window_seconds": 10,
        "minimum_progress_bytes": MIB,
    }
    values.update(changes)
    return values


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def zipped_members(
    destination: Path,
    members: tuple[tuple[str, bytes], ...],
    *,
    forced_zip64_close: bool = False,
) -> int:
    """Write exact selected format, including forced ZIP64 local headers."""

    def write() -> None:
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for name, payload in members:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                with archive.open(info, "w", force_zip64=True) as member:
                    member.write(payload)

    if forced_zip64_close:
        # Force both size and file-count ZIP64 end records with tiny data.
        with patch.object(zipfile, "ZIP64_LIMIT", 32), patch.object(zipfile, "ZIP_FILECOUNT_LIMIT", 2):
            write()
    else:
        write()
    if forced_zip64_close:
        with destination.open("rb") as stream:
            stream.seek(max(0, destination.stat().st_size - 256))
            tail = stream.read()
        if b"PK\x06\x06" not in tail or b"PK\x06\x07" not in tail:
            raise AssertionError("forced ZIP64 close records were not written")
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise AssertionError("stdlib produced an invalid ZIP")
    return destination.stat().st_size


class ResultStoragePolicyIndependentTests(unittest.TestCase):
    def test_zip_bounds_cover_incompressible_empty_utf8_and_forced_zip64_close(self) -> None:
        generator = random.Random(20260927)
        cases = (
            (("random.bin", generator.randbytes(2 * MIB)),),
            (("empty", b""), ("研究_表格_αβ.txt", b"abc")),
            (("a", b""), ("b", b"x" * 100), ("c", b"z" * 99)),
        )
        with tempfile.TemporaryDirectory(prefix="zip-bound-independent-") as scratch:
            for number, members in enumerate(cases):
                for forced in (False, True):
                    with self.subTest(case=number, forced_zip64_close=forced):
                        actual = zipped_members(
                            Path(scratch) / f"case-{number}-{int(forced)}.zip",
                            members,
                            forced_zip64_close=forced,
                        )
                        sizes = tuple((len(data), len(name.encode("utf-8"))) for name, data in members)
                        exact_bound = storage.retained_zip_upper_bound(sizes)
                        envelope = storage.retained_zip_envelope_upper_bound(
                            sum(size for size, _ in sizes), len(sizes), max(name for _, name in sizes)
                        )
                        self.assertLessEqual(actual, exact_bound)
                        self.assertLessEqual(exact_bound, envelope)
                        if number == 0:
                            self.assertGreater(actual, 2 * MIB)

    def test_more_than_old_4096_member_cap_still_has_a_sound_bound(self) -> None:
        count = 4097
        members = tuple((f"m{index:04d}", b"") for index in range(count))
        with tempfile.TemporaryDirectory(prefix="zip-many-independent-") as scratch:
            actual = zipped_members(Path(scratch) / "many.zip", members)
        sizes = tuple((0, len(name)) for name, _ in members)
        self.assertLessEqual(actual, storage.retained_zip_upper_bound(sizes))
        self.assertLessEqual(
            actual,
            storage.retained_zip_envelope_upper_bound(0, count, 5),
        )

    def test_old_128mib_source_estimate_rejects_a_small_real_zip(self) -> None:
        source_bytes = 128 * MIB
        old_initial_budget = 256 * MIB
        old_estimate = 2 * source_bytes + 65536 + MIB
        self.assertGreater(old_estimate, old_initial_budget)
        with tempfile.TemporaryDirectory(prefix="zip-sparse-independent-") as scratch:
            source = Path(scratch) / "sparse.bin"
            with source.open("wb") as stream:
                stream.truncate(source_bytes)
            result = Path(scratch) / "result.zip"
            with zipfile.ZipFile(result, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                info = zipfile.ZipInfo("sparse.bin", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                with source.open("rb") as input_file, archive.open(info, "w", force_zip64=True) as output:
                    while chunk := input_file.read(MIB):
                        output.write(chunk)
            actual = result.stat().st_size
            with zipfile.ZipFile(result) as archive:
                self.assertEqual(archive.getinfo("sparse.bin").file_size, source_bytes)
                self.assertIsNone(archive.testzip())
        self.assertLess(actual, old_initial_budget)
        self.assertLessEqual(actual, storage.retained_zip_upper_bound(((source_bytes, 10),)))

    def test_v1_canonical_bytes_remain_exact_and_v2_separates_soft_from_hard(self) -> None:
        old = storage.decode_any_mineru_capacity_config(CAPACITY_BYTES)
        self.assertIs(type(old), storage.MineruCapacityConfig)
        self.assertEqual(old.exact_bytes, CAPACITY_BYTES)
        self.assertEqual(old.sha256, "sha256:" + hashlib.sha256(CAPACITY_BYTES).hexdigest())

        policy = storage.MineruResultStoragePolicy(**policy_values())
        v2_fields = capacity_payload()
        v2_fields.pop("result_reservation_bytes")
        v2_fields.pop("max_unacked_result_bytes")
        v2_fields["contract_version"] = "mineru.capacity-config.v2"
        v2 = storage.MineruCapacityConfigV2(**v2_fields, result_storage=policy)
        self.assertEqual(policy.exact_bytes, canonical(policy_values()))
        self.assertEqual(storage.decode_mineru_result_storage_policy(policy.exact_bytes), policy)
        self.assertEqual(storage.decode_any_mineru_capacity_config(v2.exact_bytes), v2)
        self.assertEqual(v2.sha256, "sha256:" + hashlib.sha256(v2.exact_bytes).hexdigest())
        self.assertEqual(set(asdict(v2)) & {"result_reservation_bytes", "max_unacked_result_bytes"}, set())
        environment = storage.capacity_environment(v2)
        self.assertNotIn("MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES", environment)
        self.assertNotIn("MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES", environment)
        self.assertLess(policy.initial_result_estimate_bytes, policy.native_result_hard_limit_bytes)
        self.assertLess(policy.native_normal_unacked_target_bytes, policy.native_result_hard_limit_bytes)
        with self.assertRaises(ValueError):
            storage.decode_mineru_capacity_config(v2.exact_bytes)
        with self.assertRaises(ValueError):
            storage.decode_mineru_capacity_config_v2(CAPACITY_BYTES)

    def test_budget_relations_reject_uncashable_grants(self) -> None:
        invalid = (
            {"native_source_pool_bytes": 8 * MIB},  # P + C + M > D
            {"native_free_floor_bytes": 99 * MIB},  # D + H > volume
            {"native_source_single_limit_bytes": 7 * MIB},  # one growth permit > P
            {"native_result_hard_limit_bytes": 2 * MIB},  # cannot finish supported source
            {"native_result_hard_limit_bytes": 5 * MIB},  # hard result > C
            {"native_normal_unacked_target_bytes": 5 * MIB},
            {"initial_result_estimate_bytes": 3 * MIB},
            {"mac_free_floor_bytes": 99 * MIB},
            {"mac_normal_output_target_bytes": 17 * MIB},
            {"mac_work_disk_limit_bytes": 6 * MIB},  # Z + two S copies > D
            {"mac_decode_working_set_budget_bytes": MIB // 2},
            {"transfer_logical_deadline_seconds": 20},
            {"native_allocation_unit_bytes": 3000},
            {"native_volume_total_bytes": True},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                storage.MineruResultStoragePolicy(**policy_values(**changes))

        policy = storage.MineruResultStoragePolicy(**policy_values(source_pdf_bytes_limit=2 * MIB))
        v2_fields = capacity_payload()
        v2_fields.pop("result_reservation_bytes")
        v2_fields.pop("max_unacked_result_bytes")
        v2_fields["contract_version"] = "mineru.capacity-config.v2"
        with self.assertRaises(ValueError):
            storage.MineruCapacityConfigV2(**v2_fields, result_storage=policy)

    def test_policy_decoder_rejects_noncanonical_or_open_payloads(self) -> None:
        valid = storage.MineruResultStoragePolicy(**policy_values()).exact_bytes
        for raw in (
            valid + b"\n",
            b" " + valid,
            canonical({**policy_values(), "unexpected": 1}),
            valid.replace(b'"max_members":5', b'"max_members":5,"max_members":5'),
            valid.replace(b'"max_members":5', b'"max_members":NaN'),
        ):
            with self.subTest(raw=raw[:50]), self.assertRaises(ValueError):
                storage.decode_mineru_result_storage_policy(raw)

    def test_unrepresentable_computed_bounds_cannot_enter_a_budget_policy(self) -> None:
        largest = (1 << 63) - 1
        cases = (
            ("raw_deflate", lambda: storage.raw_deflate_upper_bound(largest)),
            ("selected_zip", lambda: storage.retained_zip_upper_bound(((largest, 1),))),
            ("envelope_zip", lambda: storage.retained_zip_envelope_upper_bound(largest, 1, 1)),
        )
        for name, calculate in cases:
            with self.subTest(bound=name):
                mathematical_bound = calculate()
                self.assertGreater(mathematical_bound, largest)
                with self.assertRaises(ValueError):
                    storage.MineruResultStoragePolicy(
                        **policy_values(native_result_hard_limit_bytes=mathematical_bound)
                    )

    def test_mac_disk_grant_covers_a_contract_valid_provider_envelope(self) -> None:
        # The synthetic ProviderDocumentEnvelope contract accepts short parser
        # artifact sizes while adding its own canonical source/identity fields.
        # In the materializer, this envelope is written after the parser tree
        # is flattened, so spool + parser files + envelope coexist on disk.
        document = _envelope()
        parser_bytes = sum(item.size_bytes for item in document.provider_document.artifacts)
        envelope_bytes = len(provider_document_envelope_to_bytes(document))
        self.assertGreater(envelope_bytes, parser_bytes)
        spool_bytes = 4096
        bound = storage.mac_document_disk_upper_bound(
            storage.MineruResultStoragePolicy(**policy_values()), spool_bytes, parser_bytes
        )
        self.assertGreaterEqual(bound, spool_bytes + parser_bytes + envelope_bytes)


if __name__ == "__main__":
    unittest.main()
