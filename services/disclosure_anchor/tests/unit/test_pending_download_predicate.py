"""B7 re-download predicate matrix (spec 07 §2 去重预检)."""

from __future__ import annotations

from datetime import date
import unittest

from disclosure_anchor.adapters.db.postgres.repositories import (
    _should_download_candidate,
)
from disclosure_anchor.domain import entities as e


OVERLAP_START = date(2026, 7, 1)


def _candidate(
    *,
    title: str = "普通公告",
    announcement_date: str = "2026-06-01",
    file_size: object = 186,
) -> dict[str, object]:
    return {
        "provider_document_id": "pid-1",
        "title": title,
        "announcement_date": announcement_date,
        "file_signature_hint": {
            "file_size": file_size,
            "etag": None,
            "last_modified": None,
            "index_updated_at": None,
        },
    }


def _document(*, file_size: object = 186) -> e.Document:
    return e.Document(
        document_id="doc_x",
        status="registered",
        provider="cninfo",
        provider_document_id="pid-1",
        provider_metadata={
            "file_signature": {
                "file_size": file_size,
                "etag": None,
                "last_modified": None,
                "index_updated_at": None,
            }
        },
    )


class ShouldDownloadCandidateTests(unittest.TestCase):
    def test_unregistered_candidate_downloads(self) -> None:
        self.assertTrue(
            _should_download_candidate(
                candidate=_candidate(),
                document=None,
                overlap_start=OVERLAP_START,
            )
        )

    def test_registered_same_signature_outside_window_skips(self) -> None:
        self.assertFalse(
            _should_download_candidate(
                candidate=_candidate(announcement_date="2026-06-01"),
                document=_document(),
                overlap_start=OVERLAP_START,
            )
        )

    def test_registered_same_signature_inside_window_redownloads(self) -> None:
        # The provider size hint is not unit-stable; a matching value inside
        # the overlap window still needs raw-hash verification.
        self.assertTrue(
            _should_download_candidate(
                candidate=_candidate(announcement_date="2026-07-03"),
                document=_document(),
                overlap_start=OVERLAP_START,
            )
        )

    def test_different_signature_downloads_even_outside_window(self) -> None:
        self.assertTrue(
            _should_download_candidate(
                candidate=_candidate(announcement_date="2026-06-01", file_size=204),
                document=_document(file_size=186),
                overlap_start=OVERLAP_START,
            )
        )

    def test_correction_signal_downloads_even_outside_window(self) -> None:
        self.assertTrue(
            _should_download_candidate(
                candidate=_candidate(
                    title="关于某事项的更正公告", announcement_date="2026-06-01"
                ),
                document=_document(),
                overlap_start=OVERLAP_START,
            )
        )

    def test_unreliable_signature_outside_window_skips(self) -> None:
        self.assertFalse(
            _should_download_candidate(
                candidate=_candidate(announcement_date="2026-06-01", file_size=None),
                document=_document(file_size=None),
                overlap_start=OVERLAP_START,
            )
        )

    def test_unreliable_signature_inside_window_redownloads(self) -> None:
        self.assertTrue(
            _should_download_candidate(
                candidate=_candidate(announcement_date="2026-07-03", file_size=None),
                document=_document(file_size=None),
                overlap_start=OVERLAP_START,
            )
        )


if __name__ == "__main__":
    unittest.main()
