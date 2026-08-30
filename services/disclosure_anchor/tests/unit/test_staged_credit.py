from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import unittest

from disclosure_anchor.application.contracts.staged_credit import (
    CreditShapeFacts,
    CreditVector,
    DatabaseLeaseSnapshot,
    ReservationInput,
    STAGED_CREDIT_POLICY_V1,
    STAGED_STATE_TRANSITIONS,
    build_staged_credit_envelope,
    conservative_monotonic_deadline,
    credit_shape,
    decode_reservation_input,
    validate_staged_credit_envelope,
)
from tests.unit.test_mineru_process_profile import _profile


SHA_A = "sha256:" + "a" * 64
POLICY_SHA = "sha256:4b30f8207cdb9503543c9ae38da7dd92be2b55754217b976a7dd262519f90866"
INPUT_SHA = "sha256:de5c94c2076cbd94da9c936efe52a72c047c6a56fa4bb80908b438e7f945f01f"


class StagedCreditContractTests(unittest.TestCase):
    def test_policy_golden_hash_closes_thresholds_and_state_shapes(self) -> None:
        self.assertEqual(STAGED_CREDIT_POLICY_V1.sha256, POLICY_SHA)
        exact = STAGED_CREDIT_POLICY_V1.exact_bytes
        self.assertIn(b'"source_fraction_denominator":8', exact)
        self.assertIn(b'"state_credit_shapes"', exact)
        self.assertIn(b'"temp_disk_bytes":"temporary_disk_byte_count"', exact)
        self.assertIn(b'"db_staged_bytes":"db_staged_byte_count"', exact)

    def test_each_executable_policy_section_changes_policy_identity(self) -> None:
        original = json.loads(STAGED_CREDIT_POLICY_V1.exact_bytes)
        mutations = (
            ("ordered_bucket_selection", lambda value: value.reverse()),
            (
                "reservation_mechanics",
                lambda value: value.__setitem__("compressed_bytes", "literal:0"),
            ),
            (
                "state_credit_shapes",
                lambda value: value["submitted"].__setitem__("ack_items", "one"),
            ),
            (
                "state_variants",
                lambda value: value["local_failure_committed"][
                    "post_materialization"
                ].pop("local_items"),
            ),
            (
                "state_transitions",
                lambda value: value["submitted"].remove("remote_terminal"),
            ),
            ("credit_dimensions", lambda value: value.remove("db_staged_bytes")),
        )
        for section, mutate in mutations:
            with self.subTest(section=section):
                candidate = json.loads(json.dumps(original))
                mutate(candidate[section])
                exact = json.dumps(
                    candidate,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                self.assertNotEqual(
                    "sha256:" + hashlib.sha256(exact).hexdigest(), POLICY_SHA
                )

    def test_regular_heavy_huge_are_exact_profile_relative_envelopes(self) -> None:
        profile = _profile()
        cases = (
            (profile.source_pdf_bytes_limit // 16, 100, "regular", 1),
            (profile.source_pdf_bytes_limit // 4, 1000, "heavy", 2),
            (profile.source_pdf_bytes_limit * 3 // 4, 3000, "huge", 4),
        )
        for source_bytes, pages, bucket, multiplier in cases:
            with self.subTest(bucket=bucket):
                envelope = build_staged_credit_envelope(
                    profile=profile,
                    source_pdf_sha256=SHA_A,
                    source_byte_count=source_bytes,
                    source_page_count=pages,
                )
                self.assertEqual(envelope.reservation_input.value.bucket, bucket)
                self.assertEqual(
                    envelope.reservation.retained_bytes,
                    min(
                        profile.result_reservation_bytes * multiplier,
                        profile.terminal_output_bytes_limit,
                        profile.max_unacked_result_bytes,
                    ),
                )
                self.assertEqual(
                    decode_reservation_input(
                        envelope.reservation_input.exact_bytes
                    ),
                    envelope.reservation_input,
                )
                self.assertEqual(
                    envelope.reservation_input.value.reservation,
                    envelope.reservation,
                )
                validate_staged_credit_envelope(envelope, profile=profile)

    def test_same_bucket_exact_pages_scale_page_and_decoded_reservations(self) -> None:
        profile = _profile()
        two_pages = build_staged_credit_envelope(
            profile=profile,
            source_pdf_sha256=SHA_A,
            source_byte_count=1024,
            source_page_count=2,
        )
        five_pages = build_staged_credit_envelope(
            profile=profile,
            source_pdf_sha256=SHA_A,
            source_byte_count=1024,
            source_page_count=5,
        )
        self.assertEqual(two_pages.reservation_input.value.bucket, "regular")
        self.assertEqual(five_pages.reservation_input.value.bucket, "regular")
        self.assertEqual(two_pages.reservation.unpublished_pages, 2)
        self.assertEqual(five_pages.reservation.unpublished_pages, 5)
        raster_per_page = (
            profile.rasterized_page_bytes_limit + profile.resident_pages_limit - 1
        ) // profile.resident_pages_limit
        self.assertEqual(two_pages.reservation.decoded_bytes, 2 * raster_per_page)
        self.assertEqual(five_pages.reservation.decoded_bytes, 5 * raster_per_page)

    def test_long_document_does_not_use_resident_window_as_page_cap(self) -> None:
        profile = _profile()
        pages = max(profile.resident_pages_limit + 1, 600)
        envelope = build_staged_credit_envelope(
            profile=profile,
            source_pdf_sha256=SHA_A,
            source_byte_count=profile.source_pdf_bytes_limit // 16,
            source_page_count=pages,
        )
        self.assertEqual(envelope.reservation_input.value.bucket, "heavy")

    def test_int64_profile_boundaries_use_capped_arithmetic(self) -> None:
        maximum = (1 << 63) - 1
        profile = replace(
            _profile(),
            api_task_slots=1,
            api_max_pending_tasks=1,
            finalizer_slots=1,
            source_pdf_bytes_limit=maximum,
            result_reservation_bytes=maximum,
            max_unacked_result_bytes=maximum,
            terminal_output_bytes_limit=maximum,
            rasterized_page_bytes_limit=maximum,
            decoded_payload_bytes_limit=maximum,
            temporary_disk_bytes_limit=maximum,
            db_staged_bytes_limit=maximum,
        )
        for source_bytes, pages, bucket in (
            (maximum // 8, 512, "regular"),
            (maximum // 4, 1000, "heavy"),
            (maximum, profile.unpublished_pages_limit, "huge"),
        ):
            with self.subTest(bucket=bucket):
                envelope = build_staged_credit_envelope(
                    profile=profile,
                    source_pdf_sha256=SHA_A,
                    source_byte_count=source_bytes,
                    source_page_count=pages,
                )
                self.assertEqual(envelope.reservation_input.value.bucket, bucket)
                self.assertTrue(envelope.reservation.fits(CreditVector(**{
                    item: maximum for item in envelope.reservation.nonzero()
                })))

    def test_input_replay_rejects_duplicate_noncanonical_and_nested_drift(self) -> None:
        envelope = build_staged_credit_envelope(
            profile=_profile(),
            source_pdf_sha256=SHA_A,
            source_byte_count=1024,
            source_page_count=2,
        )
        exact = envelope.reservation_input.exact_bytes
        self.assertEqual(envelope.reservation_input.sha256, INPUT_SHA)
        self.assertEqual(envelope.reservation_input.byte_count, 690)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            decode_reservation_input(exact[:-1] + b',"bucket":"regular"}')
        with self.assertRaisesRegex(ValueError, "canonical"):
            decode_reservation_input(exact.replace(b'":', b'": ', 1))
        with self.assertRaisesRegex(ValueError, "credit vector"):
            decode_reservation_input(
                exact.replace(b'"documents":1', b'"legacy_documents":1')
            )
        with self.assertRaisesRegex(ValueError, "drifted"):
            validate_staged_credit_envelope(
                replace(
                    envelope,
                    reservation=replace(envelope.reservation, documents=0),
                ),
                profile=_profile(),
            )

    def test_policy_and_envelope_types_are_structurally_closed(self) -> None:
        with self.assertRaises(TypeError):
            STAGED_STATE_TRANSITIONS["prepared"] = frozenset()  # type: ignore[index]
        envelope = build_staged_credit_envelope(
            profile=_profile(),
            source_pdf_sha256=SHA_A,
            source_byte_count=1024,
            source_page_count=2,
        )
        with self.assertRaisesRegex(ValueError, "exact credit vector"):
            replace(
                envelope.reservation_input.value,
                reservation=envelope.reservation.nonzero(),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "exact encoded"):
            replace(envelope, reservation_input={})  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "exact credit vector"):
            ReservationInput(
                source_pdf_sha256=SHA_A,
                source_byte_count=1,
                source_page_count=1,
                process_profile_sha256=SHA_A,
                credit_policy_sha256=POLICY_SHA,
                bucket="regular",
                reservation={},  # type: ignore[arg-type]
            )

    def test_missing_or_out_of_range_source_facts_fail_closed(self) -> None:
        profile = _profile()
        for source_bytes, pages in (
            (0, 1),
            (1, 0),
            (profile.source_pdf_bytes_limit + 1, 1),
            (1, profile.unpublished_pages_limit + 1),
        ):
            with self.subTest(source_bytes=source_bytes, pages=pages), self.assertRaises(
                ValueError
            ):
                build_staged_credit_envelope(
                    profile=profile,
                    source_pdf_sha256=SHA_A,
                    source_byte_count=source_bytes,
                    source_page_count=pages,
                )

    def test_credit_shape_uses_one_declarative_fact_vocabulary(self) -> None:
        facts = CreditShapeFacts(
            terminal_byte_count=10,
            compressed_byte_count=10,
            uncompressed_byte_count=40,
            decoded_byte_count=20,
            temporary_disk_byte_count=50,
            source_page_count=4,
            materialization_prepared=True,
        )
        self.assertEqual(
            credit_shape("materializing", facts),
            CreditVector(
                documents=1,
                retained_results=1,
                retained_bytes=10,
                local_items=1,
                compressed_bytes=10,
                decoded_bytes=20,
                temp_disk_bytes=50,
            ),
        )
        completed = replace(
            facts,
            local_materialization_completed=True,
            db_staged_byte_count=15,
        )
        self.assertEqual(credit_shape("local_materialized", completed).unpublished_pages, 4)
        self.assertEqual(credit_shape("local_materialized", completed).db_staged_bytes, 15)
        self.assertEqual(
            credit_shape("local_failure_committed", facts).local_items, 1
        )
        self.assertEqual(credit_shape("local_failed", facts), CreditVector())
        with self.assertRaisesRegex(ValueError, "prepared facts"):
            credit_shape("materializing", replace(facts, materialization_prepared=False))
        with self.assertRaisesRegex(ValueError, "prepared facts"):
            credit_shape(
                "local_failure_committed", replace(facts, source_page_count=0)
            )
        with self.assertRaisesRegex(ValueError, "stale facts"):
            credit_shape("prepared", replace(facts, materialization_prepared=False))
        pre_materialization_failure = CreditShapeFacts(terminal_byte_count=10)
        self.assertEqual(
            credit_shape("local_failure_committed", pre_materialization_failure),
            CreditVector(documents=1, retained_results=1, retained_bytes=10, ack_items=1),
        )
        self.assertEqual(
            credit_shape("local_failure_committed", facts),
            CreditVector(
                documents=1, retained_results=1, retained_bytes=10,
                local_items=1, compressed_bytes=10, decoded_bytes=20,
                temp_disk_bytes=50, ack_items=1,
            ),
        )
        self.assertEqual(
            credit_shape("local_failure_committed", completed).db_staged_bytes,
            15,
        )

    def test_database_lease_bridge_is_exact_and_conservative(self) -> None:
        observed = datetime(2026, 8, 30, tzinfo=timezone.utc)
        snapshot = DatabaseLeaseSnapshot(
            database_observed_at_utc=observed,
            lease_until_utc=observed + timedelta(microseconds=2_000_000),
            remaining_microseconds=2_000_000,
        )
        deadline = conservative_monotonic_deadline(
            snapshot, monotonic_before=10.0, monotonic_after=10.25
        )
        self.assertEqual(deadline, 12.0)
        self.assertLessEqual(deadline, 10.25 + 2.0)
        with self.assertRaisesRegex(ValueError, "not safely runnable"):
            conservative_monotonic_deadline(
                replace(
                    snapshot,
                    lease_until_utc=observed + timedelta(microseconds=10),
                    remaining_microseconds=10,
                ),
                monotonic_before=10.0,
                monotonic_after=10.1,
            )
        for invalid in (math.nan, math.inf, -math.inf, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                conservative_monotonic_deadline(
                    snapshot,
                    monotonic_before=invalid,  # type: ignore[arg-type]
                    monotonic_after=11.0,
                )
        with self.assertRaisesRegex(ValueError, "outside the closed range"):
            conservative_monotonic_deadline(
                snapshot,
                monotonic_before=-1e308,
                monotonic_after=1e308,
            )


if __name__ == "__main__":
    unittest.main()
