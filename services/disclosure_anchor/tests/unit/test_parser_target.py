"""Closed parser-target compatibility tests."""

from __future__ import annotations

import unittest

from disclosure_anchor.application.contracts.parser_target import (
    PARSER_TARGET_CONTRACT_VERSION,
    READABLE_PARSER_TARGET_CONTRACT_VERSION,
    ParserTargetIdentity,
    ParserTargetIdentityError,
)


class ParserTargetIdentityTests(unittest.TestCase):
    @staticmethod
    def _legacy_payload() -> dict[str, object]:
        return ParserTargetIdentity(
            name="MinerU",
            package_version="3.4.0",
            backend="pipeline",
            method="auto",
            language="ch",
            formula=True,
            table=True,
            runtime_bundle_identity_sha256="sha256:" + "b" * 64,
        ).to_payload()

    @classmethod
    def _v2_payload(cls, **changes: object) -> dict[str, object]:
        payload = cls._legacy_payload()
        payload.update(
            target_contract_version=READABLE_PARSER_TARGET_CONTRACT_VERSION,
            remote_model_name=None,
            remote_selection_mode="not_applicable",
        )
        payload.update(changes)
        return payload

    def test_v1_write_shape_remains_unchanged(self) -> None:
        payload = self._legacy_payload()

        self.assertEqual(
            payload["target_contract_version"],
            PARSER_TARGET_CONTRACT_VERSION,
        )
        self.assertNotIn("remote_model_name", payload)
        self.assertNotIn("remote_selection_mode", payload)
        self.assertEqual(ParserTargetIdentity.from_payload(payload).to_payload(), payload)

    def test_v2_exact_shapes_round_trip_without_becoming_the_write_default(
        self,
    ) -> None:
        cases = (
            self._v2_payload(),
            self._v2_payload(
                backend="vlm-http-client",
                remote_model_name="MinerU2.5-Pro-2605-1.2B",
                remote_selection_mode="explicit",
            ),
            self._v2_payload(
                backend="vlm-http-client",
                remote_model_name=None,
                remote_selection_mode="server_singleton_unattested",
            ),
        )
        for payload in cases:
            with self.subTest(payload=payload):
                target = ParserTargetIdentity.from_payload(payload)
                self.assertEqual(target.to_payload(), payload)

        self.assertEqual(
            ParserTargetIdentity.from_payload(
                self._legacy_payload()
            ).target_contract_version,
            PARSER_TARGET_CONTRACT_VERSION,
        )

    def test_v2_remote_selection_matrix_and_closed_shapes_are_enforced(
        self,
    ) -> None:
        cases = {
            "local_explicit": self._v2_payload(
                remote_model_name="served-model",
                remote_selection_mode="explicit",
            ),
            "remote_null_explicit": self._v2_payload(
                backend="vlm-http-client",
                remote_selection_mode="explicit",
            ),
            "remote_named_singleton": self._v2_payload(
                backend="vlm-http-client",
                remote_model_name="served-model",
                remote_selection_mode="server_singleton_unattested",
            ),
            "blank_model": self._v2_payload(
                backend="vlm-http-client",
                remote_model_name="   ",
                remote_selection_mode="explicit",
            ),
            "control_model": self._v2_payload(
                backend="vlm-http-client",
                remote_model_name="served\nmodel",
                remote_selection_mode="explicit",
            ),
            "missing_field": self._v2_payload(),
            "extra_field": self._v2_payload(future=True),
            "unknown_version": self._v2_payload(
                target_contract_version="parser-target.v99"
            ),
        }
        cases["missing_field"].pop("remote_selection_mode")
        legacy_with_remote = self._legacy_payload()
        legacy_with_remote["remote_model_name"] = None
        cases["legacy_with_remote"] = legacy_with_remote

        for label, payload in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ParserTargetIdentityError):
                    ParserTargetIdentity.from_payload(payload)


if __name__ == "__main__":
    unittest.main()
