"""Closed, persisted identity of one content-affecting parser target."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import re
from typing import Any, Literal, cast

PARSER_TARGET_CONTRACT_VERSION = "parser-target.v1"
READABLE_PARSER_TARGET_CONTRACT_VERSION = "parser-target.v2"
MINERU_INLINE_EQUATION_DELIMITERS = ("$", "$")

_BACKENDS = frozenset(
    {
        "pipeline",
        "vlm-engine",
        "vlm-http-client",
        "hybrid-engine",
        "hybrid-http-client",
    }
)
_METHODS = frozenset({"auto", "txt", "ocr"})
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")


class ParserTargetIdentityError(ValueError):
    """A parser target is incomplete, contradictory, or malformed."""

    reason_code = "parser_target_identity_invalid"


@dataclass(frozen=True)
class ParserTargetIdentity:
    name: str
    package_version: str
    backend: str
    method: str
    language: str
    formula: bool
    table: bool
    effort: Literal["medium", "high"] | None = None
    image_analysis: bool = False
    full_pdf: bool = True
    start_page: int | None = None
    end_page: int | None = None
    runtime_bundle_identity_sha256: str = ""
    remote_model_name: str | None = None
    remote_selection_mode: Literal[
        "explicit",
        "server_singleton_unattested",
        "not_applicable",
    ] = "not_applicable"
    inline_equation_left: str = MINERU_INLINE_EQUATION_DELIMITERS[0]
    inline_equation_right: str = MINERU_INLINE_EQUATION_DELIMITERS[1]
    target_contract_version: str = PARSER_TARGET_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _validate_target(self)

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.target_contract_version == PARSER_TARGET_CONTRACT_VERSION:
            payload.pop("remote_model_name")
            payload.pop("remote_selection_mode")
        return payload

    @classmethod
    def from_payload(cls, value: object) -> "ParserTargetIdentity":
        if not isinstance(value, Mapping):
            raise ParserTargetIdentityError(
                "parser target identity must be an object"
            )
        version = value.get("target_contract_version")
        expected = set(cls.__dataclass_fields__)
        if version == PARSER_TARGET_CONTRACT_VERSION:
            expected -= {"remote_model_name", "remote_selection_mode"}
        elif version != READABLE_PARSER_TARGET_CONTRACT_VERSION:
            raise ParserTargetIdentityError(
                "parser target contract version is unsupported"
            )
        if set(value) != expected:
            raise ParserTargetIdentityError(
                "parser fields are not closed"
            )
        try:
            payload = dict(value)
            if version == PARSER_TARGET_CONTRACT_VERSION:
                payload.update(
                    remote_model_name=None,
                    remote_selection_mode="not_applicable",
                )
            return cls(**cast(Any, payload))
        except TypeError as exc:
            raise ParserTargetIdentityError(
                "parser target identity has invalid fields"
            ) from exc


def _validate_target(target: ParserTargetIdentity) -> None:
    for field in ("name", "package_version", "language"):
        value = getattr(target, field)
        if not isinstance(value, str) or not value:
            raise ParserTargetIdentityError(
                f"parser target {field} must be non-empty text"
            )
    if target.target_contract_version not in {
        PARSER_TARGET_CONTRACT_VERSION,
        READABLE_PARSER_TARGET_CONTRACT_VERSION,
    }:
        raise ParserTargetIdentityError(
            "parser target contract version is unsupported"
        )
    if target.backend not in _BACKENDS:
        raise ParserTargetIdentityError("parser target backend is unsupported")
    if target.method not in _METHODS:
        raise ParserTargetIdentityError("parser target method is unsupported")
    for field in ("formula", "table", "image_analysis", "full_pdf"):
        if not isinstance(getattr(target, field), bool):
            raise ParserTargetIdentityError(
                f"parser target {field} must be boolean"
            )
    hybrid = target.backend.startswith("hybrid-")
    if hybrid != (target.effort in {"medium", "high"}):
        raise ParserTargetIdentityError(
            "parser target effort differs from its backend"
        )
    if target.effort == "medium" and target.image_analysis:
        raise ParserTargetIdentityError(
            "hybrid medium target cannot enable image analysis"
        )
    if target.backend == "pipeline" and target.image_analysis:
        raise ParserTargetIdentityError(
            "pipeline target cannot enable image analysis"
        )
    for field in ("start_page", "end_page"):
        page = getattr(target, field)
        if page is not None and (
            isinstance(page, bool) or not isinstance(page, int) or page < 0
        ):
            raise ParserTargetIdentityError(
                f"parser target {field} must be a non-negative integer or null"
            )
    if (
        target.start_page is not None
        and target.end_page is not None
        and target.start_page > target.end_page
    ):
        raise ParserTargetIdentityError(
            "parser target start_page exceeds end_page"
        )
    if target.full_pdf != (
        target.start_page is None and target.end_page is None
    ):
        raise ParserTargetIdentityError(
            "parser target full_pdf differs from its requested page range"
        )
    if _SHA256.fullmatch(target.runtime_bundle_identity_sha256) is None:
        raise ParserTargetIdentityError(
            "parser target requires an immutable runtime-bundle digest"
        )
    if target.remote_model_name is not None and (
        not isinstance(target.remote_model_name, str)
        or not target.remote_model_name.strip()
        or any(ord(char) < 32 for char in target.remote_model_name)
    ):
        raise ParserTargetIdentityError(
            "parser target remote model name is invalid"
        )
    if target.target_contract_version == PARSER_TARGET_CONTRACT_VERSION:
        if (
            target.remote_model_name is not None
            or target.remote_selection_mode != "not_applicable"
        ):
            raise ParserTargetIdentityError(
                "legacy parser target carries remote selection fields"
            )
    elif target.backend.endswith("-http-client"):
        expected_selection = (
            "explicit"
            if target.remote_model_name is not None
            else "server_singleton_unattested"
        )
        if target.remote_selection_mode != expected_selection:
            raise ParserTargetIdentityError(
                "HTTP parser target remote selection mode is invalid"
            )
    elif (
        target.remote_model_name is not None
        or target.remote_selection_mode != "not_applicable"
    ):
        raise ParserTargetIdentityError(
            "local parser target carries remote selection fields"
        )
    if (
        target.inline_equation_left,
        target.inline_equation_right,
    ) != MINERU_INLINE_EQUATION_DELIMITERS:
        raise ParserTargetIdentityError(
            "parser target inline-equation profile is unsupported"
        )


__all__ = [
    "MINERU_INLINE_EQUATION_DELIMITERS",
    "PARSER_TARGET_CONTRACT_VERSION",
    "READABLE_PARSER_TARGET_CONTRACT_VERSION",
    "ParserTargetIdentity",
    "ParserTargetIdentityError",
]
