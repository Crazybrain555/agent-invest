from __future__ import annotations

import unittest

from disclosure_anchor.application.contracts.staged_resource_paths import (
    staged_materialization_relpaths,
    staged_retained_relpaths,
    staged_snapshot_relpaths,
    validate_relative_resource_path_v4,
)


SHA = "sha256:" + "a" * 64


class StagedResourcePathTests(unittest.TestCase):
    def test_paths_are_stage_specific_canonical_and_lock_is_shared_metadata(self) -> None:
        snapshot = staged_snapshot_relpaths(
            attempt_id="attempt-1", fence_identity="fence-1",
            source_pdf_sha256=SHA,
        )
        self.assertRegex(snapshot["snapshot"], r"spool/\.upload-[0-9a-f]{64}\.pdf$")
        self.assertTrue(snapshot["snapshot_part"].endswith(".pdf.part"))
        self.assertTrue(
            snapshot["snapshot_part_owner"].endswith(".pdf.part.owner.json")
        )
        self.assertRegex(snapshot["snapshot_lock"], r"spool/\.upload-[0-9a-f]{64}\.lock$")
        retained = staged_retained_relpaths(
            attempt_id="attempt-1", fence_identity="fence-1",
            artifact_owner_identity="owner-1", artifact_sha256=SHA,
        )
        self.assertTrue(retained["spool_part_owner"].endswith(".part.owner.json"))
        materialization = staged_materialization_relpaths(
            output_dir_name="run-output", attempt_id="attempt-1",
            fence_identity="fence-1", artifact_sha256=SHA,
        )
        self.assertEqual(
            materialization["staging_lock"],
            "spool/.materialization-locks/.run-output.lock",
        )
        self.assertNotEqual(materialization["staging"], materialization["output"])

    def test_hash_prefix_is_not_part_of_the_digest_identity(self) -> None:
        first = staged_retained_relpaths(
            attempt_id="a", fence_identity="f", artifact_owner_identity="o",
            artifact_sha256=SHA,
        )
        second = staged_retained_relpaths(
            attempt_id="a", fence_identity="f", artifact_owner_identity="o",
            artifact_sha256=SHA,
        )
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValueError, "canonical"):
            staged_retained_relpaths(
                attempt_id="a", fence_identity="f", artifact_owner_identity="o",
                artifact_sha256="a" * 64,
            )
        for unsafe in ("../escape", "C:drive", "nested/name", "nested\\name"):
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(
                ValueError, "unsafe"
            ):
                staged_materialization_relpaths(
                    output_dir_name=unsafe,
                    attempt_id="a",
                    fence_identity="f",
                    artifact_sha256=SHA,
                )

    def test_shared_relative_path_validator_is_closed_and_canonical(self) -> None:
        validate_relative_resource_path_v4("artifacts/run-1/output", "artifact")
        validate_relative_resource_path_v4("artifacts/café/output", "artifact")
        for unsafe in (
            "C:/x",
            "a/./b",
            "a//b",
            "a/../b",
            "a:b",
            " leading",
            "trailing ",
            "artifacts/cafe\u0301/output",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(
                ValueError, "path"
            ):
                validate_relative_resource_path_v4(unsafe, "artifact")

        with self.assertRaisesRegex(ValueError, "unsafe"):
            staged_materialization_relpaths(
                output_dir_name="cafe\u0301",
                attempt_id="a",
                fence_identity="f",
                artifact_sha256=SHA,
            )


if __name__ == "__main__":
    unittest.main()
