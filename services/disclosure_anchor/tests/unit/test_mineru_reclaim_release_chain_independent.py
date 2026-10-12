"""Independent reclaim release chain using existing disposable package fixtures.

Only the borrowed fixture's temporary source repository uses Git. No developer
checkout, remote service, PowerShell process, torch or GPU is operated.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import PureWindowsPath
import re
import unittest

from disclosure_anchor.adapters.runtime import mineru_release_install as installation
from disclosure_anchor.adapters.runtime import mineru_release_package as packages
from disclosure_anchor.application.contracts import mineru_deployment_profile as profiles
from disclosure_anchor.application.contracts.mineru_release_manifest import decode_mineru_release_manifest
from disclosure_anchor.application.services import mineru_release_plan as plan
from tests import _mineru_capacity_v11_fixture as capacity_fixture
from tests import _mineru_cpu_thread_fixture as legacy_fixture
from tests.unit import test_mineru_release_package_independent as package_fixture
from tests.unit.test_mineru_release_compose_independent import DEPLOYMENT, LEGACY_INFERENCE_ARGV, _wire


MODE = "mineru.owner-memory-reclaim.v1"
OBSERVER = "mineru.owner-memory.v1"
WORKER = "mineru_inference_memory.worker.OwnerMemoryWorker"
MIDDLEWARE = "mineru_inference_memory.reclaim.OwnerMemoryReclaimMiddleware"
CONTEXT = ("Dockerfile", "mineru_inference_memory/__init__.py", "mineru_inference_memory/core.py",
           "mineru_inference_memory/reclaim.py", "mineru_inference_memory/worker.py")


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def profile_value(mode, sources):
    value = deepcopy(DEPLOYMENT)
    value["inference_declared_defaults"].update(
        vllm_gpu_memory_utilization_millionths=500000, vllm_enable_prefix_caching=True)
    if mode is not None:
        value.update(contract_version="mineru.deployment-profile.v2", inference_diagnostic={
            "contract_version": mode, "context_sha256": digest(_wire(sources)), "worker_class": WORKER})
    return value


class IndependentReclaimReleaseChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Keep the existing fixture class's state/cleanup separate. This class
        # is local, so unittest discovery does not also load its inherited tests.
        fixture_type = type("ReclaimSourceFixture", (package_fixture.MineruReleasePackageIndependentTests,), {})
        cls.addClassCleanup(fixture_type.doClassCleanups)
        fixture_type.setUpClass()
        cls.releases = {}
        for mode, names in ((None, ()), (OBSERVER, tuple(n for n in CONTEXT if not n.endswith("reclaim.py"))),
                            (MODE, CONTEXT)):
            fixture = fixture_type()
            fixture.setUp()
            source_map = {
                name: digest((fixture.source / ("scripts/windows/Dockerfile.mineru_owner_memory"
                    if name == "Dockerfile" else "scripts/windows/" + name)).read_bytes())
                for name in names
            }
            value = profile_value(mode, source_map)
            fixture.deployment.write_bytes(_wire(value))
            fixture.build()
            manifest, raw = packages.load_release_package(fixture.package)
            report = packages.verify_release_package(fixture.package)
            if not report.passed:
                raise AssertionError(report)
            cls.releases[mode] = (fixture, value, source_map, manifest, raw)

    def test_actual_builder_roundtrips_raw_observer_and_reclaim_without_version_promotion(self):
        for mode, version in ((None, "m6.release.v1"), (OBSERVER, "m6.release.v2"), (MODE, "m6.release.v3")):
            fixture, value, sources, manifest, raw = self.releases[mode]
            with self.subTest(mode=mode):
                profile = profiles.decode_mineru_deployment_profile(_wire(value))
                self.assertEqual(profile.exact_bytes, _wire(value))
                decoded = decode_mineru_release_manifest(raw)
                self.assertEqual((decoded.contract_version, decoded.inference_mode), (version, mode))
                self.assertEqual(decoded.exact_bytes, raw)
                if mode is None:
                    self.assertIsNone(manifest.inference_build)
                    self.assertFalse((fixture.package / "inference-context").exists())
                    continue
                build = manifest.inference_build
                self.assertEqual(set(build), {"context", "context_sha256", "sources"}
                                 | ({"mode"} if mode == MODE else set()))
                self.assertEqual(build["sources"], sources)
                self.assertEqual(build["context_sha256"], profile.inference_diagnostic.context_sha256)
                found = {str(path.relative_to(fixture.package / "inference-context"))
                         for path in (fixture.package / "inference-context").rglob("*") if path.is_file()}
                self.assertEqual(found, set(sources))
                for name in sources:
                    self.assertEqual(digest((fixture.package / "inference-context" / name).read_bytes()),
                                     manifest.file("inference-context/" + name).sha256)
        self.assertEqual(set(self.releases[MODE][2]), set(CONTEXT))

    def test_reclaim_changes_inference_worker_middleware_only_and_install_switch_matches(self):
        documents = {}
        for mode in (None, OBSERVER, MODE):
            fixture, value, _, _, _ = self.releases[mode]
            profile = profiles.decode_mineru_deployment_profile(_wire(value))
            documents[mode] = plan.compose_document(profile, package_fixture._capacity())
            service = documents[mode]["services"]["mineru-openai-server"]
            expected = LEGACY_INFERENCE_ARGV + ([] if mode is None else ["--worker-cls", WORKER])
            if mode == MODE:
                expected += ["--middleware", MIDDLEWARE]
            self.assertEqual(service["entrypoint"] + service["command"], expected)
            self.assertEqual(list(plan.inference_command_argv(profile)), expected)
            self.assertEqual(plan.verify_compose_projection(documents[mode], package_fixture._capacity(), profile), [])
        for mode in (OBSERVER, MODE):
            changed = deepcopy(documents[mode])
            for field in ("image", "command"):
                changed["services"]["mineru-openai-server"][field] = documents[None]["services"]["mineru-openai-server"][field]
            self.assertEqual(changed, documents[None])
        fixture = self.releases[MODE][0]
        wrapper = (fixture.package / "windows/run_mineru_installation.ps1").read_text()
        installer = (fixture.package / "windows/install_mineru_fixed_api.ps1").read_text()
        branch = wrapper.split("if ($manifest.contract_version -ceq 'm6.release.v3') {", 1)[1].split("\n            }", 1)[0]
        self.assertIn("$installerParameters['InferenceOwnerReclaim']=$null", branch)
        self.assertIn("-cne '" + MODE + "'", branch)
        named = re.search(r"\$contextNames=@\(([^\n]+)\)", branch).group(1)
        self.assertEqual(tuple(re.findall(r"'([^']+)'", named)), CONTEXT)
        self.assertIn("Assert-Closed $manifest.inference_build.sources $contextNames", wrapper)
        self.assertIn("$installerParameters['InferenceDiagnosticContextSha256']", wrapper)
        self.assertIn('[switch]$InferenceOwnerReclaim', installer)
        self.assertIn('if ($InferenceOwnerReclaim -and -not $InferenceDiagnostic)', installer)
        self.assertIn('if ($InferenceOwnerReclaim) { $inferenceArgv += @("--middleware", "' + MIDDLEWARE + '") }', installer)
        report = packages.verify_release_package(fixture.package)
        batch = installation.stage_batch(report, fixture.package, PureWindowsPath("C:/isolated-release"),
            fixture.case / "binding.json", PureWindowsPath("C:/isolated-binding"))
        for name in CONTEXT:
            expected_target = '"/C:/isolated-release/inference-context/' + name + '"'
            self.assertEqual(sum(line.startswith("put ") and line.endswith(expected_target)
                                 for line in batch.splitlines()), 1)

    def test_packaged_reclaim_bytes_cannot_be_changed_omitted_or_extended_under_same_identity(self):
        fixture, _, _, _, _ = self.releases[MODE]
        target = fixture.package / "inference-context/mineru_inference_memory/reclaim.py"
        original = target.read_bytes()
        extra = target.with_name("unreviewed.py")
        for action in ("changed", "missing", "extra"):
            with self.subTest(action=action):
                try:
                    if action == "changed":
                        target.write_bytes(original + b"\n# independent tamper\n")
                    elif action == "missing":
                        target.unlink()
                    else:
                        extra.write_text("# unreviewed context member")
                    with self.assertRaises(packages.ReleaseIdentityError):
                        packages.verify_release_package(fixture.package)
                finally:
                    target.write_bytes(original)
                    extra.unlink(missing_ok=True)
        self.assertTrue(packages.verify_release_package(fixture.package).passed)

    def test_closed_selection_and_context_membership_reject_even_with_recomputed_digest(self):
        _, value, sources, _, raw = self.releases[MODE]
        for kind in ("unknown_mode", "extra_diagnostic_field", "missing_diagnostic_field", "raw_with_diagnostic"):
            bad = deepcopy(value)
            if kind == "unknown_mode":
                bad["inference_diagnostic"]["contract_version"] = "mineru.owner-memory-reclaim.v2"
            elif kind == "extra_diagnostic_field":
                bad["inference_diagnostic"]["empty_cache_every_step"] = True
            elif kind == "missing_diagnostic_field":
                del bad["inference_diagnostic"]["worker_class"]
            else:
                bad["contract_version"] = "mineru.deployment-profile.v1"
            with self.subTest(profile=kind), self.assertRaises(ValueError):
                profiles.decode_mineru_deployment_profile(_wire(bad))
        for kind in ("missing_member", "extra_member", "unknown_mode", "observer_version", "extra_build_field"):
            bad = json.loads(raw)
            build = bad["inference_build"]
            if kind == "missing_member":
                del build["sources"]["mineru_inference_memory/reclaim.py"]
            elif kind == "extra_member":
                build["sources"]["mineru_inference_memory/unreviewed.py"] = sources["Dockerfile"]
            elif kind == "unknown_mode":
                build["mode"] = "mineru.owner-memory-reclaim.v2"
            elif kind == "observer_version":
                bad["contract_version"] = "m6.release.v2"
            else:
                build["release_when_busy"] = True
            build["context_sha256"] = digest(_wire(build["sources"]))
            with self.subTest(manifest=kind), self.assertRaises(ValueError):
                decode_mineru_release_manifest(_wire(bad))

    def diagnostic_observation(self, mode):
        _, value, sources, _, _ = self.releases[mode]
        profile = profiles.decode_mineru_deployment_profile(_wire(value))
        observed = capacity_fixture.observation()
        inference = observed["inference"]
        inference.update(image=profile.inference_diagnostic.image_reference, image_id="sha256:" + "f" * 64,
                         container_id="a" * 64, started_at="2026-10-12T01:00:00Z")
        inference["command"] += ["--worker-cls", WORKER]
        if mode == MODE:
            inference["command"] += ["--middleware", MIDDLEWARE]
        observed["schema"] = "mineru-windows-runtime-observation.v7"
        module = sources["mineru_inference_memory/worker.py"]
        observed["inference_diagnostic"] = {
            "contract_version": "mineru.owner-memory-observation.v1", "module_sha256": module,
            "image_labels": {"io.agent-invest.mineru.owner-memory-module-sha256": module,
                "io.agent-invest.mineru.owner-memory-dockerfile-sha256": sources["Dockerfile"],
                "io.agent-invest.mineru.owner-memory-base-repo-digest": value["base_image_repo_digest"],
                "io.agent-invest.mineru.owner-memory-context-sha256": profile.inference_diagnostic.context_sha256},
            "actual_source_sha256": profile.inference_diagnostic.preimages,
            "actual_diagnostic_source_sha256": {name: sha for name, sha in sources.items() if name != "Dockerfile"},
            "loaded_workers": [{"contract_version": "mineru.owner-memory-identity.v1", "module_sha256": module,
                "worker_class": WORKER, "pid": 42, "proc_start_ticks": 100, "observed_proc_start_ticks": 100,
                "namespace_init_start_ticks": 50, "device": "cuda:0", "instance_id": "independent-reclaim"}],
            "container_epoch": {key: inference[key] for key in ("container_id", "started_at")},
        }
        return observed, profile, sources

    def test_attester_uses_v7_and_exact_extended_argv_and_source_context_without_raw_fallback(self):
        for mode in (OBSERVER, MODE):
            observed, profile, sources = self.diagnostic_observation(mode)
            def attest(value, selected=sources):
                return capacity_fixture.build(value, capacity_fixture.capacity_payload(),
                    expected_deployment_profile=profile, expected_inference_sources=selected)
            original = deepcopy(observed)
            bundle = attest(observed)
            self.assertEqual(observed, original)
            self.assertEqual(bundle["manifest"]["contract_version"], "mineru-runtime-bundle.v11")
            self.assertEqual(bundle["manifest"]["inference_server"]["command"],
                             observed["inference"]["entrypoint"] + observed["inference"]["command"])
            self.assertEqual(bundle["manifest"]["inference_server"]["container_image_digest"],
                             observed["inference"]["image_id"])
            for version in ("mineru-windows-runtime-observation.v5", "mineru-windows-runtime-observation.v6"):
                bad = deepcopy(observed)
                bad["schema"] = version
                with self.subTest(mode=mode, version=version), self.assertRaises(ValueError):
                    attest(bad)
            extra = {**sources, "mineru_inference_memory/unreviewed.py": sources["Dockerfile"]}
            with self.subTest(mode=mode, extra="unreviewed"), self.assertRaises(ValueError):
                attest(observed, extra)
            for member in sources:
                missing = dict(sources)
                del missing[member]
                with self.subTest(mode=mode, missing=member), self.assertRaises(ValueError):
                    attest(observed, missing)
            if mode == MODE:
                for bad_command in (observed["inference"]["command"][:-2],
                                    observed["inference"]["command"] + ["--middleware", MIDDLEWARE]):
                    bad = deepcopy(observed)
                    bad["inference"]["command"] = bad_command
                    with self.subTest(command=bad_command), self.assertRaises(ValueError):
                        attest(bad)
        raw = capacity_fixture.observation()
        self.assertEqual(capacity_fixture.build(raw, capacity_fixture.capacity_payload())
                         ["manifest"]["contract_version"], "mineru-runtime-bundle.v11")
        self.assertEqual(legacy_fixture.build()["manifest"]["contract_version"], "mineru-runtime-bundle.v9")
        raw["schema"] = "mineru-windows-runtime-observation.v7"
        with self.assertRaises(ValueError):
            capacity_fixture.build(raw, capacity_fixture.capacity_payload())


if __name__ == "__main__":
    unittest.main()
