"""Root-cause regressions for exact visual-occurrence semantic closure."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru.content_extract_runtime import (
    run_request,
)
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUToNormalizedIRMapper,
)
from disclosure_anchor.adapters.parsers.mineru.source_evidence_validator import (
    MinerUSourceEvidenceValidator,
)
from disclosure_anchor.adapters.parsers.mineru.table_html_structure import (
    parse_table_html_structure,
)
from disclosure_anchor.adapters.parsers.mineru.visual_semantic_closure import (
    VisualContentExtractResult,
    resolve_visual_semantic_closure,
    semantic_dispositions_by_source,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from disclosure_anchor.application.contracts.visual_semantics import (
    MINERU_VL_UTILS_PACKAGE_VERSION,
    VisualSemanticContractError,
    ensure_no_unresolved_visuals,
    visual_semantic_bytes,
)
from disclosure_anchor.application.ports.source_evidence import (
    SourceEvidenceValidationError,
    VerifiedParserArtifact,
)
from disclosure_anchor.application.use_cases.publish_run import (
    NormalizedIRPublicationGuard,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import (
    ParserOutputContractError,
    PublishRunError,
)
from tests.unit._current_ir import write_text_ir_bundle


_SHA = "sha256:" + "a" * 64
_RUNTIME = "sha256:" + "b" * 64


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _target() -> ParserTargetIdentity:
    return ParserTargetIdentity(
        name="MinerU",
        package_version="3.4.0",
        backend="pipeline",
        method="auto",
        language="ch",
        formula=True,
        table=True,
        runtime_bundle_identity_sha256=_RUNTIME,
    )


def _descriptor(role: str, sha256: str) -> dict[str, object]:
    return {
        "artifact_role": role,
        "sha256": sha256,
        "size_bytes": 1,
        "pixel_width": 1,
        "pixel_height": 1,
        "media_type": "image/png",
    }


def _ledger(role: str, sha256: str) -> dict[str, object]:
    return {
        "pages": [],
        "carrier_support": [],
        "visual_occurrences": [{"artifact": _descriptor(role, sha256)}],
    }


def _resolve_image(
    path: Path,
    *,
    extracted: str | None,
):
    payload = path.read_bytes()
    role = "source_visual_occurrence_000000"
    item = {
        "type": "image",
        "page_idx": 0,
        "bbox": [1, 2, 30, 40],
        "img_path": "images/source.png",
        "image_caption": [],
        "image_footnote": [],
    }
    return resolve_visual_semantic_closure(
        identity_content_list=[item],
        canonical_content_list=[item],
        table_structures={},
        artifact_resolver=lambda requested: (
            path,
            _sha(payload),
        )
        if requested == role
        else (_ for _ in ()).throw(KeyError(requested)),
        source_evidence=_ledger(role, _sha(payload)),
        source_pdf_sha256=_SHA,
        source_pdf_page_count=1,
        source_evidence_sha256=_SHA,
        content_list_sha256=_SHA,
        content_list_v2_sha256=_SHA,
        middle_sha256=_SHA,
        model_sha256=_SHA,
        parser_target=_target(),
        server_url="http://fixture",
        extractor=lambda requests: VisualContentExtractResult(
            mineru_vl_utils_version=MINERU_VL_UTILS_PACKAGE_VERSION,
            values=(extracted,) * len(requests),
        ),
    )


class VisualSemanticClosureTests(unittest.TestCase):
    def test_empty_output_is_unresolved_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crop.png"
            path.write_bytes(b"crop")
            closure = _resolve_image(path, extracted="")
        self.assertEqual(closure.status_counts()["unresolved"], 1)
        with self.assertRaises(VisualSemanticContractError) as raised:
            ensure_no_unresolved_visuals(closure)
        self.assertEqual(raised.exception.reason_code, "visual_semantics_unresolved")

    def test_missing_crop_fails_before_remote(self) -> None:
        calls = 0

        def remote(_requests):  # noqa: ANN001, ANN202
            nonlocal calls
            calls += 1
            raise AssertionError("remote must not run")

        item = {
            "type": "image",
            "page_idx": 0,
            "bbox": [1, 2, 30, 40],
        }
        with self.assertRaises(ParserOutputContractError):
            resolve_visual_semantic_closure(
                identity_content_list=[item],
                canonical_content_list=[item],
                table_structures={},
                artifact_resolver=lambda _role: (_ for _ in ()).throw(KeyError()),
                source_evidence=_ledger(
                    "source_visual_occurrence_000000",
                    _SHA,
                ),
                source_pdf_sha256=_SHA,
                source_pdf_page_count=1,
                source_evidence_sha256=_SHA,
                content_list_sha256=_SHA,
                content_list_v2_sha256=_SHA,
                middle_sha256=_SHA,
                model_sha256=_SHA,
                parser_target=_target(),
                server_url="http://fixture",
                extractor=remote,
            )
        self.assertEqual(calls, 0)

    def test_cell_text_does_not_substitute_for_embedded_media(self) -> None:
        html = (
            '<table><tr><td>邻格文字<img src="images/media.png">'
            "</td></tr></table>"
        )
        structure = parse_table_html_structure(html)
        item = {
            "type": "table",
            "page_idx": 0,
            "bbox": [1, 2, 30, 40],
            "img_path": "images/table.png",
            "table_body": html,
            "table_caption": [],
            "table_footnote": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "media.png"
            path.write_bytes(b"media")
            closure = resolve_visual_semantic_closure(
                identity_content_list=[item],
                canonical_content_list=[item],
                table_structures={0: structure},
                artifact_resolver=lambda _role: (path, _sha(b"media")),
                source_evidence={
                    "pages": [],
                    "carrier_support": [],
                    "visual_occurrences": [],
                },
                source_pdf_sha256=_SHA,
                source_pdf_page_count=1,
                source_evidence_sha256=_SHA,
                content_list_sha256=_SHA,
                content_list_v2_sha256=_SHA,
                middle_sha256=_SHA,
                model_sha256=_SHA,
                parser_target=_target(),
                server_url="http://fixture",
                extractor=lambda requests: VisualContentExtractResult(
                    MINERU_VL_UTILS_PACKAGE_VERSION,
                    (None,) * len(requests),
                ),
            )
        self.assertEqual(
            [(item.occurrence_kind, item.status) for item in closure.dispositions],
            [("table_media", "unresolved")],
        )

    def test_offline_replay_never_calls_remote_and_checks_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crop.png"
            path.write_bytes(b"crop")
            closure = _resolve_image(path, extracted="图像语义")
            payload = visual_semantic_bytes(closure)
            role = closure.dispositions[0].artifact_role
            item = {
                "type": "image",
                "page_idx": 0,
                "bbox": [1, 2, 30, 40],
                "img_path": "images/source.png",
                "image_caption": [],
                "image_footnote": [],
            }
            replay = resolve_visual_semantic_closure(
                identity_content_list=[item],
                canonical_content_list=[item],
                table_structures={},
                artifact_resolver=lambda _role: (path, _sha(b"crop")),
                source_evidence=_ledger(role, _sha(b"crop")),
                source_pdf_sha256=_SHA,
                source_pdf_page_count=1,
                source_evidence_sha256=_SHA,
                content_list_sha256=_SHA,
                content_list_v2_sha256=_SHA,
                middle_sha256=_SHA,
                model_sha256=_SHA,
                parser_target=_target(),
                server_url=None,
                persisted_artifact=payload,
            )
        self.assertEqual(replay, closure)
        with self.assertRaises(ParserOutputContractError):
            resolve_visual_semantic_closure(
                identity_content_list=[item],
                canonical_content_list=[item],
                table_structures={},
                artifact_resolver=lambda _role: (None, _sha(b"crop")),
                source_evidence=_ledger(role, _sha(b"crop")),
                source_pdf_sha256="sha256:" + "f" * 64,
                source_pdf_page_count=1,
                source_evidence_sha256=_SHA,
                content_list_sha256=_SHA,
                content_list_v2_sha256=_SHA,
                middle_sha256=_SHA,
                model_sha256=_SHA,
                parser_target=_target(),
                server_url=None,
                persisted_artifact=payload,
            )

    def test_semantic_overlay_cannot_change_title_structure_or_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crop.png"
            path.write_bytes(b"crop")
            closure = _resolve_image(
                path,
                extracted="第七节 股东情况；单位：股；□适用",
            )
        item = {
            "type": "image",
            "page_idx": 0,
            "bbox": [1, 2, 30, 40],
            "img_path": "images/source.png",
            "image_caption": [],
            "image_footnote": [],
        }
        mapper = MinerUToNormalizedIRMapper()
        common = {
            "content_list": [item],
            "parser_info": _target(),
            "document_metadata": {
                "document_id": "doc_1",
                "title": "注册标题",
            },
            "structure_proof": {"headings": [], "sentinel": "unchanged"},
            "source_pdf_sha256": _SHA,
            "source_pdf_page_count": 1,
        }
        baseline = mapper.map_content_list(**common)
        enriched = mapper.map_content_list(
            **common,
            visual_semantics_by_source=semantic_dispositions_by_source(closure),
        )
        self.assertEqual(enriched["title"], baseline["title"])
        self.assertEqual(enriched["structure_proof"], baseline["structure_proof"])
        self.assertEqual(len(enriched["elements"]), len(baseline["elements"]))
        changed = dict(enriched["elements"][0])
        self.assertEqual(
            changed.pop("visual_semantic_text"),
            "第七节 股东情况；单位：股；□适用",
        )
        self.assertEqual(changed, baseline["elements"][0])


class RuntimeBridgeTests(unittest.TestCase):
    def test_official_client_options_and_hash_precheck(self) -> None:
        class Image:
            def __enter__(self):  # noqa: ANN204
                return self

            def __exit__(self, *_args):  # noqa: ANN002, ANN204
                return None

            def load(self) -> None:
                return None

            def copy(self):  # noqa: ANN201
                return self

        calls: list[dict[str, object]] = []

        class Client:
            def batch_content_extract(self, images, *, types):  # noqa: ANN001
                return ["语义"]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image.png"
            path.write_bytes(b"bytes")
            request = {
                "server_url": "http://gpu",
                "max_concurrency": 3,
                "items": [
                    {
                        "item_id": "one",
                        "path": str(path),
                        "sha256": _sha(b"bytes"),
                        "visual_type": "image",
                    }
                ],
            }
            with mock.patch(
                "disclosure_anchor.adapters.parsers.mineru."
                "content_extract_runtime.version",
                return_value=MINERU_VL_UTILS_PACKAGE_VERSION,
            ):
                result = run_request(
                    request,
                    client_factory=lambda **kwargs: (
                        calls.append(kwargs) or Client()
                    ),
                    image_open=lambda _stream: Image(),
                )
            request["items"][0]["sha256"] = _SHA
            with self.assertRaises(ValueError):
                run_request(
                    request,
                    client_factory=lambda **kwargs: (
                        calls.append(kwargs) or Client()
                    ),
                    image_open=lambda _stream: Image(),
                )
        self.assertEqual(result["outputs"][0]["text"], "语义")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0],
            {
                "backend": "http-client",
                "server_url": "http://gpu",
                "image_analysis": True,
                "max_concurrency": 3,
                "use_tqdm": False,
            },
        )


class BuildPublishClosureTests(unittest.TestCase):
    def test_unresolved_bundle_is_rejected_by_build_and_publish(self) -> None:
        class Paths:
            def __init__(self, root: Path) -> None:
                self.root = root

            def data_path(self, relpath: Path) -> Path:
                return self.root / relpath

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relpath = Path("normalized_ir.v4.json")
            normalized = write_text_ir_bundle(
                root,
                relpath,
                image=True,
                image_provider_text=None,
            )
            files = normalized["parser_artifacts"]["files"]

            def load(role: str) -> VerifiedParserArtifact:
                descriptor = files[role]
                payload = (root / descriptor["relpath"]).read_bytes()
                return VerifiedParserArtifact(payload, _sha(payload))

            with self.assertRaises(SourceEvidenceValidationError):
                MinerUSourceEvidenceValidator().validate(
                    normalized,
                    load_artifact=load,
                )
            raw = (root / relpath).read_bytes()
            run = e.ProcessingRun(
                processing_run_id="run_1",
                document_id="doc_1",
                artifact_owner_processing_run_id="run_1",
                run_kind="parse",
                status="succeeded",
                unit_build_status="succeeded",
                parser_target_identity=normalized["parser"],
                normalized_ir_relpath=str(relpath),
                artifact_hash=_sha(raw),
            )
            with self.assertRaises(PublishRunError) as raised:
                NormalizedIRPublicationGuard(Paths(root))(run)
        self.assertEqual(
            raised.exception.error["error_code"],
            "VISUAL_SEMANTIC_CLOSURE_UNRESOLVED",
        )


if __name__ == "__main__":
    unittest.main()
