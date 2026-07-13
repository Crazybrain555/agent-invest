from __future__ import annotations

import unittest

from disclosure_anchor.adapters.sources.cninfo.classification_coverage import (
    unmapped_code_counts,
)


class UnmappedCodeAuditTests(unittest.TestCase):
    def test_candidate_unknowns_are_not_hidden_by_download_survivors(self) -> None:
        gaps = unmapped_code_counts(
            {
                "011711": 145,
                "012399": 1106,
                "019999": 2,
                "01010503": 5095,
            },
            class_prefixes=("011711",),
            facet_prefixes=("0101",),
        )

        self.assertEqual(gaps, {"019999": 2})


if __name__ == "__main__":
    unittest.main()
