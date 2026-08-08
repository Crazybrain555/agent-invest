"""Closed parse-receipt contract: build/replay symmetry and hard edges."""

from __future__ import annotations

import unittest

from disclosure_anchor.application.contracts.parse_receipt import (
    ParseReceiptContractError,
    build_parse_receipt,
    validate_parse_receipt,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)

_SOURCE = "sha256:" + "a" * 64


def _target(backend: str, **overrides: object) -> dict[str, object]:
    remote = backend.endswith("-http-client")
    return ParserTargetIdentity(
        name="MinerU",
        package_version="3.4.0",
        backend=backend,
        method="auto",
        language="ch",
        formula=True,
        table=True,
        runtime_bundle_identity_sha256="sha256:" + "b" * 64,
        remote_model_name=("served-model" if remote else None),
        remote_selection_mode=("explicit" if remote else "not_applicable"),
        **overrides,  # type: ignore[arg-type]
    ).to_payload()


class ParseReceiptTests(unittest.TestCase):
    def test_http_receipt_round_trips(self) -> None:
        target = _target("vlm-http-client")
        receipt = build_parse_receipt(
            source_pdf_sha256=_SOURCE,
            parser_target_payload=target,
            server_url="http://gpu.example:30000/",
            http_request_concurrency=4,
            timeout_seconds=1200,
        )
        self.assertEqual(
            receipt["endpoint"]["server_url"], "http://gpu.example:30000"
        )
        validate_parse_receipt(
            receipt,
            source_pdf_sha256=_SOURCE,
            parser_target_payload=target,
        )

    def test_local_receipt_never_carries_an_endpoint_url(self) -> None:
        target = _target("pipeline")
        receipt = build_parse_receipt(
            source_pdf_sha256=_SOURCE,
            parser_target_payload=target,
            server_url="http://leaked-anyway:1",
            http_request_concurrency=None,
            timeout_seconds=None,
        )
        self.assertIsNone(receipt["endpoint"]["server_url"])
        validate_parse_receipt(
            receipt,
            source_pdf_sha256=_SOURCE,
            parser_target_payload=target,
        )
        receipt["endpoint"]["server_url"] = "http://leaked-anyway:1"
        with self.assertRaisesRegex(
            ParseReceiptContractError, "null server_url"
        ):
            validate_parse_receipt(
                receipt,
                source_pdf_sha256=_SOURCE,
                parser_target_payload=target,
            )

    def test_http_receipt_requires_a_normalized_endpoint_url(self) -> None:
        target = _target("vlm-http-client")
        base = build_parse_receipt(
            source_pdf_sha256=_SOURCE,
            parser_target_payload=target,
            server_url="http://gpu.example:30000",
            http_request_concurrency=None,
            timeout_seconds=None,
        )
        for label, bad_url in (
            ("null", None),
            ("blank", "   "),
            ("trailing_slash", "http://gpu.example:30000/"),
            ("not_http", "ftp://gpu.example:30000"),
        ):
            with self.subTest(label=label):
                import copy

                receipt = copy.deepcopy(base)
                receipt["endpoint"]["server_url"] = bad_url
                with self.assertRaises(ParseReceiptContractError):
                    validate_parse_receipt(
                        receipt,
                        source_pdf_sha256=_SOURCE,
                        parser_target_payload=target,
                    )

    def test_fingerprint_and_identity_replays_fail_closed(self) -> None:
        target = _target("vlm-http-client")
        base = build_parse_receipt(
            source_pdf_sha256=_SOURCE,
            parser_target_payload=target,
            server_url="http://gpu.example:30000",
            http_request_concurrency=None,
            timeout_seconds=None,
        )
        import copy

        wrong_pdf = copy.deepcopy(base)
        with self.assertRaisesRegex(
            ParseReceiptContractError, "different source PDF"
        ):
            validate_parse_receipt(
                wrong_pdf,
                source_pdf_sha256="sha256:" + "c" * 64,
                parser_target_payload=target,
            )
        forged_model = copy.deepcopy(base)
        forged_model["endpoint"]["remote_model_name"] = "other"
        with self.assertRaises(ParseReceiptContractError):
            validate_parse_receipt(
                forged_model,
                source_pdf_sha256=_SOURCE,
                parser_target_payload=target,
            )
        forged_fingerprint = copy.deepcopy(base)
        forged_fingerprint["endpoint"]["endpoint_selection_sha256"] = (
            "sha256:" + "d" * 64
        )
        with self.assertRaisesRegex(
            ParseReceiptContractError, "does not replay"
        ):
            validate_parse_receipt(
                forged_fingerprint,
                source_pdf_sha256=_SOURCE,
                parser_target_payload=target,
            )


class WriteAuthorityTests(unittest.TestCase):
    def test_unknown_write_authority_fails_closed(self) -> None:
        from disclosure_anchor.application.contracts.normalized_ir import (
            NormalizedIRVersionError,
            validate_current_normalized_ir_for_write,
        )

        with self.assertRaisesRegex(
            NormalizedIRVersionError, "write authority"
        ):
            validate_current_normalized_ir_for_write(
                {},
                write_authority="prodution",  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
