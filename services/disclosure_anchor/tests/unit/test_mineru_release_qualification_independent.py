"""Independent qualification composition checks: child environment and receipt gates."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_release_qualification as qualification
from disclosure_anchor.adapters.runtime.resident_owner_control import OwnerCommandResult
from disclosure_anchor.application.contracts.mineru_capacity_config import MineruCapacityConfig
from tests._mineru_capacity_config_fixture import capacity_payload


class ReleaseQualificationIndependentTests(unittest.TestCase):
    def exercise(self, root, *, ambient_window=None, fault=None):
        capacity = MineruCapacityConfig(**capacity_payload(processing_window_size=16))
        bundle = root / "bundle.json"
        identity = "sha256:" + "1" * 64
        bundle.write_text(json.dumps({"identity_sha256": identity,
                                     "manifest": {"orchestrator": {"capacity_config_sha256": capacity.sha256}}}))
        package = root / "package"
        (package / "inputs").mkdir(parents=True)
        (package / "inputs" / "capacity-config.json").write_bytes(capacity.exact_bytes)
        documents = tuple(qualification.HeldoutDocument(label, root / (label + ".pdf"), "sha256:" + "2" * 64, pages)
                          for label, pages in (("long", 193), ("short", 11)))
        canary = qualification.CanaryManifest(root / "fixture.pdf", documents, "sha256:" + "3" * 64)
        report = SimpleNamespace(passed=True, inputs=SimpleNamespace(capacity=capacity),
                                 manifest=SimpleNamespace(sha256="sha256:" + "4" * 64))
        binding = SimpleNamespace(mineru_bin=root / "mineru", api_url="http://localhost:30002",
                                  observability_url="http://localhost:30001/v1", inference_upstream_url="http://engine:30000/v1",
                                  ssh_host="192.0.2.1", ssh=SimpleNamespace(username="test", port=22,
                                  private_key_path="/unused/key", known_hosts_path="/unused/known_hosts"))
        calls = []

        class Child:
            def __init__(child, argv, **options):
                env = options["environment"]
                self.assertEqual(env["MINERU_PROCESSING_WINDOW_SIZE"], "16")
                self.assertNotIn("DATABASE_URL", env)
                self.assertNotIn("DISCLOSURE_MIGRATION_DATABASE_URL", env)
                self.assertNotIn("PYTHONOPTIMIZE", env)
                receipt = Path(argv[argv.index("--receipt-out") + 1])
                calls.append(receipt.name)
                status = 13 if fault == "child_failure" else 0
                body = {"status": "pass"}
                if Path(argv[2]).name == "mineru_smoke.py":
                    body["identity"] = {"runtime_manifest_identity_sha256": identity if fault != "wrong_runtime" else "sha256:" + "9" * 64}
                    name = Path(argv[argv.index("--input") + 1]).stem
                    body["input"] = {"page_count": {"fixture": 1, "long": 193, "short": 11}[name]}
                    if fault == "wrong_pages" and name == "long":
                        body["input"]["page_count"] = 192
                receipt.write_text(json.dumps(body))
                child.result = OwnerCommandResult(status, b"declared executor output", b"declared executor error" if status else b"")

            def finish(child):
                return child.result

            def abort(child):
                return child.result

        ambient = {"DATABASE_URL": "must-not-reach-provider", "DISCLOSURE_MIGRATION_DATABASE_URL": "must-not-reach-provider", "PYTHONOPTIMIZE": "1"}
        if ambient_window is not None:
            ambient["MINERU_PROCESSING_WINDOW_SIZE"] = ambient_window
        with patch.dict(os.environ, ambient, clear=True), patch.object(qualification, "BoundedOwnerCommand", Child):
            if fault:
                with self.assertRaises(qualification.ReleaseIdentityError):
                    qualification.qualify_release(report=report, runtime_bundle=bundle, canary=canary,
                                                  binding=binding, package=package, output=root / "out")
                self.assertFalse((root / "out" / "qualification.json").exists())
            else:
                result = qualification.qualify_release(report=report, runtime_bundle=bundle, canary=canary,
                                                       binding=binding, package=package, output=root / "out")
                self.assertEqual(result["status"], "pass")
                self.assertEqual(result["database_access"], "none")
                self.assertEqual(result["heldout"], [{"label": "long", "source_sha256": "sha256:" + "2" * 64, "expected_pages": 193},
                                                    {"label": "short", "source_sha256": "sha256:" + "2" * 64, "expected_pages": 11}])
        return calls

    def test_entry_supplies_release_window_with_empty_or_conflicting_shell(self):
        for ambient in (None, "999"):
            with self.subTest(ambient=ambient), tempfile.TemporaryDirectory() as directory:
                self.assertEqual(self.exercise(Path(directory), ambient_window=ambient),
                                 ["smoke-fixture.v6.json", "epoch-before.json", "long.v6.json", "short.v6.json", "epoch-after.json", "validation-final.json"])

    def test_child_failure_and_identity_or_page_drift_never_emit_qualification(self):
        for fault, expected in (("child_failure", 1), ("wrong_runtime", 1), ("wrong_pages", 3)):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                calls = self.exercise(Path(directory), fault=fault)
                self.assertEqual(len(calls), expected)

    def test_environment_override_reaches_the_real_owned_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = "import os; print(os.environ['MINERU_PROCESSING_WINDOW_SIZE']); assert 'DATABASE_URL' not in os.environ"
            env = {"MINERU_PROCESSING_WINDOW_SIZE": "16"}
            result = qualification.run_step(root, "environment", [sys.executable, "-c", script],
                                            timeout_seconds=10, environment=env)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual((root / "environment-stdout.raw").read_bytes(), b"16\n")


if __name__ == "__main__":
    unittest.main()
