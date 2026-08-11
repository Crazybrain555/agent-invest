from __future__ import annotations

import copy
import json
from pathlib import PurePosixPath
import unittest

from disclosure_anchor.application.contracts.normalized_ir_v4_evidence import (
    HistoricalEvidenceArtifact,
    HistoricalEvidenceClaim,
    HistoricalNormalizedIRV4EvidenceError,
    resolve_historical_normalized_ir_v4_evidence,
)


_SOURCE_SHA256 = "sha256:" + "a" * 64
_ARTIFACT_SHA256 = "sha256:" + "b" * 64
_CLAIM = HistoricalEvidenceClaim(
    artifact_role="source_bbox_visual_000001_000001",
    sha256=_ARTIFACT_SHA256,
    size_bytes=17,
)


def _payload() -> dict:
    return {
        "contract_version": "normalized_ir.v4",
        "document_id": "doc_1",
        "source_pdf_sha256": _SOURCE_SHA256,
        "elements": [{"raw_kind": "legacy-or-unknown"}],
        "parser_artifacts": {
            "artifact_root_relpath": "parser_artifacts/doc/run",
            "files": {
                _CLAIM.artifact_role: {
                    "availability": "present",
                    "relpath": "parser_artifacts/doc/run/evidence.png",
                    "sha256": _CLAIM.sha256,
                    "size_bytes": _CLAIM.size_bytes,
                },
                "unselected_broken_role": "ignored by the read projection",
            },
        },
    }


