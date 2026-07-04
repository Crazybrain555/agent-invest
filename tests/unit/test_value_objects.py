"""Unit tests for domain value objects."""

import unittest

from disclosure_anchor.domain.value_objects.common import (
    ContentHash,
    ProviderRef,
    ReportPeriod,
    validate_filing_type,
    validate_official_provider,
    validate_report_period_for_filing_type,
)


class ContentHashTests(unittest.TestCase):
    def test_parse_lowercases_digest_and_round_trips(self) -> None:
        digest = "A" * 64
        parsed = ContentHash.parse(f"sha256:{digest}")
        self.assertEqual(parsed.algorithm, "sha256")
        self.assertEqual(parsed.digest, digest.lower())
        self.assertEqual(str(parsed), f"sha256:{digest.lower()}")

    def test_parse_requires_algorithm_separator(self) -> None:
        with self.assertRaises(ValueError):
            ContentHash.parse("a" * 64)

    def test_only_sha256_is_supported(self) -> None:
        with self.assertRaises(ValueError):
            ContentHash(algorithm="md5", digest="a" * 32)

    def test_digest_is_required(self) -> None:
        with self.assertRaises(ValueError):
            ContentHash(algorithm="sha256", digest="")


class ProviderRefTests(unittest.TestCase):
    def test_accepts_populated_fields(self) -> None:
        ref = ProviderRef(provider="cninfo", provider_document_id="1225087169")
        self.assertEqual(ref.provider, "cninfo")
        self.assertEqual(ref.provider_document_id, "1225087169")

    def test_provider_is_required(self) -> None:
        with self.assertRaises(ValueError):
            ProviderRef(provider="", provider_document_id="1225087169")

    def test_provider_document_id_is_required(self) -> None:
        with self.assertRaises(ValueError):
            ProviderRef(provider="cninfo", provider_document_id="")


class ReportPeriodTests(unittest.TestCase):
    def test_parse_accepts_contract_labels(self) -> None:
        self.assertEqual(str(ReportPeriod.parse("2025A")), "2025A")
        self.assertEqual(str(ReportPeriod.parse("2025Q2")), "2025Q2")

    def test_parse_rejects_non_contract_labels(self) -> None:
        for value in ("2025H1", "25A", "2025Q5"):
            with self.assertRaises(ValueError):
                ReportPeriod.parse(value)

    def test_filing_type_and_provider_vocabularies_are_closed(self) -> None:
        self.assertEqual(validate_filing_type("annual_report"), "annual_report")
        self.assertEqual(validate_official_provider("cninfo"), "cninfo")
        with self.assertRaises(ValueError):
            validate_filing_type("free_text")
        with self.assertRaises(ValueError):
            validate_official_provider("local")

    def test_period_required_for_regular_reports(self) -> None:
        with self.assertRaises(ValueError):
            validate_report_period_for_filing_type(
                filing_type="annual_report", report_period=None
            )


if __name__ == "__main__":
    unittest.main()
