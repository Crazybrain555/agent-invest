"""Real MinerU no-merge table-closure canary."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import unittest

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import (
    MinerUArtifactReader,
)
from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    resolved_image_path,
    resolved_table_html,
)
from disclosure_anchor.adapters.parsers.mineru.table_html_structure import (
    parse_table_html_structure,
)
from disclosure_anchor.adapters.parsers.mineru.table_reconciler import (
    reconcile_content_list_tables,
)


DEFAULT_CANARY_ROOT = Path(
    "/private/tmp/disclosure-table-no-merge-canary-1218382773-p167-168-r3/"
    "sha256_2ffa5bdc0c2bd00c16b360ebdf23e1229f44be56b950a862ca653c445953a3d1/"
    "vlm"
)
CANARY_ROOT = Path(
    os.environ.get("DISCLOSURE_TABLE_NO_MERGE_CANARY", str(DEFAULT_CANARY_ROOT))
)
STEM = (
    "sha256_2ffa5bdc0c2bd00c16b360ebdf23e1229f44be56b950a862ca653c445953a3d1"
)
EXPECTED_HASHES = {
    "origin": "1a6955f94adcaa412bff573546845fb8dc1c492eae5ef23950b51d7c99ad7d49",
    "content_list": (
        "bc55a8109435fda7cb3ef1decb70b7a71d03fb4afe8335e8359ead8ee8c2bad1"
    ),
    "model": "82b42fd531bccab0bdb5ce6fe00c7ee990971fc1c10c965640a70320b19e1c7f",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RealMinerUTableReconciliationTests(unittest.TestCase):
    @unittest.skipUnless(
        (CANARY_ROOT / f"{STEM}_content_list.json").is_file(),
        "fresh 1218382773 MinerU no-merge canary is absent",
    )
    def test_1218382773_pages_167_168_close_as_four_page_local_tables(
        self,
    ) -> None:
        paths = {
            "origin": CANARY_ROOT / f"{STEM}_origin.pdf",
            "content_list": CANARY_ROOT / f"{STEM}_content_list.json",
            "model": CANARY_ROOT / f"{STEM}_model.json",
        }
        self.assertEqual(
            {role: _sha256(path) for role, path in paths.items()},
            EXPECTED_HASHES,
        )
        artifact = MinerUArtifactReader().read_content_artifact(
            paths["content_list"]
        )
        content = artifact.items
        registered = artifact.evidence_image_paths
        registered_outer = {
            f"evidence_image_{index:06d}"
            for index, item in enumerate(content)
            if resolved_image_path(item) is not None
        }
        self.assertTrue(registered_outer <= set(registered))

        result = reconcile_content_list_tables(
            content,
            model_path=paths["model"],
            registered_evidence_image_paths=registered,
            content_table_structures=artifact.table_structures,
        )

        self.assertEqual(result.content_list, content)
        self.assertEqual(
            result.stats.as_dict(),
            {
                "algorithm_version": "mineru-page-local-table-closure.v6",
                "model_hash": "sha256:" + EXPECTED_HASHES["model"],
                "content_tables": 4,
                "model_tables": 4,
                "matched_tables": 4,
                "page_local_closed": True,
            },
        )
        page_rows: dict[int, list[list[str]]] = {}
        for item in content:
            if item.get("type") != "table":
                continue
            html = resolved_table_html(item)
            self.assertIsInstance(html, str)
            structure = parse_table_html_structure(str(html))
            page_rows.setdefault(int(item["page_idx"]), []).extend(
                [list(row) for row in structure.rows]
            )

        first_page_rows = page_rows[0]
        second_page_rows = page_rows[1]
        self.assertTrue(
            any(
                "航空工业" in set(row)
                and "272,517.84" in set(row)
                for row in first_page_rows
            )
        )
        self.assertTrue(
            any(
                "黎明公司" in set(row)
                and "974,082.55" in set(row)
                for row in second_page_rows
            )
        )
        self.assertFalse(
            any(
                {"航空工业", "黎明公司"}.issubset(set(row))
                for rows in page_rows.values()
                for row in rows
            )
        )


if __name__ == "__main__":
    unittest.main()
