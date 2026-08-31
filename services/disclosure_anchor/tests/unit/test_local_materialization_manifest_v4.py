from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import unittest

from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
    LocalMaterializationManifestV4,
    LocalMaterializationObservationsV4,
    LocalMaterializationPayloadFileV4,
    decode_local_materialization_manifest_v4,
    seal_local_materialization_manifest_v4,
)


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
SHA_E = "sha256:" + "e" * 64
SHA_F = "sha256:" + "f" * 64


class LocalMaterializationManifestV4Tests(unittest.TestCase):
    def test_seal_is_canonical_closed_and_portable(self) -> None:
        value = _manifest()
        self.assertEqual(
            decode_local_materialization_manifest_v4(value.canonical_bytes), value
        )
        self.assertEqual(
            value.payload_files,
            tuple(sorted(value.payload_files, key=lambda item: item.relpath)),
        )
        envelope = tuple(
            item for item in value.payload_files if item.role == "provider_envelope"
        )
        self.assertEqual(len(envelope), 1)
        self.assertEqual(envelope[0].relpath, value.provider_envelope_relpath)
        self.assertTrue(
            any(item.role == "parser_artifact" for item in value.payload_files)
        )
        self.assertNotIn(b"claim", value.canonical_bytes)
        self.assertNotIn(b"timestamp", value.canonical_bytes)
        self.assertNotIn(b"/Users/", value.canonical_bytes)
        self.assertNotIn(
            LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME.encode(),
            tuple(item.relpath.encode() for item in value.payload_files),
        )
        self.assertEqual(
            value.sha256,
            "sha256:" + hashlib.sha256(value.canonical_bytes).hexdigest(),
        )

    def test_decoder_rejects_unknown_noncanonical_and_duplicate_fields(self) -> None:
        exact = _manifest().canonical_bytes
        payload = json.loads(exact)
        payload["unknown"] = "drift"
        with self.assertRaisesRegex(ValueError, "fields are not closed"):
            decode_local_materialization_manifest_v4(_canonical(payload))

        payload = json.loads(exact)
        payload["observations"]["unknown"] = 1
        with self.assertRaisesRegex(ValueError, "fields are not closed"):
            decode_local_materialization_manifest_v4(_canonical(payload))

        noncanonical = json.dumps(
            json.loads(exact), ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "JSON is not canonical"):
            decode_local_materialization_manifest_v4(noncanonical)

        duplicate = b'{"schema":"local-materialization-manifest.v4",' + exact[1:]
        with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
            decode_local_materialization_manifest_v4(duplicate)

    def test_payload_paths_are_safe_sorted_unique_and_not_management_files(self) -> None:
        value = _manifest()
        parser = value.payload_files[1]
        for unsafe in (
            "/absolute/content.md",
            "../escape.md",
            "nested/../../escape.md",
            "C:/windows/path.md",
            "result/cafe\u0301.md",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(ValueError, "path"):
                    replace(
                        value,
                        payload_files=(
                            value.payload_files[0],
                            replace(parser, relpath=unsafe),
                        ),
                    )

        with self.assertRaisesRegex(ValueError, "canonically ordered"):
            replace(value, payload_files=tuple(reversed(value.payload_files)))

        with self.assertRaisesRegex(ValueError, "duplicate paths"):
            duplicate_paths = (
                value.payload_files[0],
                replace(parser, relpath=value.payload_files[0].relpath.upper()),
            )
            replace(
                value,
                payload_files=tuple(
                    sorted(duplicate_paths, key=lambda item: item.relpath)
                ),
            )

        for management_name in (
            LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
            ".agent-materialization-inflight.v1.json",
            ".agent-materialization-inflight.v4.json",
        ):
            for alias in (management_name, management_name.upper()):
                with self.subTest(management_name=alias):
                    with self.assertRaisesRegex(ValueError, "cannot be payload"):
                        replace(parser, relpath=f"nested/{alias}")

        ancestor = replace(parser, relpath="result")
        descendant = replace(
            value.payload_files[0],
            role="parser_artifact",
            relpath="RESULT/content.md",
        )
        envelope = replace(
            value.payload_files[0],
            relpath="provider_document.v1.json",
        )
        conflicting = tuple(
            sorted((ancestor, descendant, envelope), key=lambda item: item.relpath)
        )
        with self.assertRaisesRegex(ValueError, "ancestor path conflicts"):
            replace(
                value,
                payload_files=conflicting,
                observations=replace(
                    value.observations,
                    output_file_count=len(conflicting),
                    output_byte_count=sum(item.byte_count for item in conflicting),
                ),
            )

    def test_envelope_and_observation_hash_closure_rejects_drift(self) -> None:
        value = _manifest()
        wrong_envelope_name = "provider-document.v1.json"
        with self.assertRaisesRegex(ValueError, "evidence triple drifted"):
            replace(
                value,
                provider_envelope_relpath=wrong_envelope_name,
                payload_files=tuple(
                    replace(item, relpath=wrong_envelope_name)
                    if item.role == "provider_envelope"
                    else item
                    for item in value.payload_files
                ),
            )
        with self.assertRaisesRegex(ValueError, "evidence triple drifted"):
            replace(value, provider_envelope_sha256=SHA_F)
        with self.assertRaisesRegex(ValueError, "evidence triple drifted"):
            replace(value, provider_envelope_byte_count=8)
        with self.assertRaisesRegex(ValueError, "one provider envelope"):
            replace(
                value,
                payload_files=tuple(
                    replace(item, role="parser_artifact")
                    for item in value.payload_files
                ),
            )
        with self.assertRaisesRegex(ValueError, "lacks parser artifacts"):
            replace(
                value,
                payload_files=tuple(
                    replace(item, role="provider_envelope")
                    for item in value.payload_files[:1]
                ),
                observations=replace(
                    value.observations,
                    output_file_count=1,
                    output_byte_count=value.payload_files[0].byte_count,
                ),
            )
        with self.assertRaisesRegex(ValueError, "observations do not close"):
            replace(
                value,
                observations=replace(value.observations, output_byte_count=999),
            )


def _manifest() -> LocalMaterializationManifestV4:
    payload_files = (
        LocalMaterializationPayloadFileV4(
            role="provider_envelope",
            relpath="provider_document.v1.json",
            sha256=SHA_E,
            byte_count=7,
        ),
        LocalMaterializationPayloadFileV4(
            role="parser_artifact",
            relpath="result/content.md",
            sha256=SHA_F,
            byte_count=11,
        ),
    )
    return seal_local_materialization_manifest_v4(
        attempt_id="attempt-1",
        fence_identity="fence-1",
        document_id="document-1",
        processing_run_id="run-1",
        materialization_intent_sha256=SHA_A,
        terminal_receipt_sha256=SHA_B,
        remote_task_identity="task-1",
        artifact_owner_identity="owner-1",
        artifact_sha256=SHA_C,
        artifact_byte_count=20,
        source_pdf_sha256=SHA_D,
        source_page_count=2,
        parser_target_sha256=SHA_E,
        spool_relpath="spool/retained.zip",
        output_relpath="materialization/output-1",
        provider_envelope_relpath="provider_document.v1.json",
        provider_envelope_sha256=SHA_E,
        provider_envelope_byte_count=7,
        observations=LocalMaterializationObservationsV4(
            member_count=2,
            uncompressed_byte_count=30,
            decoded_byte_count=22,
            temporary_disk_peak_byte_count=50,
            output_file_count=2,
            output_byte_count=18,
        ),
        payload_files=payload_files,
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
