from __future__ import annotations

from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4

from dataclasses import replace
import base64
import hashlib
import json
import unittest

from disclosure_anchor.application.contracts.mineru_process_profile import (
    encode_mineru_process_profile,
)
from disclosure_anchor.application.contracts.v4_prepared_execution_spec import (
    V4_PREPARED_EXECUTION_SPEC_CONTRACT,
    V4PreparedExecutionSpec,
    decode_v4_prepared_execution_spec,
)
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.staged_provider_parser import (
    PreparedSubmissionIdentity,
)
from tests.unit.test_mineru_process_profile import _profile


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _prepared(
    *,
    identity: ParserIdentity,
    options: ParserOptions,
    request: bytes,
) -> PreparedSubmissionIdentity:
    target = options.target_identity(identity)
    target_sha256 = _digest(
        json.dumps(
            target.to_payload(), sort_keys=True, separators=(",", ":")
        ).encode()
    )
    projection = {
        "schema": "mineru-prepared-submission.v1",
        "attempt_identity": "attempt-1",
        "fence_identity": "fence-1",
        "source_pdf_sha256": "sha256:" + "a" * 64,
        "parser_target_identity_sha256": target_sha256,
        "runtime_bundle_identity_sha256": options.runtime_bundle_identity_sha256,
        "request_sha256": _digest(request),
        "client_submit_key": "submit-1",
        "submission_epoch_unix": 1_725_000_000,
    }
    exact = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    return PreparedSubmissionIdentity(
        **projection,
        exact_bytes=exact,
        sha256=_digest(exact),
    )


def _spec(
    *,
    options: ParserOptions | None = None,
    request: bytes = b'{"model":"mineru","stream":false}',
) -> V4PreparedExecutionSpec:
    profile = _profile()
    identity = ParserIdentity(name="mineru", version="3.4.4")
    parser_options = options or ParserOptions(
        method="auto",
        backend="hybrid-http-client",
        language="ch",
        formula=True,
        table=True,
        effort="medium",
        image_analysis=False,
        start_page=None,
        end_page=None,
        timeout_seconds=21_600,
        api_url="http://127.0.0.1:30003",
        api_drain_timeout_seconds=86_400,
        server_url="http://mineru-openai-server:30000/v1",
        http_request_concurrency=7,
        runtime_bundle_identity_sha256=profile.runtime_bundle_identity_sha256,
    )
    profile_bytes = encode_mineru_process_profile(profile)
    return V4PreparedExecutionSpec(
        contract_version=V4_PREPARED_EXECUTION_SPEC_CONTRACT,
        prepared_submission=_prepared(
            identity=identity,
            options=parser_options,
            request=request,
        ),
        parser_identity=identity,
        parser_options=parser_options,
        api_origin="http://127.0.0.1:30003",
        server_url="http://mineru-openai-server:30000/v1",
        request_exact_bytes=request,
        request_sha256=_digest(request),
        process_profile_exact_bytes=profile_bytes,
        process_profile_sha256=profile.sha256,
        worker_profile=StagedWorkerProfileV4(profile.sha256, 1, 1),
        result_lease_seconds=300,
        remote_runaway_seconds=86_400,
        archive_member_count_limit=100_000,
        archive_uncompressed_byte_limit=16 * 1024 * 1024 * 1024,
    )


