"""Runtime binding of existing immutable M6 membership to staged V4 admission.

The M6 wire contracts keep their original canonical ordering and hashes. This
binding adds no wire schema, scheduling order, publication or history authority.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field

from disclosure_anchor.application.contracts.m6_campaign import (
    M6CampaignScope, M6CorpusManifest,
)


class V4CampaignScopeViolation(ValueError):
    """An attempted source or durable responsibility escapes the frozen scope."""


@dataclass(frozen=True, slots=True)
class V4CampaignAdmissionScope:
    scope: M6CampaignScope
    manifest: M6CorpusManifest
    _source_hashes: tuple[str, ...] = field(init=False, repr=False)
    _ordinary_document_ids: tuple[str, ...] = field(init=False, repr=False)
    _scope_sha256: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.scope) is not M6CampaignScope or type(self.manifest) is not M6CorpusManifest:
            raise ValueError("V4 campaign requires exact M6 scope and corpus")
        # Revalidate constructed/copied models as well as ordinary decoded input.
        scope = M6CampaignScope.model_validate(self.scope)
        manifest = M6CorpusManifest.model_validate(self.manifest)
        scope.verify_manifest(manifest)
        entries = {entry.document_id: entry for entry in manifest.entries}
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "_source_hashes", tuple(
            entries[document_id].source_pdf_sha256 for document_id in scope.document_ids
        ))
        object.__setattr__(self, "_ordinary_document_ids", tuple(
            document_id for document_id in scope.document_ids
            if entries[document_id].origin != "carry_in"
        ))
        object.__setattr__(self, "_scope_sha256", scope.canonical_sha256())

    @property
    def document_ids(self) -> tuple[str, ...]:
        return self.scope.document_ids

    @property
    def source_hashes(self) -> tuple[str, ...]:
        """Source hashes in the same canonical document order as document_ids."""
        return self._source_hashes

    @property
    def ordinary_document_ids(self) -> tuple[str, ...]:
        """Carry-in may recover existing heads, but cannot create a fresh H0."""
        return self._ordinary_document_ids

    @property
    def scope_sha256(self) -> str:
        return self._scope_sha256

    def require_document_source(self, document_id: str, source_pdf_sha256: str) -> None:
        if type(document_id) is not str or type(source_pdf_sha256) is not str:
            raise V4CampaignScopeViolation("V4 campaign source identity is invalid")
        index = bisect_left(self.document_ids, document_id)
        if index == len(self.document_ids) or self.document_ids[index] != document_id:
            raise V4CampaignScopeViolation("V4 document is outside campaign scope")
        if self.source_hashes[index] != source_pdf_sha256:
            raise V4CampaignScopeViolation("V4 source differs from frozen campaign source")

    def require_ordinary_document_source(self, document_id: str, source_pdf_sha256: str) -> None:
        self.require_document_source(document_id, source_pdf_sha256)
        index = bisect_left(self.ordinary_document_ids, document_id)
        if (index == len(self.ordinary_document_ids)
                or self.ordinary_document_ids[index] != document_id):
            raise V4CampaignScopeViolation("V4 campaign carry-in cannot create a fresh H0")


def require_v4_campaign_scope(value: object) -> V4CampaignAdmissionScope:
    """New campaign entrypoints call this before constructing any runtime owner."""
    if type(value) is not V4CampaignAdmissionScope:
        raise ValueError("V4 campaign requires an explicit nonempty frozen scope")
    return value


__all__ = [
    "V4CampaignAdmissionScope", "V4CampaignScopeViolation", "require_v4_campaign_scope",
]
