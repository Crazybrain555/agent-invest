"""Parse a registered raw document into one provider-native source record."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Any, cast

from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
    ParserTargetIdentityError,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelope,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    SourcePdfObservation,
)
from disclosure_anchor.application.ports.file_store import (
    ArtifactStorePort,
    FileStorePathPort,
    RawDocumentStorePort,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.ports.provider_document_source import (
    ProviderDocumentSourceError,
    ProviderDocumentSourcePort,
)
from disclosure_anchor.application.ports.provider_parser import (
    ProviderDocumentParserPort,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.worker.locks import (
    exclusive_document_producer,
    maybe_lock_document,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.entities import outbox_events
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import (
    ParseDocumentError,
    ParserError,
    ParserBackendOverloadedError,
    ParserCancelledError,
    ParserInvocationError,
    ParserLocalInvocationError,
    ParserOutputContractError,
    ParserTaskDeadlineError,
    ParserTaskError,
    ParserTimeoutError,
    ParserUnknownError,
    ParserVersionProbeError,
    ParserRetryBudgetClass,
)


@dataclass(frozen=True)
class ParseDocumentCommand:
    document_id: str
    options: ParserOptions


@dataclass(frozen=True)
class ParseDocumentResult:
    processing_run_id: str
    status: str
    parser_artifact_relpath: str | None = None
    normalized_ir_relpath: str | None = None
    provider_document_relpath: str | None = None
    artifact_hash: str | None = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True)
class _ParseRunFailure(Exception):
    stage: str
    error_code: str
    retryable: bool
    retry_budget_class: ParserRetryBudgetClass
    message: str


class ParseDocument:
    """Use case for the Phase 04 parse step."""

    def __init__(
        self,
        *,
        parser: ProviderDocumentParserPort,
        provider_source: ProviderDocumentSourcePort,
        path_builder: FileStorePathPort,
        raw_store: RawDocumentStorePort,
        artifact_store: ArtifactStorePort,
        uow_factory: Callable[[], UnitOfWork],
        default_timeout_seconds: int = 1800,
        check_readiness: bool = True,
    ) -> None:
        self._parser = parser
        self._provider_source = provider_source
        self._paths = path_builder
        self._raw_store = raw_store
        self._artifact_store = artifact_store
        self._uow_factory = uow_factory
        self._default_timeout_seconds = default_timeout_seconds
        self._check_readiness = check_readiness

    def execute(self, command: ParseDocumentCommand) -> ParseDocumentResult:
        # The producer UoW owns CORPUS(shared) and
        # DOCUMENT_PRODUCER(exclusive) for one lifecycle. Both leases span
        # parser filesystem writes and both DB transactions,
        # so destructive maintenance cannot unlink an in-flight parse and
        # another producer cannot race the same document lifecycle.
        with exclusive_document_producer(
            self._uow_factory,
            command.document_id,
        ):
            return self._execute_owned(command)

    def _execute_owned(self, command: ParseDocumentCommand) -> ParseDocumentResult:
        options = self._effective_options(command.options)
        context = self._prepare_run(command.document_id, options)
        if context.get("prepare_failed"):
            run = context["run"]
            return ParseDocumentResult(
                processing_run_id=run.processing_run_id,
                status=run.status,
                parser_artifact_relpath=run.parser_artifact_relpath,
                normalized_ir_relpath=run.normalized_ir_relpath,
                provider_document_relpath=run.provider_document_relpath,
                artifact_hash=run.artifact_hash,
                error=run.error,
            )
        try:
            source_observation = self._verify_raw_document(context)
            parser_result = self._parser.parse(
                input_pdf=context["input_pdf"],
                output_dir=context["artifact_root_path"],
                options=options,
                source_pdf_sha256=source_observation.sha256,
            )
            expected_target = context["parser_target_identity"]
            if (
                not isinstance(expected_target, ParserTargetIdentity)
                or parser_result.target_identity != expected_target
            ):
                raise ParserOutputContractError(
                    "parser result target identity differs from the prepared run"
                )
            artifact_root_relpath = self._artifact_root_relpath(
                artifact_root_relpath=context["artifact_root_relpath"],
                artifact_root_path=context["artifact_root_path"],
                artifact_root=parser_result.artifact_root,
            )
            document = context["document"]
            envelope = ProviderDocumentEnvelope.build(
                document_id=document.document_id,
                artifact_owner_processing_run_id=(
                    context["processing_run_id"]
                ),
                provider=cast(str, document.provider),
                provider_document_id=cast(str, document.provider_document_id),
                source_pdf_relpath=cast(str, document.raw_file_relpath),
                source_pdf_page_count=source_observation.page_count,
                parser_artifact_root_relpath=artifact_root_relpath.as_posix(),
                parser_target_identity=expected_target,
                provider_document=parser_result.provider_document,
            )
            record_bytes = provider_document_envelope_to_bytes(envelope)
            provider_result = self._artifact_store.write_text_atomic(
                relpath=context["provider_document_relpath"],
                text=record_bytes.decode("utf-8"),
            )
        except (
            _ParseRunFailure,
            ParserTimeoutError,
            ParserInvocationError,
            ParserVersionProbeError,
            ParserOutputContractError,
            ParserUnknownError,
        ) as exc:
            failure = (
                exc
                if isinstance(exc, _ParseRunFailure)
                else self._parser_failure_from_exception(exc)
            )
            run = self._finish_failed_run(context=context, failure=failure)
            return ParseDocumentResult(
                processing_run_id=run.processing_run_id,
                status=run.status,
                parser_artifact_relpath=run.parser_artifact_relpath,
                normalized_ir_relpath=run.normalized_ir_relpath,
                provider_document_relpath=run.provider_document_relpath,
                artifact_hash=run.artifact_hash,
                error=run.error,
            )
        except OSError as exc:
            # Local storage/path failures are shared infrastructure, not a
            # malformed PDF. Preserve the failed run for observability, keep
            # it retryable, and let the worker close rolling admission before
            # the same outage is multiplied across the candidate window.
            run = self._finish_failed_run(
                context=context,
                failure=_ParseRunFailure(
                    stage="parse_io",
                    error_code="OSError",
                    retryable=True,
                    retry_budget_class="infrastructure",
                    message=str(exc),
                ),
            )
            return ParseDocumentResult(
                processing_run_id=run.processing_run_id,
                status=run.status,
                parser_artifact_relpath=run.parser_artifact_relpath,
                normalized_ir_relpath=run.normalized_ir_relpath,
                provider_document_relpath=run.provider_document_relpath,
                artifact_hash=run.artifact_hash,
                error=run.error,
            )
        except Exception as exc:
            self._finish_failed_run(
                context=context,
                failure=_ParseRunFailure(
                    stage="parse",
                    error_code=exc.__class__.__name__,
                    retryable=False,
                    retry_budget_class="item",
                    message=str(exc),
                ),
            )
            raise

        run = self._finish_run(
            processing_run_id=context["processing_run_id"],
            status="succeeded",
            input_raw_file_hash=context["document"].raw_file_hash,
            parser_artifact_relpath=str(artifact_root_relpath),
            provider_document_relpath=str(context["provider_document_relpath"]),
            artifact_hash=provider_result.artifact_hash,
        )
        return ParseDocumentResult(
            processing_run_id=run.processing_run_id,
            status=run.status,
            parser_artifact_relpath=run.parser_artifact_relpath,
            normalized_ir_relpath=run.normalized_ir_relpath,
            provider_document_relpath=run.provider_document_relpath,
            artifact_hash=run.artifact_hash,
        )

    def _effective_options(self, options: ParserOptions) -> ParserOptions:
        if options.timeout_seconds is not None:
            return options
        return replace(options, timeout_seconds=self._default_timeout_seconds)

    def _prepare_run(self, document_id: str, options: ParserOptions) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        processing_run_id = ids.new_processing_run_id()
        with self._uow_factory() as uow:
            document = uow.documents.get(document_id)
            if document is None:
                raise ParseDocumentError(f"document not found: {document_id}")
            self._validate_document(document)
            # Lock ordering discipline (round23 review S2): every transaction
            # that touches both the document row and the outbox takes the
            # document lock FIRST (finish/publish/register all do). The
            # prepare failure branch updates document.status after its first
            # outbox insert, which inverted the order and could deadlock
            # against a concurrent finisher on the same document.
            maybe_lock_document(uow, document_id)
            security = (
                uow.securities.get(document.security_id)
                if document.security_id is not None
                else None
            )
            if security is None:
                raise ParseDocumentError(
                    f"document security not found: {document.security_id}"
                )

            provider = cast(str, document.provider)
            provider_document_id = cast(str, document.provider_document_id)
            artifact_root_relpath = self._paths.parser_run_artifacts_relpath(
                provider=provider,
                security_code=security.security_code,
                provider_document_id=provider_document_id,
                processing_run_id=processing_run_id,
            )
            provider_document_relpath = self._paths.provider_document_relpath(
                provider=provider,
                security_code=security.security_code,
                provider_document_id=provider_document_id,
                artifact_owner_processing_run_id=processing_run_id,
            )
            prepare_error: dict[str, Any] | None = None
            parser_target_identity: ParserTargetIdentity | None = None
            try:
                identity = self._parser.identity()
                parser_name = identity.name
                parser_version = identity.version
            except (ParserVersionProbeError, ParserOutputContractError) as exc:
                parser_name = self._parser.__class__.__name__
                parser_version = None
                prepare_error = self._structured_error(
                    stage="parser_identity",
                    error_code=(
                        "parser_version_probe_failed"
                        if isinstance(exc, ParserVersionProbeError)
                        else "parser_output_contract_failed"
                    ),
                    # Parser identity is process/configuration health, not a
                    # property of this PDF. Keep the item retryable so a
                    # repaired binary/service can resume after worker cooldown.
                    retryable=isinstance(exc, ParserVersionProbeError),
                    retry_budget_class="infrastructure",
                    message=str(exc),
                )
            else:
                try:
                    parser_target_identity = options.target_identity(identity)
                except ParserTargetIdentityError as exc:
                    parser_target_identity = None
                    prepare_error = self._structured_error(
                        stage="parser_identity",
                        error_code="parser_target_identity_invalid",
                        retryable=False,
                        retry_budget_class="infrastructure",
                        message=str(exc),
                    )
                # Identity is stable package/configuration metadata. Runtime
                # readiness is an admission check and must happen before the
                # parser consumes this document, never after successful
                # artifacts have already been produced.
                readiness = getattr(self._parser, "readiness", None)
                if (
                    prepare_error is None
                    and self._check_readiness
                    and callable(readiness)
                ):
                    try:
                        readiness(options)
                    except (ParserVersionProbeError, ParserOutputContractError) as exc:
                        prepare_error = self._structured_error(
                            stage="parser_readiness",
                            error_code=(
                                "parser_readiness_failed"
                                if isinstance(exc, ParserVersionProbeError)
                                else "parser_output_contract_failed"
                            ),
                            retryable=isinstance(exc, ParserVersionProbeError),
                            retry_budget_class="infrastructure",
                            message=str(exc),
                        )
            run = uow.processing_runs.add(
                e.ProcessingRun(
                    processing_run_id=processing_run_id,
                    document_id=document.document_id,
                    artifact_owner_processing_run_id=processing_run_id,
                    run_kind="parse",
                    status="failed" if prepare_error else "running",
                    parser_name=parser_name,
                    parser_version=parser_version,
                    parser_backend=options.backend,
                    parser_method=options.method,
                    parser_language=options.language,
                    parser_target_identity=(
                        parser_target_identity.to_payload()
                        if parser_target_identity is not None
                        else None
                    ),
                    input_raw_file_hash=document.raw_file_hash,
                    parser_artifact_relpath=str(artifact_root_relpath),
                    normalized_ir_relpath=None,
                    provider_document_relpath=str(provider_document_relpath),
                    started_at=now,
                    finished_at=now if prepare_error else None,
                    error=prepare_error,
                    is_active=False,
                )
            )
            uow.outbox.add(
                outbox_events.processing_run_created(
                    document_id=document.document_id,
                    processing_run_id=run.processing_run_id,
                    occurred_at=now,
                )
            )
            if prepare_error is not None:
                self._update_document_status_for_run(
                    uow=uow, document_id=document.document_id, status="failed"
                )
                uow.outbox.add(
                    outbox_events.processing_run_failed(
                        document_id=document.document_id,
                        processing_run_id=run.processing_run_id,
                        error=prepare_error,
                        occurred_at=now,
                    )
                )
            uow.commit()

        return {
            "document": document,
            "run": run,
            "prepare_failed": prepare_error is not None,
            "processing_run_id": run.processing_run_id,
            "input_pdf": self._paths.data_path(
                Path(cast(str, document.raw_file_relpath))
            ),
            "artifact_root_relpath": artifact_root_relpath,
            "artifact_root_path": self._paths.data_path(artifact_root_relpath),
            "provider_document_relpath": provider_document_relpath,
            "parser_target_identity": parser_target_identity,
            "document_metadata": {
                "document_id": document.document_id,
                "title": document.title,
                "source_pdf": document.raw_file_relpath,
                "provider": document.provider,
                "provider_document_id": document.provider_document_id,
                "raw_file_hash": document.raw_file_hash,
            },
        }

    def _validate_document(self, document: e.Document) -> None:
        missing = [
            name
            for name in (
                "provider",
                "provider_document_id",
                "security_id",
                "raw_file_relpath",
                "raw_file_hash",
            )
            if not getattr(document, name)
        ]
        if missing:
            raise ParseDocumentError(
                f"document {document.document_id} missing parse metadata: {missing}"
            )

    def _verify_raw_document(
        self, context: dict[str, Any]
    ) -> SourcePdfObservation:
        document = context["document"]
        verification = self._raw_store.verify_raw_document(
            relpath=Path(document.raw_file_relpath),
            expected_hash=document.raw_file_hash,
        )
        if not verification.ok:
            error_code = (
                "raw_missing"
                if verification.actual_hash is None
                else "raw_hash_mismatch"
            )
            raise _ParseRunFailure(
                stage="raw_verification",
                error_code=error_code,
                retryable=False,
                retry_budget_class="item",
                message=verification.message,
            )
        try:
            observation = self._provider_source.observe_source_pdf(
                Path(document.raw_file_relpath)
            )
        except ProviderDocumentSourceError as exc:
            raise _ParseRunFailure(
                stage="raw_verification",
                error_code=exc.reason_code,
                retryable=exc.retryable,
                retry_budget_class=(
                    "infrastructure" if exc.retryable else "item"
                ),
                message=str(exc),
            ) from exc
        if observation.sha256 != document.raw_file_hash:
            raise _ParseRunFailure(
                stage="raw_verification",
                error_code="raw_hash_mismatch",
                retryable=False,
                retry_budget_class="item",
                message=(
                    "independent source observation differs from the registered "
                    "raw file hash"
                ),
            )
        return observation

    def _artifact_root_relpath(
        self,
        *,
        artifact_root_relpath: Path,
        artifact_root_path: Path,
        artifact_root: Path,
    ) -> Path:
        try:
            output_root = artifact_root_path.resolve(strict=True)
            parser_root = artifact_root.resolve(strict=True)
            relative = parser_root.relative_to(output_root)
        except FileNotFoundError as exc:
            raise ParserOutputContractError(
                f"parser artifact root does not exist: {artifact_root}"
            ) from exc
        except ValueError as exc:
            raise ParserOutputContractError(
                f"parser artifact root escapes its run directory: {artifact_root}"
            ) from exc
        if not parser_root.is_dir():
            raise ParserOutputContractError(
                f"parser artifact root is not a directory: {artifact_root}"
            )
        return artifact_root_relpath / relative

    def _finish_run(
        self,
        *,
        processing_run_id: str,
        status: str,
        input_raw_file_hash: str | None = None,
        parser_artifact_relpath: str | None = None,
        provider_document_relpath: str | None = None,
        artifact_hash: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> e.ProcessingRun:
        with self._uow_factory() as uow:
            run = uow.processing_runs.get(processing_run_id)
            if run is None:
                raise ParseDocumentError(
                    f"processing run not found: {processing_run_id}"
                )
            maybe_lock_document(uow, run.document_id)
            if run.status != "running":
                # First terminal state wins: the stale reclaimer (or a
                # duplicate finisher) already closed this run, and a retry
                # run may exist for the document. A late failure is
                # idempotent; a late success must not resurrect the run and
                # race the retry (round23).
                if status == "failed":
                    return run
                raise ParseDocumentError(
                    f"processing run {processing_run_id} is already "
                    f"{run.status}; discarding late success (stale reclaim "
                    "or duplicate finisher won)"
                )
            run.status = status
            run.input_raw_file_hash = input_raw_file_hash or run.input_raw_file_hash
            run.parser_artifact_relpath = (
                parser_artifact_relpath or run.parser_artifact_relpath
            )
            run.provider_document_relpath = (
                provider_document_relpath or run.provider_document_relpath
            )
            run.artifact_hash = artifact_hash or run.artifact_hash
            run.error = error
            finished_at = datetime.now(timezone.utc)
            run.finished_at = finished_at
            updated = uow.processing_runs.update(run)
            self._update_document_status_for_run(
                uow=uow, document_id=run.document_id, status=status
            )
            if status == "failed" and error is not None:
                uow.outbox.add(
                    outbox_events.processing_run_failed(
                        document_id=run.document_id,
                        processing_run_id=run.processing_run_id,
                        error=error,
                        occurred_at=finished_at,
                    )
                )
            uow.commit()
            return updated

    def _finish_failed_run(
        self, *, context: dict[str, Any], failure: _ParseRunFailure
    ) -> e.ProcessingRun:
        return self._finish_run(
            processing_run_id=context["processing_run_id"],
            status="failed",
            error=self._structured_error(
                stage=failure.stage,
                error_code=failure.error_code,
                retryable=failure.retryable,
                retry_budget_class=failure.retry_budget_class,
                message=failure.message,
            ),
        )

    def _update_document_status_for_run(
        self, *, uow: UnitOfWork, document_id: str, status: str
    ) -> None:
        document = uow.documents.get(document_id)
        if document is None or document.current_processing_run_id is not None:
            return
        if status == "succeeded":
            document.status = "parsed"
        elif status == "failed":
            document.status = "parse_failed"
        else:
            return
        uow.documents.update(document)

    def _structured_error(
        self,
        *,
        stage: str,
        error_code: str,
        retryable: bool,
        retry_budget_class: ParserRetryBudgetClass,
        message: str,
    ) -> dict[str, Any]:
        return {
            "stage": stage,
            "error_code": error_code,
            "retryable": retryable,
            "retry_budget_class": retry_budget_class,
            "message": message,
        }

    def _parser_failure_from_exception(self, exc: Exception) -> _ParseRunFailure:
        if not isinstance(exc, ParserError):
            raise TypeError(f"unsupported parser failure type: {type(exc)!r}")

        def typed(
            stage: str,
            error_code: str,
            *,
            retryable: bool,
        ) -> _ParseRunFailure:
            return _ParseRunFailure(
                stage=stage,
                error_code=error_code,
                retryable=retryable,
                retry_budget_class=exc.retry_budget_class,
                message=str(exc),
            )

        if isinstance(exc, ParserTimeoutError):
            return typed("parse", "parse_timeout", retryable=True)
        if isinstance(exc, ParserTaskDeadlineError):
            return typed(
                "parse",
                "parser_task_deadline_exceeded",
                retryable=True,
            )
        if isinstance(exc, ParserBackendOverloadedError):
            return typed("parse", "parser_backend_overloaded", retryable=True)
        if isinstance(exc, ParserCancelledError):
            return typed("parse", "parser_cancelled", retryable=True)
        if isinstance(exc, ParserTaskError):
            return typed("parse", "parser_task_failed", retryable=True)
        if isinstance(exc, ParserLocalInvocationError):
            return typed(
                "parse",
                "parser_local_invocation_failed",
                retryable=True,
            )
        if isinstance(exc, ParserInvocationError):
            return typed("parse", "parser_invocation_failed", retryable=True)
        if isinstance(exc, ParserVersionProbeError):
            return typed(
                "parser_identity",
                "parser_version_probe_failed",
                retryable=True,
            )
        if isinstance(exc, ParserOutputContractError):
            return typed(
                "parse_output",
                "parser_output_contract_failed",
                retryable=False,
            )
        if isinstance(exc, ParserUnknownError):
            return typed("parse", "parser_unknown_failed", retryable=False)
        raise TypeError(f"unsupported parser failure type: {type(exc)!r}")


_ARTIFACT_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def build_parser_artifact_manifest(
    *,
    artifact_root: Path,
    artifact_root_relpath: Path,
    artifact_paths: Mapping[str, Path | None],
) -> dict[str, Any]:
    """Hash a parser-neutral role map without inferring provider-specific roles."""

    if (
        artifact_root_relpath.is_absolute()
        or ".." in artifact_root_relpath.parts
        or not artifact_root_relpath.parts
    ):
        raise ParserOutputContractError("parser artifact root relpath is unsafe")
    if not artifact_paths:
        raise ParserOutputContractError("parser emitted no artifact roles")
    try:
        resolved_root = artifact_root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ParserOutputContractError(
            f"parser artifact root does not exist: {artifact_root}"
        ) from exc
    if not resolved_root.is_dir():
        raise ParserOutputContractError(
            f"parser artifact root is not a directory: {artifact_root}"
        )

    files: dict[str, dict[str, Any]] = {}
    for role in sorted(artifact_paths):
        if not isinstance(role, str) or _ARTIFACT_ROLE_RE.fullmatch(role) is None:
            raise ParserOutputContractError(f"parser artifact role is unsafe: {role!r}")
        path = artifact_paths[role]
        if path is None:
            files[role] = {"availability": "not_emitted"}
            continue
        if not isinstance(path, Path):
            raise ParserOutputContractError(
                f"parser artifact {role!r} path is not a pathlib.Path"
            )
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(resolved_root)
        except FileNotFoundError as exc:
            raise ParserOutputContractError(
                f"parser artifact {role!r} does not exist: {path}"
            ) from exc
        except ValueError as exc:
            raise ParserOutputContractError(
                f"parser artifact {role!r} escapes artifact root: {path}"
            ) from exc
        if not relative.parts or not resolved.is_file():
            raise ParserOutputContractError(
                f"parser artifact {role!r} is not a file below artifact root: {path}"
            )
        with resolved.open("rb") as artifact_file:
            digest = hashlib.file_digest(artifact_file, "sha256").hexdigest()
        files[role] = {
            "availability": "present",
            "relpath": str(artifact_root_relpath / relative),
            "sha256": f"sha256:{digest}",
            "size_bytes": resolved.stat().st_size,
        }
    return {
        "artifact_root_relpath": str(artifact_root_relpath),
        "files": files,
    }
