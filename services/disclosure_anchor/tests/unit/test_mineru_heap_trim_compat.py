"""Exact-source regressions for the MinerU 3.4.4 heap-return image patch."""

from __future__ import annotations

from pathlib import Path
import unittest

from scripts.windows.mineru_heap_trim_compat.patch_mineru_344 import (
    BASE_IMAGE_DIGEST,
    TARGET_PREIMAGE_SHA256,
    patch_source,
)


class MinerUHeapTrimCompatibilityTests(unittest.TestCase):
    def test_preimages_match_the_reproduced_deployed_344_sources(self) -> None:
        self.assertEqual(
            TARGET_PREIMAGE_SHA256,
            {
                "mineru/backend/vlm/vlm_analyze.py": (
                    "0fadf7a94ae702861b4a1fa7f42358c6687cfc63fbe322c004fb1d3248658390"
                ),
                "mineru/backend/hybrid/hybrid_analyze.py": (
                    "404ce6552e9d7374b96de798d2d0f7d72927eef9485668e79c82c5002b36adb0"
                ),
                "mineru/utils/model_utils.py": (
                    "7662656c5c406ab704065b8a3a6e662b662b0bb877b76b08c7d8a8a7eaf9c109"
                ),
            },
        )

    def test_model_utils_hook_is_explicit_guarded_and_fail_visible(self) -> None:
        source = (
            "import math\nimport os\nimport time\nimport gc\n"
            "\ndef clean_memory(device='cuda'):\n    gc.collect()\n"
        )
        patched = patch_source("mineru/utils/model_utils.py", source)

        self.assertIn("MINERU_MALLOC_TRIM must be explicitly configured", patched)
        self.assertIn("if not is_heap_trim_enabled():", patched)
        self.assertIn("raise RuntimeError(\"glibc malloc_trim is unavailable\")", patched)
        self.assertNotIn("except Exception", patched)
        with self.assertRaisesRegex(RuntimeError, "anchor count drifted"):
            patch_source("mineru/utils/model_utils.py", patched)

    def test_vlm_and_hybrid_trim_every_window_and_document(self) -> None:
        vlm = (
            "from ...utils.config_reader import get_device, get_processing_window_size\n\n"
            "from ...utils.enum_class import ImageType\n"
            + "                finally:\n                    _close_images(images_list)\n"
            * 2
            + "        doc_closed = True\n        return middle_json, results\n" * 2
        )
        hybrid = (
            "from mineru.utils.model_utils import clean_memory, crop_img, get_vram\n"
            + "                finally:\n                    _close_images(images_list)\n"
            * 2
            + "        clean_memory(device)\n        return middle_json, model_list\n"
            * 2
        )

        patched_vlm = patch_source("mineru/backend/vlm/vlm_analyze.py", vlm)
        patched_hybrid = patch_source(
            "mineru/backend/hybrid/hybrid_analyze.py", hybrid
        )

        self.assertEqual(patched_vlm.count("trim_process_heap()"), 4)
        self.assertEqual(patched_hybrid.count("trim_process_heap()"), 4)

    def test_dockerfile_pins_base_and_enables_the_closed_policy(self) -> None:
        dockerfile = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "windows"
            / "mineru_heap_trim_compat"
            / "Dockerfile"
        ).read_text(encoding="utf-8")

        self.assertIn(f"FROM mineru@{BASE_IMAGE_DIGEST}", dockerfile)
        self.assertIn("ENV MINERU_MALLOC_TRIM=1", dockerfile)
        self.assertIn("COMPAT_PATCHER_SHA256", dockerfile)
        self.assertIn("COMPAT_DOCKERFILE_SHA256", dockerfile)
        self.assertNotIn("latest", dockerfile.lower())

    def test_windows_installer_and_collector_bind_the_derived_image(self) -> None:
        root = Path(__file__).resolve().parents[2]
        installer = (
            root / "scripts" / "windows" / "install_mineru_fixed_api.ps1"
        ).read_text(encoding="utf-8")
        collector = (
            root / "scripts" / "windows" / "collect_mineru_runtime.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("[Parameter(Mandatory = $true)][string]$CompatDockerfileSource", installer)
        self.assertIn("[Parameter(Mandatory = $true)][string]$CompatPatcherSource", installer)
        self.assertIn('"build", "--pull=false"', installer)
        self.assertIn('"--tag", $ApiCompatBuildTag', installer)
        self.assertIn("$OldApiCompatImageId = Get-OptionalImageId", installer)
        self.assertIn('$ErrorActionPreference = "SilentlyContinue"', installer)
        self.assertIn("$inspectExitCode = $LASTEXITCODE", installer)
        self.assertIn(
            "$ErrorActionPreference = $previousErrorActionPreference",
            installer,
        )
        self.assertIn("function Restore-ApiCompatTag", installer)
        self.assertIn('"tag", $OldApiCompatImageId, $ApiCompatImage', installer)
        self.assertIn("Remove-CompatBuildTag", installer)
        self.assertLess(
            installer.index("$MutationStarted = $true"),
            installer.index('"tag", $ExpectedApiCompatImageId, $ApiCompatImage'),
        )
        self.assertIn('schema = "mineru-windows-install-receipt.v2"', installer)
        self.assertIn("mineru-runtime-v5", installer)
        self.assertNotIn("versioned v4 evidence paths", installer)
        self.assertIn('schema = "mineru-windows-runtime-observation.v3"', collector)
        self.assertIn("actual_source_sha256", collector)
        self.assertIn("heap_trim_enabled", collector)


if __name__ == "__main__":
    unittest.main()
