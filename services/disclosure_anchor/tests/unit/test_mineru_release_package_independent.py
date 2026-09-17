"""Black-box source-only release boundary in a disposable committed Git tree.

The current edited implementation is the CLI under test. Its inputs are copied
to an independent local Git repository, so the test does not stage/commit the
developer checkout or borrow an earlier release package.
"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.unit.test_mineru_release_compose_independent import DEPLOYMENT, _wire
from tests.unit.test_mineru_release_projection_independent import _capacity


LOCAL = {
    "contract_version": "mineru.local-worker-profile.v1",
    "mac_preflight_workers": 3, "mac_finalize_workers": 2,
    "provider_poll_milliseconds": 1000, "admission_probe_milliseconds": 1000,
    "commit_stage_seconds": 3600, "archive_member_count_limit": 100000,
    "stream_ceiling": 7,
    "stream_source_ages": {"api_max_age_seconds": 3.0, "gpu_max_age_seconds": 8.0},
    "stream_policy": {
        "gpu_pause_bytes": 536870912, "gpu_reduce_bytes": 1073741824,
        "gpu_recover_bytes": 1610612736, "host_pause_bytes": 4294967296,
        "host_recover_bytes": 6442450944, "sample_max_age_seconds": 8.0,
        "missing_pause_seconds": 10.0, "recovery_seconds": 10.0,
        "reduction_interval_seconds": 2.0,
    },
    "process_ceilings": {
        "source_pdf_bytes_limit": 134217728, "rasterized_page_bytes_limit": 536870912,
        "decoded_payload_bytes_limit": 2147483648, "reorder_buffer_bytes_limit": 536870912,
        "terminal_output_bytes_limit": 2147483648, "temporary_disk_bytes_limit": 8589934592,
        "db_staged_bytes_limit": 536870912, "gpu_allocated_bytes_limit": 17094934528,
        "resident_pages_limit": 16, "unpublished_pages_limit": 4096, "cpu_worker_threads": 3,
        "raster_stage_slots": 1, "layout_stage_slots": 1, "postprocess_stage_slots": 1,
        "native_owner_slots": 1, "hybrid_ocr_override": False,
    },
}
NATIVE_SOURCES = {
    "mineru_m6_owner_binding.cs", "mineru_m6_owner_endpoint.cs", "mineru_m6_owner_host.cs",
    "mineru_m6_owner_identity.cs", "mineru_m6_owner_journal.cs", "mineru_m6_owner_platform.cs",
    "mineru_m6_owner_wire.cs", "mineru_m6_private_store.cs", "mineru_m6_run_control.cs",
    "mineru_m6_writer_guard.cs", "mineru_nvml_backend.cs", "mineru_resident_wire.cs",
}


class MineruReleasePackageIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = Path(__file__).resolve().parents[2]
        cls.temporary = tempfile.TemporaryDirectory(prefix="release-independent-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.repo = cls.root / "source repo"
        cls.source = cls.repo / "services" / "disclosure_anchor"
        cls.source.mkdir(parents=True)
        # Preserve the project's Git text/eol contract: PS1 worktree bytes are
        # CRLF while their committed blobs are LF. No global Git configuration.
        shutil.copyfile(cls.service / ".gitattributes", cls.source / ".gitattributes")
        # Snapshot source directories, including new uncommitted product files;
        # do not copy runtime, private config, .venv or any previous package.
        for directory in ("src", "scripts", "config"):
            for original in (cls.service / directory).rglob("*"):
                if not original.is_file() or original.is_symlink():
                    continue
                relative = original.relative_to(cls.service)
                if "__pycache__" in relative.parts or original.suffix in (".pyc", ".dll", ".exe"):
                    continue
                target = cls.source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, target)
        (cls.repo / ".worktreeinclude").write_text("independent baseline\n")
        cls.git("init", "-q")
        cls.git("add", ".")
        cls.git("-c", "user.name=Independent Test", "-c", "user.email=independent@example.invalid",
                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                "commit", "-qm", "independent release source fixture")
        cls.head = cls.git("rev-parse", "HEAD").strip()
        cls.env = {**os.environ, "PYTHONPATH": str(cls.source / "src"),
                   "PYTHONDONTWRITEBYTECODE": "1"}
        for name in ("DATABASE_URL", "DISCLOSURE_MIGRATION_DATABASE_URL"):
            cls.env.pop(name, None)

    @classmethod
    def git(cls, *args):
        return subprocess.run(["git", *args], cwd=cls.repo, text=True, capture_output=True,
                              check=True, timeout=30).stdout

    def setUp(self):
        self.case = Path(tempfile.mkdtemp(prefix="case-", dir=self.root))
        self.capacity = self.case / "capacity.json"
        self.capacity.write_bytes(_capacity().exact_bytes)
        self.deployment = self.case / "deployment.json"
        self.deployment.write_bytes(_wire(DEPLOYMENT))
        self.local = self.case / "local.json"
        self.local.write_bytes(_wire(LOCAL))
        self.package = self.case / "new release"

    def cli(self, *arguments, expected=0):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "disclosure_anchor.cli.mineru_release", *map(str, arguments)],
            cwd=self.case, env=self.env, text=True, capture_output=True, timeout=45,
        )
        self.assertEqual(result.returncode, expected, result.stdout + "\n" + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "pass" if expected == 0 else "fail")
        return value

    def build(self, expected=0):
        return self.cli("build", "--source-root", self.repo, "--source-head", self.head,
                        "--capacity", self.capacity, "--deployment-profile", self.deployment,
                        "--local-profile", self.local, "--out", self.package, expected=expected)

    def test_fresh_source_build_verifies_without_previous_package_and_excludes_tests(self):
        self.build()
        receipt = self.cli("verify", "--package", self.package)
        self.assertEqual(receipt["source_head"], self.head)
        self.assertEqual(receipt["projection_mismatches"], [])
        self.assertEqual(receipt["implicit_external_reads"], [])
        manifest = json.loads((self.package / "release-manifest.json").read_bytes())
        self.assertEqual(manifest["api_build"]["build_target"], "explicit-capacity")
        self.assertEqual(manifest["api_build"]["capacity_config_sha256"], _capacity().sha256)
        self.assertEqual(set(manifest["native_m6"]["sources"]), NATIVE_SOURCES)
        self.assertFalse(manifest["projection"]["local_profile_admits_full_pending"])
        self.assertEqual((self.package / "inputs/capacity-config.json").read_bytes(), self.capacity.read_bytes())
        self.assertEqual((self.package / "api-context/agent_capacity_config.py").read_bytes(),
                         (self.source / "src/disclosure_anchor/application/contracts/mineru_capacity_config.py").read_bytes())
        for path in self.package.rglob("*"):
            if path.is_file():
                self.assertFalse(path.name.startswith("test_"), path)
                self.assertNotIn(path.suffix, (".dll", ".exe", ".pyc"))
        for item in manifest["files"]:
            raw = (self.package / item["path"]).read_bytes()
            self.assertEqual(item["sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
            self.assertEqual(item["bytes"], len(raw))

    def test_in_scope_source_drift_is_rejected_before_package_creation(self):
        source = self.source / "src/disclosure_anchor/application/contracts/mineru_capacity_config.py"
        original = source.read_bytes()
        try:
            source.write_bytes(original + b"\n# independently injected drift\n")
            self.build(expected=65)
            self.assertFalse(self.package.exists())
        finally:
            source.write_bytes(original)

    def test_unrelated_user_change_is_not_silently_copied_or_a_build_blocker(self):
        user = self.repo / ".worktreeinclude"
        original = user.read_bytes()
        try:
            user.write_text("independent user edit must stay here\n")
            self.build()
            self.cli("verify", "--package", self.package)
            self.assertEqual(user.read_text(), "independent user edit must stay here\n")
            self.assertFalse((self.package / ".worktreeinclude").exists())
        finally:
            user.write_bytes(original)

    def test_modified_release_generator_cannot_claim_an_older_source_head(self):
        source = self.source / "src/disclosure_anchor/application/services/mineru_release_plan.py"
        original = source.read_bytes()
        try:
            source.write_bytes(original + b"\n# generator drift outside copied API inputs\n")
            self.build(expected=65)
            self.assertFalse(self.package.exists())
        finally:
            source.write_bytes(original)

    def test_untracked_source_in_a_release_directory_is_rejected(self):
        source = self.source / "scripts/windows/mineru_heap_trim_compat/untracked-helper.py"
        try:
            source.write_text("# unreviewed product helper\n")
            self.build(expected=65)
            self.assertFalse(self.package.exists())
        finally:
            source.unlink(missing_ok=True)

    def test_verify_detects_source_tamper_and_does_not_repair_the_release(self):
        self.build()
        source = self.package / "api-context/agent_capacity_config.py"
        tampered = source.read_bytes() + b"\n# tampered\n"
        source.write_bytes(tampered)
        self.cli("verify", "--package", self.package, expected=65)
        self.assertEqual(source.read_bytes(), tampered)

    def test_verify_rejects_internally_inconsistent_git_blob_provenance(self):
        self.build()
        path = self.package / "release-manifest.json"
        manifest = json.loads(path.read_bytes())
        entry = next(item for item in manifest["files"]
                     if item["path"] == "api-context/agent_capacity_config.py")
        entry["provenance"]["blob_sha1"] = "0" * 40
        path.write_bytes(_wire(manifest))
        self.cli("verify", "--package", self.package, expected=65)

    def test_existing_release_is_never_overwritten_by_a_new_build(self):
        self.build()
        manifest = (self.package / "release-manifest.json").read_bytes()
        self.build(expected=64)
        self.assertEqual((self.package / "release-manifest.json").read_bytes(), manifest)

    def test_current_runtime_capability_failure_precedes_package_creation(self):
        value = json.loads(self.capacity.read_bytes())
        value["processing_window_size"] = 32
        self.capacity.write_bytes(_wire(value))
        self.build(expected=65)
        self.assertFalse(self.package.exists())

    def test_input_format_failure_is_structured_without_a_partial_release(self):
        value = deepcopy(DEPLOYMENT)
        value["api_published_port"] = True
        self.deployment.write_bytes(_wire(value))
        self.build(expected=64)
        self.assertFalse(self.package.exists())