class V4PreparedExecutionSpecTests(unittest.TestCase):
    def test_local_capacity_and_probe_policy_change_the_h0_bound_spec_identity(self) -> None:
        original = _spec()
        for change in (
            {"mac_preflight_workers": 2}, {"mac_finalize_workers": 2},
            {"provider_poll_milliseconds": 2000}, {"admission_probe_milliseconds": 2000},
        ):
            with self.subTest(change=change):
                changed = replace(original, worker_profile=replace(original.worker_profile, **change))
                self.assertNotEqual(changed.worker_profile.sha256, original.worker_profile.sha256)
                self.assertNotEqual(changed.sha256, original.sha256)
                self.assertEqual(decode_v4_prepared_execution_spec(changed.exact_bytes), changed)
        with self.assertRaisesRegex(ValueError, "worker composition"):
            replace(original, worker_profile=replace(
                original.worker_profile, process_profile_sha256="sha256:" + "f" * 64,
            ))

    def test_exact_spec_roundtrips_with_stable_identity(self) -> None:
        spec = _spec()
        reopened = decode_v4_prepared_execution_spec(spec.exact_bytes)

        self.assertEqual(reopened, spec)
        self.assertEqual(reopened.sha256, spec.sha256)
        self.assertEqual(reopened.byte_count, len(spec.exact_bytes))
        self.assertEqual(reopened.process_profile, _profile())

    def test_nested_execution_fields_and_exact_bytes_are_all_serialized(self) -> None:
        spec = _spec()
        payload = json.loads(spec.exact_bytes)

        self.assertEqual(
            set(payload["parser_options"]),
            set(ParserOptions.__dataclass_fields__),
        )
        self.assertEqual(
            base64.b64decode(payload["prepared_submission_exact_b64"]),
            spec.prepared_submission.exact_bytes,
        )
        self.assertEqual(
            base64.b64decode(payload["request_exact_b64"]),
            spec.request_exact_bytes,
        )
        self.assertEqual(
            base64.b64decode(payload["process_profile_exact_b64"]),
            spec.process_profile_exact_bytes,
        )

    def test_transport_and_runtime_execution_fields_change_spec_identity(self) -> None:
        baseline = _spec()
        changes = (
            {"timeout_seconds": 10_800},
            {"api_drain_timeout_seconds": 43_200},
            {"http_request_concurrency": 8},
            {"formula": False},
            {"table": False},
        )
        for update in changes:
            with self.subTest(update=update):
                changed = _spec(options=replace(baseline.parser_options, **update))
                self.assertNotEqual(changed.sha256, baseline.sha256)

    def test_request_profile_target_and_endpoint_drift_fail_closed(self) -> None:
        spec = _spec()
        mutations = (
            {"request_sha256": "sha256:" + "b" * 64},
            {"process_profile_sha256": "sha256:" + "b" * 64},
            {"api_origin": "http://127.0.0.1:30004"},
            {"server_url": "http://mineru-openai-server:30001/v1"},
            {
                "prepared_submission": _prepared(
                    identity=spec.parser_identity,
                    options=replace(spec.parser_options, table=False),
                    request=spec.request_exact_bytes,
                )
            },
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                replace(spec, **mutation)

    def test_contract_rejects_extras_duplicates_aliases_and_noncanonical_base64(self) -> None:
        spec = _spec()
        decoded = json.loads(spec.exact_bytes)

        extra = dict(decoded)
        extra["legacy"] = True
        with self.assertRaisesRegex(ValueError, "fields are not closed"):
            decode_v4_prepared_execution_spec(
                json.dumps(extra, sort_keys=True, separators=(",", ":")).encode()
            )

        duplicate = spec.exact_bytes[:-1] + b',"result_lease_seconds":300}'
        with self.assertRaisesRegex(ValueError, "strict UTF-8 JSON"):
            decode_v4_prepared_execution_spec(duplicate)

        noncanonical = spec.exact_bytes.replace(b'":', b'": ', 1)
        with self.assertRaisesRegex(ValueError, "bytes are not canonical"):
            decode_v4_prepared_execution_spec(noncanonical)

        bad_base64 = dict(decoded)
        bad_base64["request_exact_b64"] += "="
        with self.assertRaisesRegex(ValueError, "base64"):
            decode_v4_prepared_execution_spec(
                json.dumps(bad_base64, sort_keys=True, separators=(",", ":")).encode()
            )

    def test_bounds_and_url_authority_fail_closed(self) -> None:
        spec = _spec()
        changes = (
            {"result_lease_seconds": 3601},
            {"remote_runaway_seconds": True},
            {"archive_member_count_limit": 100_001},
            {
                "archive_uncompressed_byte_limit": (
                    spec.process_profile.temporary_disk_bytes_limit + 1
                )
            },
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(spec, **change)

        bad_options = replace(
            spec.parser_options,
            api_url="http://user@127.0.0.1:30003",
        )
        with self.assertRaises(ValueError):
            replace(
                spec,
                parser_options=bad_options,
                api_origin=bad_options.api_url,
            )

        default_port_options = replace(
            spec.parser_options,
            api_url="https://mineru.example",
            server_url="https://inference.example/v1",
        )
        default_port_spec = V4PreparedExecutionSpec(
            contract_version=spec.contract_version,
            prepared_submission=_prepared(
                identity=spec.parser_identity,
                options=default_port_options,
                request=spec.request_exact_bytes,
            ),
            parser_identity=spec.parser_identity,
            parser_options=default_port_options,
            api_origin=default_port_options.api_url,
            server_url=default_port_options.server_url,
            request_exact_bytes=spec.request_exact_bytes,
            request_sha256=spec.request_sha256,
            process_profile_exact_bytes=spec.process_profile_exact_bytes,
            process_profile_sha256=spec.process_profile_sha256,
            worker_profile=spec.worker_profile,
            result_lease_seconds=spec.result_lease_seconds,
            remote_runaway_seconds=spec.remote_runaway_seconds,
            archive_member_count_limit=spec.archive_member_count_limit,
            archive_uncompressed_byte_limit=spec.archive_uncompressed_byte_limit,
        )
        self.assertEqual(
            decode_v4_prepared_execution_spec(default_port_spec.exact_bytes),
            default_port_spec,
        )


if __name__ == "__main__":
    unittest.main()
