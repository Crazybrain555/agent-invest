"""Strict immutable values and canonical wire bytes for private M6 evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from disclosure_anchor.application.contracts.synchronized_telemetry import (
    parse_canonical_json_artifact,
)


M6Hash = Annotated[str, StringConstraints(
    strict=True, min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$",
)]
M6Id = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=128, pattern=r"^[^\x00-\x20\x7f]+$",
)]
M6Reason = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=256, pattern=r"^[^\x00-\x20\x7f]+$",
)]
M6PositiveInt = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
M6NonnegativeInt = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class M6ClosedModel(BaseModel):
    """Nested values must also be closed models, tuples, or immutable scalars.

    This is a data validation boundary, not an attestation authority: a valid
    model or hash cannot prove that an external verifier performed its checks.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False,
        revalidate_instances="always",
    )

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")

    def canonical_sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_canonical_bytes(cls, payload: bytes, *, maximum_bytes: int) -> Self:
        if type(maximum_bytes) is not int or maximum_bytes < 1:
            raise ValueError("M6 wire byte limit must be positive")
        try:
            parse_canonical_json_artifact(
                payload, label=cls.__name__, maximum_bytes=maximum_bytes,
            )
        except RecursionError as exc:
            raise ValueError("M6 JSON nesting exceeds the decoder boundary") from exc
        value = cls.model_validate_json(payload)
        if value.canonical_bytes() != payload:
            raise ValueError("M6 wire must include the exact complete model")
        return value
