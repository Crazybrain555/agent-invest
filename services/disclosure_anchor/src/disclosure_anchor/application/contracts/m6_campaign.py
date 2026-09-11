"""Frozen full-source membership; independent of legacy 1..8 commissioning."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from disclosure_anchor.application.contracts.m6_common import (
    M6ClosedModel, M6Hash, M6Id, M6PositiveInt,
)


# A wire/memory bound, not a prescribed experiment size or throughput target.
M6_MAX_CORPUS_ENTRIES = 10_000
def validate_m6_document_id(value: str) -> str:
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("M6 document ID is not an opaque canonical identifier")
    return value


M6DocumentId = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=64),
    AfterValidator(validate_m6_document_id),
]
M6Mode = Literal["service_diagnostic", "e2e_publication"]


class M6CorpusEntry(M6ClosedModel):
    source_pdf_sha256: M6Hash
    source_byte_count: M6PositiveInt
    source_page_count: M6PositiveInt
    document_id: M6DocumentId | None
    stratum: M6Id
    origin: Literal["fresh", "replay", "carry_in"]

class M6CorpusManifest(M6ClosedModel):
    contract_version: Literal["m6.corpus-manifest.v1"] = "m6.corpus-manifest.v1"
    campaign_id: M6Id
    mode: M6Mode
    entries: Annotated[tuple[M6CorpusEntry, ...], Field(
        min_length=1, max_length=M6_MAX_CORPUS_ENTRIES,
    )]

    @model_validator(mode="after")
    def closed_membership(self) -> Self:
        hashes = tuple(entry.source_pdf_sha256 for entry in self.entries)
        if hashes != tuple(sorted(set(hashes))):
            raise ValueError("M6 corpus must be source-hash ordered and unique")
        document_ids = tuple(entry.document_id for entry in self.entries)
        if self.mode == "e2e_publication":
            if None in document_ids or len(set(document_ids)) != len(document_ids):
                raise ValueError("E2E corpus requires unique explicit document IDs")
        elif any(value is not None for value in document_ids):
            raise ValueError("service corpus cannot claim business document membership")
        return self

    @property
    def source_bytes(self) -> int:
        return sum(entry.source_byte_count for entry in self.entries)


class M6CampaignScope(M6ClosedModel):
    contract_version: Literal["m6.campaign-scope.v1"] = "m6.campaign-scope.v1"
    campaign_id: M6Id
    manifest_sha256: M6Hash
    document_ids: Annotated[tuple[M6DocumentId, ...], Field(
        min_length=1, max_length=M6_MAX_CORPUS_ENTRIES,
    )]

    @model_validator(mode="after")
    def canonical_membership(self) -> Self:
        if self.document_ids != tuple(sorted(set(self.document_ids))):
            raise ValueError("campaign document IDs must be sorted and unique")
        return self

    @classmethod
    def from_manifest(cls, manifest: M6CorpusManifest) -> Self:
        if manifest.mode != "e2e_publication":
            raise ValueError("campaign scope requires an E2E corpus")
        return cls(
            campaign_id=manifest.campaign_id,
            manifest_sha256=manifest.canonical_sha256(),
            document_ids=tuple(sorted(
                entry.document_id for entry in manifest.entries if entry.document_id is not None
            )),
        )

    def verify_manifest(self, manifest: M6CorpusManifest) -> None:
        if self != self.from_manifest(manifest):
            raise ValueError("campaign scope differs from frozen corpus")
