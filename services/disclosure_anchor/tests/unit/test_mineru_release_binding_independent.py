"""Independent refusal cases for release binding; no live HTTP or credentials."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from disclosure_anchor.adapters.runtime import mineru_release_binding as binding


IDENTITY = "sha256:" + "1" * 64


class MineruReleaseBindingIndependentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.evidence = self.root / "dummy-reference.json"
        self.evidence.write_text("{}")
        self.private = self.root / "private.json"
        self.document = {
            "contract_version": "m6.release-bind-private-inputs.v1",
            "api_url": "http://127.0.0.1:30003",
            "gpu_uuid": "GPU-12345678-abcd-4321-9876-abcdef123456",
            "smoke_receipt_path": str(self.evidence),
            "validation_receipt_path": str(self.evidence),
            "canary_cache_path": str(self.evidence),
        }

    def write(self):
        self.private.write_text(json.dumps(self.document))
        self.private.chmod(0o600)

    def test_a_pass_label_and_matching_hash_do_not_prove_deployment_qualification(self):
        # Missing canary identities, before/after epoch, consumption/absence,
        # closure and conservation. The thin binder must reuse real validation,
        # not let this self-asserted pass become a qualification hash.
        path = self.root / "claimed-pass.json"
        path.write_text(json.dumps({
            "schema": "mineru_heldout_validation_receipt.v2", "status": "pass",
            "documents": [{"receipt": {"identity": {
                "runtime_manifest_identity_sha256": IDENTITY,
            }}}],
        }))
        path.chmod(0o600)
        with self.assertRaises(ValueError):
            binding.load_validation_receipt(path, runtime_identity=IDENTITY)

    def test_local_endpoint_validation_uses_parsed_authority_not_a_string_prefix(self):
        for url in ("http://127.0.0.1:30003@outside.invalid", "http://localhost:30003@outside.invalid",
                    "http://127.0.0.1:30003#ignored", "http://127.0.0.1:30003?unexpected=1",
                    "http://127.0.0.1:not-a-port", "http://127.0.0.1:0"):
            with self.subTest(url=url):
                self.document["api_url"] = url
                self.write()
                with self.assertRaises(ValueError):
                    binding.load_bind_private_inputs(self.private)

    @unittest.skipIf(os.name == "nt", "POSIX private configuration boundary")
    def test_private_binding_is_not_accepted_with_group_or_world_read_access(self):
        self.write()
        for mode in (0o640, 0o644, 0o660):
            with self.subTest(mode=oct(mode)):
                self.private.chmod(mode)
                with self.assertRaises(ValueError):
                    binding.load_bind_private_inputs(self.private)
