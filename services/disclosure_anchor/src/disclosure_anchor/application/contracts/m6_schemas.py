"""Generated private operational M6 schemas; existing public v1 is unchanged."""

from __future__ import annotations

from typing import Any

from disclosure_anchor.application.contracts.m6_campaign import M6CampaignScope, M6CorpusManifest
from disclosure_anchor.application.contracts.m6_common import M6ClosedModel
from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6DocumentQualification, M6QualificationEvidence, M6QualityPlan,
)
from disclosure_anchor.application.contracts.m6_owner import (
    M6OwnerAnchor, M6OwnerReply, M6OwnerRequest, M6OwnerStatus,
)
from disclosure_anchor.application.contracts.m6_run import M6RunReceipt, M6RunSpec, M6SourceHistoryFact
from disclosure_anchor.application.contracts.m6_run_events import M6ProducerEvent, M6RunEvent


def operational_m6_schema_documents() -> dict[str, dict[str, Any]]:
    models: dict[str, type[M6ClosedModel]] = {
        "m6-campaign-scope.v1.schema.json": M6CampaignScope,
        "m6-corpus-manifest.v1.schema.json": M6CorpusManifest,
        "m6-quality-plan.v1.schema.json": M6QualityPlan,
        "m6-qualification-evidence.v1.schema.json": M6QualificationEvidence,
        "m6-document-qualification.v1.schema.json": M6DocumentQualification,
        "m6-source-history-fact.v1.schema.json": M6SourceHistoryFact,
        "m6-run-spec.v1.schema.json": M6RunSpec,
        "m6-producer-event.v1.schema.json": M6ProducerEvent,
        "m6-run-event.v1.schema.json": M6RunEvent,
        "m6-run-receipt.v1.schema.json": M6RunReceipt,
        "m6-owner-anchor.v1.schema.json": M6OwnerAnchor,
        "m6-owner-request.v1.schema.json": M6OwnerRequest,
        "m6-owner-status.v1.schema.json": M6OwnerStatus,
        "m6-owner-reply.v1.schema.json": M6OwnerReply,
    }
    result: dict[str, dict[str, Any]] = {}
    for filename, model in models.items():
        document = model.model_json_schema()
        document["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        document["$id"] = "urn:disclosure-anchor:operational:" + filename
        result[filename] = document
    return result