def _content(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")


def _resolve(
    content: bytes,
    *,
    ir_relpath: PurePosixPath = PurePosixPath(
        "derived/normalized_ir/doc/run/normalized_ir.v4.json"
    ),
    claims: tuple[HistoricalEvidenceClaim, ...] = (_CLAIM,),
) -> HistoricalEvidenceArtifact:
    return resolve_historical_normalized_ir_v4_evidence(
        content,
        ir_relpath=ir_relpath,
        expected_document_id="doc_1",
        expected_source_pdf_sha256=_SOURCE_SHA256,
        claims=claims,
    )


class HistoricalNormalizedIRV4EvidenceTests(unittest.TestCase):
    def assert_reason(
        self,
        expected: str,
        content: bytes,
        **kwargs: object,
    ) -> None:
        with self.assertRaises(HistoricalNormalizedIRV4EvidenceError) as caught:
            _resolve(content, **kwargs)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.reason, expected)

    def test_minimal_projection_ignores_old_writer_semantics(self) -> None:
        artifact = _resolve(_content(_payload()))

        self.assertEqual(
            artifact.relpath,
            PurePosixPath("parser_artifacts/doc/run/evidence.png"),
        )
        self.assertEqual(artifact.sha256, _ARTIFACT_SHA256)
        self.assertEqual(artifact.size_bytes, 17)

    def test_rejects_wrong_primary_identity_and_non_strict_json(self) -> None:
        cases: dict[str, tuple[bytes, dict[str, object]]] = {}
        for label, field, value in (
            ("version", "contract_version", "normalized_ir.v3"),
            ("document", "document_id", "doc_other"),
            ("source", "source_pdf_sha256", "sha256:" + "c" * 64),
        ):
            payload = _payload()
            payload[field] = value
            cases[label] = (_content(payload), {})
        cases["filename"] = (
            _content(_payload()),
            {
                "ir_relpath": PurePosixPath(
                    "derived/normalized_ir/doc/run/normalized_ir.v3.json"
                )
            },
        )
        cases["duplicate"] = (
            (
                b'{"contract_version":"normalized_ir.v4",'
                b'"document_id":"doc_1","document_id":"doc_2",'
                b'"source_pdf_sha256":"sha256:'
                + b"a" * 64
                + b'","parser_artifacts":{}}'
            ),
            {},
        )
        cases["nan"] = (
            _content(_payload())[:-1] + b',"invalid":NaN}',
            {},
        )

        for label, (content, kwargs) in cases.items():
            with self.subTest(label=label):
                self.assert_reason("normalized_ir_invalid", content, **kwargs)

    def test_parser_artifacts_and_selected_descriptor_are_closed(self) -> None:
        extra_parser_field = _payload()
        extra_parser_field["parser_artifacts"]["legacy"] = True
        self.assert_reason(
            "normalized_ir_invalid",
            _content(extra_parser_field),
        )

        extra_descriptor_field = _payload()
        extra_descriptor_field["parser_artifacts"]["files"][
            _CLAIM.artifact_role
        ]["media_type"] = "image/png"
        self.assert_reason(
            "normalized_ir_invalid",
            _content(extra_descriptor_field),
        )

        boolean_size = _payload()
        boolean_size["parser_artifacts"]["files"][_CLAIM.artifact_role][
            "size_bytes"
        ] = True
        boolean_claim = HistoricalEvidenceClaim(
            artifact_role=_CLAIM.artifact_role,
            sha256=_CLAIM.sha256,
            size_bytes=1,
        )
        self.assert_reason(
            "normalized_ir_invalid",
            _content(boolean_size),
            claims=(boolean_claim,),
        )

    def test_selected_manifest_must_match_every_authorized_claim(self) -> None:
        for label, mutate in (
            (
                "missing",
                lambda payload: payload["parser_artifacts"]["files"].pop(
                    _CLAIM.artifact_role
                ),
            ),
            (
                "not_emitted",
                lambda payload: payload["parser_artifacts"]["files"].__setitem__(
                    _CLAIM.artifact_role,
                    {"availability": "not_emitted"},
                ),
            ),
            (
                "hash",
                lambda payload: payload["parser_artifacts"]["files"][
                    _CLAIM.artifact_role
                ].__setitem__("sha256", "sha256:" + "c" * 64),
            ),
            (
                "size",
                lambda payload: payload["parser_artifacts"]["files"][
                    _CLAIM.artifact_role
                ].__setitem__("size_bytes", 18),
            ),
        ):
            payload = _payload()
            mutate(payload)
            with self.subTest(label=label):
                self.assert_reason(
                    "evidence_manifest_mismatch",
                    _content(payload),
                )

        payload = _payload()
        second_claim = HistoricalEvidenceClaim(
            artifact_role="source_bbox_visual_000001_000002",
            sha256=_ARTIFACT_SHA256,
            size_bytes=17,
        )
        payload["parser_artifacts"]["files"][second_claim.artifact_role] = {
            "availability": "present",
            "relpath": "parser_artifacts/doc/run/second.png",
            "sha256": "sha256:" + "c" * 64,
            "size_bytes": 17,
        }
        self.assert_reason(
            "evidence_manifest_mismatch",
            _content(payload),
            claims=(_CLAIM, second_claim),
        )

    def test_selected_paths_must_be_canonical_and_below_artifact_root(self) -> None:
        for label, root, selected in (
            ("absolute", "parser_artifacts/doc/run", "/tmp/evidence.png"),
            ("backslash", "parser_artifacts/doc/run", "parser\\evidence.png"),
            ("nul", "parser_artifacts/doc/run", "parser/a\x00.png"),
            ("dot", "parser_artifacts/doc/run", "parser_artifacts/./evidence.png"),
            ("dotdot", "parser_artifacts/doc/run", "parser_artifacts/../evidence.png"),
            ("file_uri", "parser_artifacts/doc/run", "file:evidence.png"),
            ("drive", "parser_artifacts/doc/run", "C:/evidence.png"),
            ("equal_root", "parser_artifacts/doc/run", "parser_artifacts/doc/run"),
            ("escape", "parser_artifacts/doc/run", "parser_artifacts/other/evidence.png"),
            ("unsafe_root", "../parser_artifacts", "parser_artifacts/evidence.png"),
        ):
            payload = copy.deepcopy(_payload())
            payload["parser_artifacts"]["artifact_root_relpath"] = root
            payload["parser_artifacts"]["files"][_CLAIM.artifact_role][
                "relpath"
            ] = selected
            with self.subTest(label=label):
                self.assert_reason(
                    "normalized_ir_invalid",
                    _content(payload),
                )


if __name__ == "__main__":
    unittest.main()
