from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import (
    MinerUArtifactReader,
)
from disclosure_anchor.adapters.parsers.mineru.source_evidence import (
    SourceEvidenceContractError,
    resolve_middle_table_roles,
)
from disclosure_anchor.adapters.parsers.pdf_native_text import (
    NativeTextAtom,
    NativeTextLayoutRef,
    NativeTextPage,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


def _middle_payload() -> dict[str, object]:
    pre_table = {
        "type": "table",
        "bbox": [100, 200, 900, 700],
        "blocks": [
            {
                "type": "table_body",
                "bbox": [100, 200, 900, 700],
                "lines": [],
            },
            {
                "type": "table_footnote",
                "bbox": [120, 720, 880, 760],
                "lines": [
                    {
                        "spans": [
                            {"type": "text", "content": "不得作为文本真值"}
                        ]
                    }
                ],
            },
        ],
    }
    para_table = {
        "type": "table",
        "bbox": [100, 200, 900, 700],
        "blocks": [
            {
                "type": "table_body",
                "bbox": [100, 200, 900, 700],
                "lines": [],
            },
            {
                "type": "table_footnote",
                "bbox": [120, 720, 880, 760],
                "lines": [],
                "lines_deleted": True,
            },
        ],
    }
    return {
        "_backend": "vlm",
        "_version_name": "3.4.0",
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [1000, 1000],
                "preproc_blocks": [pre_table],
                "para_blocks": [para_table],
            }
        ],
    }


def _content_table(*, footnotes: list[str] | None = None) -> list[dict[str, object]]:
    return [
        {
            "type": "table",
            "page_idx": 0,
            "bbox": [100, 200, 900, 700],
            "table_caption": [],
            "table_footnote": footnotes or [],
        }
    ]


def _source_page(
    *,
    atom_bbox: tuple[float, float, float, float] = (130, 725, 300, 750),
) -> NativeTextPage:
    text = "真实附注"
    return NativeTextPage(
        page_idx=0,
        width=1000,
        height=1000,
        text=text,
        atoms=(
            NativeTextAtom(
                page_idx=0,
                order=0,
                bbox=atom_bbox,
                char_span=(0, len(text)),
                text=text,
                layout=NativeTextLayoutRef(0, 0, 0, 0),
            ),
        ),
    )


class MinerUMiddleRoleTests(unittest.TestCase):
    def test_reader_returns_only_hash_bound_role_and_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample_middle.json"
            path.write_text(
                json.dumps(_middle_payload(), ensure_ascii=False),
                encoding="utf-8",
            )
            artifact = MinerUArtifactReader().read_middle(
                path,
                expected_version="3.4.0",
                expected_backend="vlm",
                expected_page_count=1,
            )

        self.assertRegex(artifact.sha256, r"^sha256:[a-f0-9]{64}$")
        self.assertEqual(len(artifact.table_roles), 1)
        role = artifact.table_roles[0]
        self.assertEqual(role.field, "table_footnote")
        self.assertEqual(role.field_index, 0)
        self.assertEqual(role.parent_bbox, (100.0, 200.0, 900.0, 700.0))
        self.assertEqual(role.role_bbox, (120.0, 720.0, 880.0, 760.0))
        self.assertTrue(role.provider_deleted)
        self.assertNotIn("text", role.__dataclass_fields__)

    def test_reader_rejects_identity_geometry_and_cross_page_role_drift(
        self,
    ) -> None:
        cases: list[tuple[str, dict[str, object]]] = []
        wrong_identity = _middle_payload()
        wrong_identity["_backend"] = "hybrid"
        cases.append(("identity", wrong_identity))
        wrong_version = _middle_payload()
        wrong_version["_version_name"] = "3.4.1"
        cases.append(("version", wrong_version))
        bad_bbox = _middle_payload()
        bad_bbox["pdf_info"][0]["preproc_blocks"][0]["blocks"][1]["bbox"] = [
            120,
            720,
            1200,
            760,
        ]
        cases.append(("bbox", bad_bbox))
        moved_role = _middle_payload()
        moved_role["pdf_info"][0]["para_blocks"][0]["blocks"][1]["bbox"] = [
            120,
            620,
            880,
            660,
        ]
        cases.append(("role closure", moved_role))

        with tempfile.TemporaryDirectory() as tmp:
            for name, payload in cases:
                with self.subTest(name=name):
                    path = Path(tmp) / f"{name}_middle.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ParserOutputContractError):
                        MinerUArtifactReader().read_middle(
                            path,
                            expected_version="3.4.0",
                            expected_backend="vlm",
                            expected_page_count=1,
                        )

    def test_deleted_role_uses_exact_pdf_slice_not_middle_text(self) -> None:
        payload = json.dumps(_middle_payload(), ensure_ascii=False).encode()
        middle = MinerUArtifactReader().read_middle_bytes(
            payload,
            expected_version="3.4.0",
            expected_backend="vlm",
            expected_page_count=1,
        )
        roles = resolve_middle_table_roles(
            _content_table(),
            middle_artifact=middle,
            source_pages=(_source_page(),),
        )

        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0].text, "真实附注")
        self.assertTrue(roles[0].provider_deleted)

    def test_role_binding_fails_closed_on_missing_or_ambiguous_source(
        self,
    ) -> None:
        cases: list[tuple[str, str, dict[str, object], NativeTextPage]] = []
        nondeleted = _middle_payload()
        nondeleted["pdf_info"][0]["para_blocks"][0]["blocks"][1][
            "lines_deleted"
        ] = False
        cases.append(
            (
                "nondeleted missing",
                "middle_role_provider_conflict",
                nondeleted,
                _source_page(),
            )
        )
        cases.append(
            (
                "grazing-only atom leaves the role empty",
                "middle_role_source_span_unproved",
                _middle_payload(),
                _source_page(atom_bbox=(0, 700, 130, 750)),
            )
        )
        for name, reason_code, payload, page in cases:
            with self.subTest(name=name):
                middle = MinerUArtifactReader().read_middle_bytes(
                    json.dumps(payload).encode(),
                    expected_version="3.4.0",
                    expected_backend="vlm",
                    expected_page_count=1,
                )
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    resolve_middle_table_roles(
                        _content_table(),
                        middle_artifact=middle,
                        source_pages=(page,),
                    )
                self.assertEqual(raised.exception.reason_code, reason_code)


if __name__ == "__main__":
    unittest.main()
