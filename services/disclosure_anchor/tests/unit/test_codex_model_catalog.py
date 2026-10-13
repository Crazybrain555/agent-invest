from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from disclosure_anchor.adapters.semantics.codex_model_catalog import (
    NEUTRAL_TOOL_FIELDS,
    CodexModelCatalog,
    codex_model_catalog_path,
    load_codex_model_catalog,
    neutralize_bundled_catalog,
)
from tests.unit._codex_model_catalog_fixture import (
    BUNDLED_TOOL_FIELDS,
    bundled_catalog_bytes,
    bundled_entry,
    neutral_catalog,
    write_prepared_catalog,
)


SERVICE_ROOT = Path(__file__).resolve().parents[2]
PREPARE_SCRIPT = SERVICE_ROOT / "scripts" / "prepare_codex_semantic_model_catalog.py"


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class CodexModelCatalogTransformTests(unittest.TestCase):
    def test_neutralization_changes_only_the_five_tool_fields_deterministically(self) -> None:
        bundled = bundled_catalog_bytes("gpt-5.6-terra", "gpt-6-luna", "gpt-5.6-luna")
        raw = neutralize_bundled_catalog(bundled, model="gpt-6-luna")

        catalog = json.loads(raw)
        self.assertEqual(set(catalog), {"models"})
        self.assertEqual([item["slug"] for item in catalog["models"]], ["gpt-6-luna"])
        entry = catalog["models"][0]
        original = bundled_entry("gpt-6-luna")
        self.assertEqual(set(entry), set(original))
        for key, value in original.items():
            if key in NEUTRAL_TOOL_FIELDS:
                self.assertEqual(entry[key], NEUTRAL_TOOL_FIELDS[key], key)
                self.assertNotEqual(value, NEUTRAL_TOOL_FIELDS[key], key)
            else:
                self.assertEqual(entry[key], value, key)
        self.assertEqual(set(NEUTRAL_TOOL_FIELDS), set(BUNDLED_TOOL_FIELDS))

        # Canonical bytes: vendor key order and whitespace never change identity.
        reordered = json.dumps(
            {"models": [dict(reversed(list(json.loads(bundled)["models"][1].items())))]},
            indent=2,
        ).encode("utf-8")
        self.assertEqual(neutralize_bundled_catalog(reordered, model="gpt-6-luna"), raw)
        self.assertEqual(neutralize_bundled_catalog(bundled, model="gpt-6-luna"), raw)
        self.assertEqual(
            raw,
            json.dumps(
                catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"),
        )

    def test_neutralization_refuses_ambiguous_or_changed_vendor_metadata(self) -> None:
        missing_field_cases = []
        for field in NEUTRAL_TOOL_FIELDS:
            entry = bundled_entry("gpt-6-luna")
            del entry[field]
            missing_field_cases.append(
                (f"missing {field}", json.dumps({"models": [entry]}).encode("utf-8"))
            )
        entry = json.dumps(bundled_entry("gpt-6-luna"))
        cases = (
            ("absent model", bundled_catalog_bytes("gpt-5.6-luna")),
            ("duplicate model", bundled_catalog_bytes("gpt-6-luna", "gpt-6-luna")),
            *missing_field_cases,
            (
                "duplicate key",
                ('{"models":[' + entry[:-1] + ',"slug":"gpt-6-luna"}]}').encode("utf-8"),
            ),
            ("NaN constant", ('{"models":[' + entry[:-1] + ',"x":NaN}]}').encode("utf-8")),
            ("extra top-level key", ('{"models":[' + entry + '],"etag":"x"}').encode("utf-8")),
            ("models not a list", b'{"models":{}}'),
            ("entry not an object", b'{"models":["gpt-6-luna"]}'),
            ("not JSON", b"codex-cli 0.156.1"),
            ("not UTF-8", b"\xff\xfe"),
            ("empty", b""),
        )
        for label, bundled in cases:
            with self.subTest(label), self.assertRaises(ValueError):
                neutralize_bundled_catalog(bundled, model="gpt-6-luna")

    def test_catalog_identity_rejects_hash_model_or_tool_surface_drift(self) -> None:
        catalog = neutral_catalog("gpt-6-luna")
        entry = json.loads(catalog.raw)["models"][0]

        def encoded(value: object) -> bytes:
            return json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")

        drifted = []
        for field, value in (
            *BUNDLED_TOOL_FIELDS.items(),
            ("supports_search_tool", 0),
            ("apply_patch_tool_type", "null"),
            ("experimental_supported_tools", None),
        ):
            changed = dict(entry, **{field: value})
            drifted.append((f"{field}={value!r}", encoded({"models": [changed]}), "gpt-6-luna"))
        missing = dict(entry)
        del missing["tool_mode"]
        drifted.extend(
            (
                ("tool field absent", encoded({"models": [missing]}), "gpt-6-luna"),
                ("other model", catalog.raw, "gpt-5.6-luna"),
                ("two models", encoded({"models": [entry, dict(entry, slug="x")]}), "gpt-6-luna"),
                ("extra top-level key", encoded({"models": [entry], "etag": "x"}), "gpt-6-luna"),
            )
        )
        for label, raw, model in drifted:
            with self.subTest(label), self.assertRaises(ValueError):
                CodexModelCatalog(model=model, sha256=_sha256(raw), raw=raw)
        with self.assertRaises(ValueError):
            CodexModelCatalog(
                model="gpt-6-luna", sha256="sha256:" + "0" * 64, raw=catalog.raw
            )
        self.assertEqual(
            CodexModelCatalog(model="gpt-6-luna", sha256=catalog.sha256, raw=catalog.raw),
            catalog,
        )

    def test_loader_accepts_only_the_prepared_regular_file_for_its_hash(self) -> None:
        catalog = neutral_catalog("gpt-6-luna")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = codex_model_catalog_path(root, catalog.sha256)
            self.assertEqual(
                path,
                root / "semantic" / "codex_model_catalogs" / f"{catalog.sha256[7:]}.json",
            )
            with self.assertRaisesRegex(ValueError, "missing or unreadable"):
                load_codex_model_catalog(root, sha256=catalog.sha256, model="gpt-6-luna")

            write_prepared_catalog(root, catalog)
            loaded = load_codex_model_catalog(
                root, sha256=catalog.sha256, model="gpt-6-luna"
            )
            self.assertEqual(loaded, catalog)
            for sha256, model in (
                ("sha256:" + "0" * 64, "gpt-6-luna"),
                (catalog.sha256, "gpt-5.6-luna"),
                (catalog.sha256.upper(), "gpt-6-luna"),
                (catalog.sha256[7:], "gpt-6-luna"),
                ("sha256:../../" + "a" * 58, "gpt-6-luna"),
            ):
                with self.subTest(sha256=sha256, model=model), self.assertRaises(ValueError):
                    load_codex_model_catalog(root, sha256=sha256, model=model)

            path.write_bytes(catalog.raw + b"\n")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                load_codex_model_catalog(root, sha256=catalog.sha256, model="gpt-6-luna")

            elsewhere = root / "elsewhere.json"
            elsewhere.write_bytes(catalog.raw)
            path.unlink()
            path.symlink_to(elsewhere)
            with self.assertRaises(ValueError):
                load_codex_model_catalog(root, sha256=catalog.sha256, model="gpt-6-luna")
            path.unlink()
            path.mkdir()
            with self.assertRaises(ValueError):
                load_codex_model_catalog(root, sha256=catalog.sha256, model="gpt-6-luna")


class PrepareCodexModelCatalogScriptTests(unittest.TestCase):
    def test_script_writes_one_content_addressed_catalog_from_the_pinned_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundled_path = root / "bundled.json"
            bundled = bundled_catalog_bytes("gpt-5.6-luna", "gpt-6-luna")
            bundled_path.write_bytes(bundled)
            observed_env = root / "observed-env.txt"
            codex = root / "bin" / "codex"
            codex.parent.mkdir()
            codex.write_text(
                "#!/bin/sh\n"
                f'env >> "{observed_env}"\n'
                'if [ "$1" = "--version" ]; then echo "codex-cli 0.156.1"; exit 0; fi\n'
                'if [ "$1 $2 $3" = "debug models --bundled" ]; then\n'
                f'  cat "{bundled_path}"; exit 0\n'
                "fi\n"
                "exit 64\n",
                encoding="utf-8",
            )
            codex.chmod(0o755)
            runtime_root = root / "runtime"

            def prepare(expect_version: str = "0.156.1") -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        sys.executable,
                        str(PREPARE_SCRIPT),
                        "--codex",
                        str(codex),
                        "--model",
                        "gpt-6-luna",
                        "--expect-version",
                        expect_version,
                        "--runtime-root",
                        str(runtime_root),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env={
                        "PATH": os.defpath,
                        "PYTHONPATH": str(SERVICE_ROOT / "src"),
                        "CODEX_API_KEY": "sentinel-must-not-reach-codex",
                        "OPENAI_API_KEY": "sentinel-must-not-reach-codex",
                    },
                )

            wrong = prepare("0.156.2")
            self.assertNotEqual(wrong.returncode, 0)
            self.assertFalse((runtime_root / "semantic").exists())

            first = prepare()
            self.assertEqual(first.returncode, 0, first.stderr)
            printed = json.loads(first.stdout)
            expected_raw = neutralize_bundled_catalog(bundled, model="gpt-6-luna")
            self.assertEqual(printed["model_catalog_sha256"], _sha256(expected_raw))
            catalog_path = Path(printed["path"])
            self.assertEqual(
                catalog_path,
                codex_model_catalog_path(runtime_root, printed["model_catalog_sha256"]),
            )
            self.assertEqual(catalog_path.read_bytes(), expected_raw)
            self.assertEqual(
                load_codex_model_catalog(
                    runtime_root,
                    sha256=printed["model_catalog_sha256"],
                    model="gpt-6-luna",
                ).raw,
                expected_raw,
            )
            provenance = json.loads(Path(printed["provenance"]).read_text(encoding="utf-8"))
            self.assertEqual(provenance["codex_version"], "codex-cli 0.156.1")
            self.assertEqual(provenance["bundled_sha256"], _sha256(bundled))
            self.assertEqual(provenance["executable_sha256"], _sha256(codex.read_bytes()))
            self.assertEqual(
                provenance["tool_fields"],
                {
                    key: {"before": BUNDLED_TOOL_FIELDS[key], "after": value}
                    for key, value in NEUTRAL_TOOL_FIELDS.items()
                },
            )
            seen = observed_env.read_text(encoding="utf-8")
            self.assertNotIn("sentinel-must-not-reach-codex", seen)
            self.assertNotIn(f"HOME={Path.home()}\n", seen)

            again = prepare()
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(json.loads(again.stdout), printed)

            catalog_path.write_bytes(expected_raw + b" ")
            refused = prepare()
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("refusing overwrite", refused.stderr)
            self.assertEqual(catalog_path.read_bytes(), expected_raw + b" ")


if __name__ == "__main__":
    unittest.main()
