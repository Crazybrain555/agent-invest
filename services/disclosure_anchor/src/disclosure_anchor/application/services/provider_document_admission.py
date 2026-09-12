"""Single source-admission boundary for parse-owned provider documents."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
    ParserTargetIdentityError,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    AdmittedProviderDocument,
    ProviderDocumentAdmissionError,
)
from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelope,
    provider_document_envelope_from_bytes,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.application.ports.provider_document_source import (
    ProviderDocumentSourceError,
    ProviderDocumentSourcePort,
)
from disclosure_anchor.application.services.provider_source_semantics import (
    derive_source_semantics,
)
from disclosure_anchor.domain import entities as e


class ProviderDocumentAdmission:
    """Rebuild one exact MinerU bundle before exposing its typed projection."""

    def __init__(
        self,
        *,
        path_builder: FileStorePathPort,
        source: ProviderDocumentSourcePort,
    ) -> None:
        self._paths = path_builder
        self._source = source

    def admit(
        self,
        *,
        document: e.Document,
        run: e.ProcessingRun,
        artifact_owner: e.ProcessingRun,
        security_code: str,
    ) -> AdmittedProviderDocument:
        """Admit one provider parse/rebuild through its self-owned parse root."""

        self._validate_run(
            document=document,
            run=run,
            artifact_owner=artifact_owner,
        )
        provider_document_relpath = Path(
            _required(run.provider_document_relpath, "provider document path")
        )
        provider = _required(document.provider, "document provider")
        provider_document_id = _required(
            document.provider_document_id,
            "provider document id",
        )
        source_pdf_relpath = _required(
            document.raw_file_relpath,
            "source PDF path",
        )
        source_pdf_sha256 = _required(
            document.raw_file_hash,
            "source PDF hash",
        )
        if not security_code:
            self._fail("document_identity_invalid", "security code is missing")
        source_parts = PurePosixPath(source_pdf_relpath).parts
        if len(source_parts) < 3 or source_parts[2] != security_code:
            self._fail(
                "document_identity_invalid",
                "source PDF path differs from the document security code",
            )

        expected_record_relpath = self._paths.provider_document_relpath(
            provider=provider,
            security_code=security_code,
            provider_document_id=provider_document_id,
            artifact_owner_processing_run_id=(artifact_owner.processing_run_id),
        )
        if provider_document_relpath != expected_record_relpath:
            self._fail(
                "provider_document_path_mismatch",
                "provider document path differs from the canonical owner path",
            )

        try:
            record_bytes = self._source.read_provider_document_record(
                provider_document_relpath
            )
        except ProviderDocumentSourceError as exc:
            raise ProviderDocumentAdmissionError(
                exc.reason_code,
                str(exc),
                retryable=exc.retryable,
            ) from exc
        record_sha256 = _sha256(record_bytes)
        if record_sha256 != artifact_owner.artifact_hash:
            self._fail(
                "provider_document_hash_mismatch",
                "provider document bytes differ from the processing run hash",
            )
        try:
            envelope = provider_document_envelope_from_bytes(record_bytes)
        except ValueError as exc:
            raise ProviderDocumentAdmissionError(
                "provider_document_contract_invalid",
                str(exc),
            ) from exc

        target = self._run_target(artifact_owner)
        expected_facts = {
            "document_id": document.document_id,
            "artifact_owner_processing_run_id": (
                artifact_owner.processing_run_id
            ),
            "provider": provider,
            "provider_document_id": provider_document_id,
            "source_pdf_relpath": source_pdf_relpath,
            "input_raw_file_hash": source_pdf_sha256,
            "parser_artifact_root_relpath": _required(
                artifact_owner.parser_artifact_relpath,
                "parser artifact root",
            ),
            "parser_target_identity": target,
        }
        for field, expected in expected_facts.items():
            if getattr(envelope, field) != expected:
                self._fail(
                    "provider_document_identity_mismatch",
                    f"provider document field {field} differs from its owner",
                )

        try:
            source_observation = self._source.observe_source_pdf(
                Path(source_pdf_relpath)
            )
        except ProviderDocumentSourceError as exc:
            raise ProviderDocumentAdmissionError(
                exc.reason_code,
                str(exc),
                retryable=exc.retryable,
            ) from exc
        if (
            source_observation.sha256 != envelope.input_raw_file_hash
            or source_observation.page_count != envelope.source_pdf_page_count
        ):
            self._fail(
                "source_pdf_identity_mismatch",
                "source PDF bytes or page count differ from the provider record",
            )

        try:
            rebuilt = self._source.rebuild_provider_document(
                Path(envelope.parser_artifact_root_relpath),
                source_pdf_sha256=envelope.input_raw_file_hash,
            )
        except ProviderDocumentSourceError as exc:
            raise ProviderDocumentAdmissionError(
                exc.reason_code,
                str(exc),
                retryable=exc.retryable,
            ) from exc
        if rebuilt != envelope.provider_document:
            self._fail(
                "provider_document_projection_mismatch",
                "provider document projection differs from the frozen MinerU bundle",
            )
        return self._admit_verified_projection(
            provider_document_relpath=provider_document_relpath,
            provider_document_sha256=record_sha256,
            envelope=envelope,
            provider_document=rebuilt,
            source_pdf_relpath=Path(source_pdf_relpath),
        )

    def admit_materialized(
        self,
        *,
        document: e.Document,
        envelope: ProviderDocumentEnvelope,
        provider_document_sha256: str,
        expected_source_byte_count: int,
        security_code: str,
    ) -> AdmittedProviderDocument:
        """Admit an already V4-verified materialized provider envelope.

        The staged path has not yet selected transaction P, so its candidate
        processing run is deliberately still ``running`` and the provider
        record has not been promoted into the durable derived namespace.  This
        entry point validates that exact in-memory envelope and then delegates
        to the same native-PDF reconciliation/quality projection used by the
        post-publication ``admit`` path.
        """

        if type(envelope) is not ProviderDocumentEnvelope:
            self._fail(
                "provider_document_contract_invalid",
                "materialized provider envelope is not exact",
            )
        provider = _required(document.provider, "document provider")
        provider_document_id = _required(
            document.provider_document_id,
            "provider document id",
        )
        source_pdf_relpath = _required(document.raw_file_relpath, "source PDF path")
        source_pdf_sha256 = _required(document.raw_file_hash, "source PDF hash")
        if not security_code:
            self._fail("document_identity_invalid", "security code is missing")
        source_parts = PurePosixPath(source_pdf_relpath).parts
        if len(source_parts) < 3 or source_parts[2] != security_code:
            self._fail(
                "document_identity_invalid",
                "source PDF path differs from the document security code",
            )
        expected_envelope_facts = {
            "document_id": document.document_id,
            "provider": provider,
            "provider_document_id": provider_document_id,
            "source_pdf_relpath": source_pdf_relpath,
            "input_raw_file_hash": source_pdf_sha256,
        }
        for field, expected in expected_envelope_facts.items():
            if getattr(envelope, field) != expected:
                self._fail(
                    "provider_document_identity_mismatch",
                    f"provider document field {field} differs from its source",
                )
        record_sha256 = _sha256(provider_document_envelope_to_bytes(envelope))
        if record_sha256 != provider_document_sha256:
            self._fail(
                "provider_document_hash_mismatch",
                "materialized provider document bytes differ from its receipt",
            )
        try:
            source_observation = self._source.observe_source_pdf(
                Path(source_pdf_relpath)
            )
        except ProviderDocumentSourceError as exc:
            raise ProviderDocumentAdmissionError(
                exc.reason_code,
                str(exc),
                retryable=exc.retryable,
            ) from exc
        if (
            source_observation.sha256 != envelope.input_raw_file_hash
            or source_observation.byte_count != expected_source_byte_count
            or source_observation.page_count != envelope.source_pdf_page_count
        ):
            self._fail(
                "source_pdf_identity_mismatch",
                "source PDF bytes, byte count, or page count differ from the provider record",
            )
        provider_document_relpath = self._paths.provider_document_relpath(
            provider=provider,
            security_code=security_code,
            provider_document_id=provider_document_id,
            artifact_owner_processing_run_id=(
                envelope.artifact_owner_processing_run_id
            ),
        )
        return self._admit_verified_projection(
            provider_document_relpath=provider_document_relpath,
            provider_document_sha256=record_sha256,
            envelope=envelope,
            provider_document=envelope.provider_document,
            source_pdf_relpath=Path(source_pdf_relpath),
        )

    def _admit_verified_projection(
        self,
        *,
        provider_document_relpath: Path,
        provider_document_sha256: str,
        envelope: ProviderDocumentEnvelope,
        provider_document: ProviderDocument,
        source_pdf_relpath: Path,
    ) -> AdmittedProviderDocument:
        """Apply the sole source-text reconciliation and quality semantics."""

        try:
            native_text = self._source.observe_source_pdf_text(
                source_pdf_relpath,
                document=provider_document,
                expected_sha256=envelope.input_raw_file_hash,
            )
        except ProviderDocumentSourceError as exc:
            raise ProviderDocumentAdmissionError(
                exc.reason_code,
                str(exc),
                retryable=exc.retryable,
            ) from exc
        try:
            semantics = derive_source_semantics(
                document=provider_document, observations=native_text
            )
        except ValueError as exc:
            raise ProviderDocumentAdmissionError(
                "source_pdf_text_contract_invalid",
                str(exc),
            ) from exc
        return AdmittedProviderDocument(
            provider_document_relpath=provider_document_relpath,
            provider_document_sha256=provider_document_sha256,
            envelope=envelope,
            source_text_reconciliations=semantics.source_text_reconciliations,
            source_quality_findings=semantics.source_quality_findings,
        )

    def _validate_run(
        self,
        *,
        document: e.Document,
        run: e.ProcessingRun,
        artifact_owner: e.ProcessingRun,
    ) -> None:
        if (
            run.document_id != document.document_id
            or run.run_kind not in {"parse", "rebuild_units"}
            or run.status != "succeeded"
        ):
            self._fail(
                "parse_owner_invalid",
                "provider document admission requires a succeeded provider run",
            )
        if (
            run.normalized_ir_relpath is not None
            or run.provider_document_relpath is None
        ):
            self._fail(
                "run_output_contract_unsupported",
                "legacy, missing, or dual-tagged output cannot enter admission",
            )
        if run.input_raw_file_hash != document.raw_file_hash:
            self._fail(
                "parse_owner_identity_mismatch",
                "parse owner input hash differs from the document",
            )
        if not run.artifact_hash:
            self._fail(
                "parse_owner_identity_mismatch",
                "parse owner has no provider document hash",
            )
        if (
            artifact_owner.document_id != document.document_id
            or artifact_owner.run_kind != "parse"
            or artifact_owner.status != "succeeded"
            or artifact_owner.artifact_owner_processing_run_id
            != artifact_owner.processing_run_id
            or artifact_owner.normalized_ir_relpath is not None
            or artifact_owner.provider_document_relpath is None
            or run.artifact_owner_processing_run_id
            != artifact_owner.processing_run_id
        ):
            self._fail(
                "parse_owner_invalid",
                "provider run does not reference a succeeded self-owned parse",
            )
        copied_fields = (
            "input_raw_file_hash",
            "parser_artifact_relpath",
            "artifact_hash",
            "provider_document_relpath",
            "parser_name",
            "parser_version",
            "parser_backend",
            "parser_method",
            "parser_language",
            "parser_target_identity",
        )
        for field in copied_fields:
            if getattr(run, field) != getattr(artifact_owner, field):
                self._fail(
                    "parse_owner_identity_mismatch",
                    f"provider run field {field} differs from its parse owner",
                )

    def _run_target(self, run: e.ProcessingRun) -> ParserTargetIdentity:
        try:
            target = ParserTargetIdentity.from_payload(run.parser_target_identity)
        except ParserTargetIdentityError as exc:
            raise ProviderDocumentAdmissionError(
                "parser_target_identity_invalid",
                str(exc),
            ) from exc
        run_fields = {
            "name": run.parser_name,
            "package_version": run.parser_version,
            "backend": run.parser_backend,
            "method": run.parser_method,
            "language": run.parser_language,
        }
        for field, actual in run_fields.items():
            if actual != getattr(target, field):
                self._fail(
                    "parser_target_identity_mismatch",
                    f"processing run field {field} differs from parser target",
                )
        return target

    @staticmethod
    def _fail(reason_code: str, message: str) -> None:
        raise ProviderDocumentAdmissionError(reason_code, message)


def _required(value: str | None, label: str) -> str:
    if not value:
        raise ProviderDocumentAdmissionError(
            "document_identity_invalid",
            f"{label} is missing",
        )
    return value


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = ["ProviderDocumentAdmission"]
