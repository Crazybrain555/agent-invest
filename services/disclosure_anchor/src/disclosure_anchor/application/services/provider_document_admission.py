"""Single source-admission boundary for parse-owned provider documents."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import re

from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
    ParserTargetIdentityError,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    AdmittedProviderDocument,
    ProviderDocumentAdmissionError,
    SourcePdfTextObservation,
    SourceQualityFinding,
    SourceTextReconciliation,
)
from disclosure_anchor.application.contracts.html_visible_text import html_visible_text
from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.contracts.provider_document_envelope import (
    provider_document_envelope_from_bytes,
)
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.application.ports.provider_document_source import (
    ProviderDocumentSourceError,
    ProviderDocumentSourcePort,
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
        try:
            native_text = self._source.observe_source_pdf_text(
                Path(source_pdf_relpath),
                document=rebuilt,
                expected_sha256=envelope.input_raw_file_hash,
            )
        except ProviderDocumentSourceError as exc:
            raise ProviderDocumentAdmissionError(
                exc.reason_code,
                str(exc),
                retryable=exc.retryable,
            ) from exc
        try:
            reconciliations = _source_text_reconciliations(
                rebuilt,
                native_text,
            )
            quality_findings = _source_quality_findings(
                rebuilt,
                native_text,
                reconciliations=reconciliations,
            )
        except ValueError as exc:
            raise ProviderDocumentAdmissionError(
                "source_pdf_text_contract_invalid",
                str(exc),
            ) from exc
        return AdmittedProviderDocument(
            provider_document_relpath=provider_document_relpath,
            provider_document_sha256=record_sha256,
            envelope=envelope,
            source_text_reconciliations=reconciliations,
            source_quality_findings=quality_findings,
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


_NUMERIC_TOKEN_RE = re.compile(
    r"[+\-−]?(?:[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)"
)
_LINE_BREAK_RE = re.compile(r"\r\n")
_ADJACENT_LINE_BREAK_RE = re.compile(r"\r\n[^\S\r\n]*\r\n")
_BOUNDARY_LINE_BREAK_RE = re.compile(
    r"(?:\A[^\S\r\n]*\r\n|\r\n[^\S\r\n]*\Z)"
)
_PADDED_LINE_BREAK_RE = re.compile(r"(?:[^\S\r\n]\r\n|\r\n[^\S\r\n])")
_ASCII_WORD_CHAR_RE = re.compile(r"[A-Za-z0-9_]")


def _source_text_reconciliations(
    document: ProviderDocument,
    observations: tuple[SourcePdfTextObservation, ...],
) -> tuple[SourceTextReconciliation, ...]:
    identities = [
        (item.source_index, item.payload_ordinal) for item in observations
    ]
    if identities != sorted(identities) or len(identities) != len(set(identities)):
        raise ValueError("native source text observations are not unique and ordered")
    reconciliations: list[SourceTextReconciliation] = []
    blocks = document.blocks
    for observation in observations:
        if observation.source_index >= len(blocks):
            raise ValueError("native source text references an unknown block")
        block = blocks[observation.source_index]
        if (
            observation.page_index != block.page_index
            or observation.raw_block_sha256 != block.raw_item_sha256
            or observation.payload_ordinal >= len(block.payloads)
        ):
            raise ValueError("native source text differs from its provider block")
        payload = block.payloads[observation.payload_ordinal]
        if block.provider_type == "table" and payload.field == "table_body":
            continue
        if block.provider_type != "text" or payload.field != "text":
            raise ValueError("native source text references an unsupported payload")
        source_text = _numeric_source_replacement(
            provider_text=payload.text,
            native_text=observation.text,
        )
        source_kind = "source_pdf_native_numeric.v1"
        if source_text is None:
            source_text = _identifier_source_replacement(
                provider_text=payload.text,
                native_text=observation.text,
            )
            source_kind = "source_pdf_native_identifier.v1"
        if source_text is None:
            source_text = _identifier_v2_source_replacement(
                provider_text=payload.text,
                native_text=observation.text,
            )
            source_kind = "source_pdf_native_identifier.v2"
        if source_text is None:
            continue
        reconciliations.append(
            SourceTextReconciliation(
                source_index=block.source_index,
                payload_ordinal=observation.payload_ordinal,
                raw_block_sha256=block.raw_item_sha256,
                provider_text_sha256=_sha256(payload.text.encode("utf-8")),
                source_text_sha256=_sha256(source_text.encode("utf-8")),
                source_text=source_text,
                source_kind=source_kind,
            )
        )
    return tuple(reconciliations)


_TABLE_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TABLE_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_DIGIT_SEQUENCE_RE = re.compile(r"[0-9]+")
_USCC_CANDIDATE_RE = re.compile(r"(?<![0-9A-Z])[0-9A-Z]{18}(?![0-9A-Z])")
_USCC_ALPHABET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
_MALFORMED_GROUPING_DOT_RE = re.compile(
    r"(?<![0-9])(?:[0-9]{1,3},)*[0-9]{1,3}\.[0-9]{3},[0-9]{3}"
    r"\.[0-9]{2}(?![0-9])"
)
_MALFORMED_GROUPING_SPACE_RE = re.compile(
    r"(?<![0-9])[0-9]{1,3}(?:,[0-9]{3})+,(?:\s+[0-9]{3},)+"
    r"\s*[0-9]{3}\.[0-9]{2}(?![0-9])"
)


def _source_quality_findings(
    document: ProviderDocument,
    observations: tuple[SourcePdfTextObservation, ...],
    *,
    reconciliations: tuple[SourceTextReconciliation, ...],
) -> tuple[SourceQualityFinding, ...]:
    """Flag high-confidence native mismatch that cannot safely rewrite payload."""

    findings: list[SourceQualityFinding] = []
    repaired = {
        (item.source_index, item.payload_ordinal) for item in reconciliations
    }
    blocks = document.blocks
    for observation in observations:
        if observation.source_index >= len(blocks):
            raise ValueError("native source text references an unknown block")
        block = blocks[observation.source_index]
        if (
            observation.page_index != block.page_index
            or observation.raw_block_sha256 != block.raw_item_sha256
            or observation.payload_ordinal >= len(block.payloads)
        ):
            raise ValueError("native source text differs from its provider block")
        payload = block.payloads[observation.payload_ordinal]
        reason: str | None
        source_kind = "source_pdf_native_table_quality.v1"
        if block.provider_type == "table" and payload.field == "table_body":
            if _has_uscc_confusable_mismatch(
                provider_html=payload.text,
                native_text=observation.text,
            ):
                reason = "identifier_confusable_mismatch"
                source_kind = "source_pdf_native_identifier_quality.v1"
            else:
                reason = _table_quality_reason(
                    provider_html=payload.text,
                    native_text=observation.text,
                )
        elif block.provider_type == "text" and payload.field == "text":
            if (observation.source_index, observation.payload_ordinal) in repaired:
                continue
            if _has_cjk_bracket_omission(
                provider_text=payload.text,
                native_text=observation.text,
            ):
                reason = "cjk_bracket_omission"
                source_kind = "source_pdf_native_text_quality.v2"
            else:
                reason = _text_quality_reason(
                    provider_text=payload.text,
                    native_text=observation.text,
                )
                source_kind = "source_pdf_native_text_quality.v1"
        else:
            continue
        if reason is None:
            continue
        findings.append(
            SourceQualityFinding(
                source_index=block.source_index,
                payload_ordinal=observation.payload_ordinal,
                raw_block_sha256=block.raw_item_sha256,
                provider_text_sha256=_sha256(payload.text.encode("utf-8")),
                source_text_sha256=_sha256(observation.text.encode("utf-8")),
                reason=reason,
                source_kind=source_kind,
            )
        )
    return tuple(findings)


def _has_uscc_confusable_mismatch(
    *,
    provider_html: str,
    native_text: str,
) -> bool:
    """Flag one invalid O/0 substitution in a typed two-cell USCC row."""

    provider_codes: list[str] = []
    for row in _TABLE_ROW_RE.findall(provider_html):
        cells = tuple(html_visible_text(cell).strip() for cell in _TABLE_CELL_RE.findall(row))
        if len(cells) != 2 or cells[0] != "统一社会信用代码":
            continue
        provider_codes.extend(_USCC_CANDIDATE_RE.findall(cells[1].upper()))
    native_codes = _USCC_CANDIDATE_RE.findall(native_text.upper())
    if len(provider_codes) != 1 or len(native_codes) != 1:
        return False
    provider = provider_codes[0]
    native = native_codes[0]
    differences = tuple(
        (left, right)
        for left, right in zip(provider, native, strict=True)
        if left != right
    )
    return (
        len(differences) == 1
        and set(differences[0]) == {"0", "O"}
        and not _valid_uscc(provider)
        and _valid_uscc(native)
    )


def _valid_uscc(value: str) -> bool:
    if len(value) != 18 or any(character not in _USCC_ALPHABET for character in value):
        return False
    total = sum(
        _USCC_ALPHABET.index(character) * weight
        for character, weight in zip(value[:17], _USCC_WEIGHTS, strict=True)
    )
    return value[-1] == _USCC_ALPHABET[(31 - total % 31) % 31]


def _has_cjk_bracket_omission(*, provider_text: str, native_text: str) -> bool:
    """Flag exactly one source-only 〔 or 〕 without rewriting the payload."""

    source = _canonical_native_text(native_text)
    if source is None or provider_text == source:
        return False
    if "".join(_DIGIT_SEQUENCE_RE.findall(provider_text)) != "".join(
        _DIGIT_SEQUENCE_RE.findall(source)
    ):
        return False
    if source.count("〔") != source.count("〕"):
        return False
    for index, character in enumerate(source):
        if character not in {"〔", "〕"}:
            continue
        if source[:index] + source[index + 1 :] != provider_text:
            continue
        if abs(provider_text.count("〔") - provider_text.count("〕")) != 1:
            continue
        return True
    return False


def _table_quality_reason(
    *,
    provider_html: str,
    native_text: str,
) -> str | None:
    provider_visible = html_visible_text(provider_html)
    provider_digits = _DIGIT_SEQUENCE_RE.findall(provider_visible)
    native_digits = _DIGIT_SEQUENCE_RE.findall(native_text)
    rows = _TABLE_ROW_RE.findall(provider_html)
    empty_tail = 0
    for row in reversed(rows):
        if html_visible_text(row).strip():
            break
        empty_tail += 1
    if empty_tail >= 2 and len(native_digits) > len(provider_digits):
        return "empty_table_tail"
    if (
        (
            _MALFORMED_GROUPING_DOT_RE.search(provider_visible) is not None
            and _MALFORMED_GROUPING_DOT_RE.search(native_text) is None
        )
        or (
            _MALFORMED_GROUPING_SPACE_RE.search(provider_visible) is not None
            and _MALFORMED_GROUPING_SPACE_RE.search(native_text) is None
        )
    ):
        return "malformed_numeric_grouping"
    if (
        len(provider_digits) >= 8
        and len(provider_digits) == len(native_digits)
        and provider_digits != native_digits
    ):
        return "numeric_token_mismatch"
    return None


def _numeric_source_replacement(
    *,
    provider_text: str,
    native_text: str,
) -> str | None:
    """Prefer native PDF text only when all nonnumeric content is identical."""

    provider = provider_text
    source = _canonical_native_text(native_text)
    if source is None or not provider.strip() or not source or provider == source:
        return None
    if not _is_numeric_deletion_only(provider, source):
        return None
    return source


_DIGIT_ATOM_RE = re.compile(r"[0-9]+")
_NUMERIC_ADJACENT_ASCII_SPACES_RE = re.compile(r"(?<=\d) +| +(?=\d)")
_OPENING_QUOTES = frozenset("“‘「『")
_CLOSING_QUOTES = frozenset("”’」』")


def _identifier_source_replacement(
    *,
    provider_text: str,
    native_text: str,
) -> str | None:
    """Recover omitted numeric identifiers with one source-proved opening quote."""

    source = _canonical_native_text(native_text)
    if source is None or not provider_text.strip() or not source or provider_text == source:
        return None
    provider_atoms = _DIGIT_ATOM_RE.findall(provider_text)
    source_atoms = _DIGIT_ATOM_RE.findall(source)
    if (
        len(source_atoms) <= len(provider_atoms)
        or not _is_ordered_subsequence(provider_atoms, source_atoms)
    ):
        return None
    provider_proof = _NUMERIC_ADJACENT_ASCII_SPACES_RE.sub("", provider_text)
    source_proof = _NUMERIC_ADJACENT_ASCII_SPACES_RE.sub("", source)
    if _is_numeric_deletion_only(provider_proof, source_proof):
        return source
    unmatched_closing = sum(
        provider_proof.count(value) for value in _CLOSING_QUOTES
    ) > sum(provider_proof.count(value) for value in _OPENING_QUOTES)
    if not unmatched_closing:
        return None
    for index, character in enumerate(source_proof):
        if character not in _OPENING_QUOTES:
            continue
        without_opening_quote = source_proof[:index] + source_proof[index + 1 :]
        if _is_numeric_deletion_only(provider_proof, without_opening_quote):
            return source
    return None


def _identifier_v2_source_replacement(
    *,
    provider_text: str,
    native_text: str,
) -> str | None:
    """Recover numeric identifiers plus one uniquely omitted equals sign."""

    source = _canonical_native_text(native_text)
    if (
        source is None
        or not provider_text.strip()
        or not source
        or provider_text == source
        or source.count("=") != 1
        or "=" in provider_text
    ):
        return None
    provider_atoms = _DIGIT_ATOM_RE.findall(provider_text)
    source_atoms = _DIGIT_ATOM_RE.findall(source)
    if (
        len(source_atoms) <= len(provider_atoms)
        or not _is_ordered_subsequence(provider_atoms, source_atoms)
    ):
        return None
    if _is_numeric_and_single_equals_deletion_only(provider_text, source):
        return source
    return None


def _text_quality_reason(*, provider_text: str, native_text: str) -> str | None:
    source = _canonical_native_text(native_text)
    if (
        source is None
        or not provider_text.strip()
        or not source
        or provider_text == source
    ):
        return None
    if _is_source_omission_with_full_numeric_atom(provider_text, source):
        return "native_text_omission"
    return None


def _is_ordered_subsequence(left: list[str], right: list[str]) -> bool:
    position = 0
    for value in left:
        try:
            position = right.index(value, position) + 1
        except ValueError:
            return False
    return True


def _canonical_native_text(value: str) -> str | None:
    # PDFium's bounded-text implementation inserts isolated CRLF pairs between
    # selected visual lines.  Do not reinterpret other controls, bare line
    # endings or blank lines as that generated boundary.
    without_crlf = value.replace("\r\n", "")
    if (
        "\x00" in value
        or "\r" in without_crlf
        or "\n" in without_crlf
        or _ADJACENT_LINE_BREAK_RE.search(value) is not None
        or _BOUNDARY_LINE_BREAK_RE.search(value) is not None
        or _PADDED_LINE_BREAK_RE.search(value) is not None
    ):
        return None
    normalized = value
    has_generated_line_break = _LINE_BREAK_RE.search(normalized) is not None
    normalized = _LINE_BREAK_RE.sub(
        lambda match: _native_line_break_separator(normalized, match),
        normalized,
    )
    # PDFium's bounded-text path synthesizes CRLF between selected visual
    # lines.  Horizontal whitespace around CRLF remains in the string and must
    # match the provider exactly.  The same real rectangles can carry one
    # terminal ASCII space
    # after the final selected glyph.  Remove only that observed companion
    # artifact: a boundary space without a generated line break, or multiple
    # terminal spaces, remains an exact mismatch and cannot authorize repair.
    if (
        has_generated_line_break
        and normalized.endswith(" ")
        and not normalized.endswith("  ")
    ):
        normalized = normalized[:-1]
    return normalized


def _native_line_break_separator(value: str, match: re.Match[str]) -> str:
    left = value[match.start() - 1] if match.start() else ""
    right = value[match.end()] if match.end() < len(value) else ""
    if _ASCII_WORD_CHAR_RE.fullmatch(left) and _ASCII_WORD_CHAR_RE.fullmatch(right):
        return " "
    return ""


def _is_numeric_deletion_only(provider: str, source: str) -> bool:
    """Prove provider text is source text with only numeric cores deleted.

    A deleted core may consume its trailing percent/per-mille unit or leave
    that unit in provider text.  Every other character, including every
    existing provider number, must match in source order.
    """

    return _is_source_atom_deletion_only(
        provider,
        source,
        allow_single_equals=False,
    )


def _is_numeric_and_single_equals_deletion_only(
    provider: str,
    source: str,
) -> bool:
    return _is_source_atom_deletion_only(
        provider,
        source,
        allow_single_equals=True,
    )


def _is_source_atom_deletion_only(
    provider: str,
    source: str,
    *,
    allow_single_equals: bool,
) -> bool:
    provider_atoms = _numeric_atoms(provider)
    source_atoms = _numeric_atoms(source)
    if _has_ambiguous_adjacent_numeric_atoms(source_atoms):
        return False
    pending = [(0, 0, False, False)]
    seen: set[tuple[int, int, bool, bool]] = set()
    while pending:
        provider_index, source_index, deleted_number, deleted_equals = pending.pop()
        state = (provider_index, source_index, deleted_number, deleted_equals)
        if state in seen:
            continue
        seen.add(state)
        if provider_index == len(provider_atoms) and source_index == len(source_atoms):
            if deleted_number and (deleted_equals or not allow_single_equals):
                return True
            continue
        if (
            provider_index < len(provider_atoms)
            and source_index < len(source_atoms)
            and provider_atoms[provider_index] == source_atoms[source_index]
        ):
            pending.append(
                (
                    provider_index + 1,
                    source_index + 1,
                    deleted_number,
                    deleted_equals,
                )
            )
        if source_index >= len(source_atoms):
            continue
        source_atom = source_atoms[source_index]
        if source_atom[0] == "number":
            source_ends = [source_index + 1]
            if (
                source_index + 1 < len(source_atoms)
                and source_atoms[source_index + 1]
                in {("text", "%"), ("text", "‰")}
            ):
                source_ends.append(source_index + 2)
            next_deleted_number = True
            next_deleted_equals = deleted_equals
        elif (
            allow_single_equals
            and not deleted_equals
            and source_atom == ("text", "=")
        ):
            source_ends = [source_index + 1]
            next_deleted_number = deleted_number
            next_deleted_equals = True
        else:
            continue
        source_ends_with_spacing = list(source_ends)
        for source_end in source_ends:
            spaced_end = source_end
            while (
                spaced_end < len(source_atoms)
                and _is_numeric_placeholder_space(source_atoms[spaced_end])
            ):
                spaced_end += 1
            if spaced_end != source_end:
                source_ends_with_spacing.append(spaced_end)
        # MinerU sometimes leaves one or more blank characters exactly where
        # the native PDF has the omitted numeric token.  Permit only that
        # bounded placeholder: unrelated whitespace remains an exact text atom
        # and cannot be normalized away elsewhere in the string.
        provider_ends = [provider_index]
        provider_end = provider_index
        while (
            provider_end < len(provider_atoms)
            and _is_numeric_placeholder_space(provider_atoms[provider_end])
        ):
            provider_end += 1
        if provider_end != provider_index:
            provider_ends.append(provider_end)
        pending.extend(
            (
                next_provider,
                next_source,
                next_deleted_number,
                next_deleted_equals,
            )
            for next_provider in provider_ends
            for next_source in source_ends_with_spacing
        )
    return False


def _is_source_omission_with_full_numeric_atom(provider: str, source: str) -> bool:
    """Prove provider is an exact source subsequence with a whole number omitted."""

    raw_provider_atoms = _numeric_atoms(provider)
    raw_source_atoms = _numeric_atoms(source)
    if _has_ambiguous_adjacent_numeric_atoms(raw_source_atoms):
        return False
    provider_atoms = _numeric_quality_proof_atoms(raw_provider_atoms)
    source_atoms = _numeric_quality_proof_atoms(raw_source_atoms)
    pending = [(0, 0, False)]
    seen: set[tuple[int, int, bool]] = set()
    while pending:
        provider_index, source_index, deleted_number = pending.pop()
        state = (provider_index, source_index, deleted_number)
        if state in seen:
            continue
        seen.add(state)
        if provider_index == len(provider_atoms) and source_index == len(source_atoms):
            if deleted_number:
                return True
            continue
        if (
            provider_index < len(provider_atoms)
            and source_index < len(source_atoms)
            and provider_atoms[provider_index] == source_atoms[source_index]
        ):
            pending.append(
                (provider_index + 1, source_index + 1, deleted_number)
            )
        if source_index >= len(source_atoms):
            continue
        source_atom = source_atoms[source_index]
        if _is_numeric_placeholder_space(source_atom):
            continue
        next_source = source_index + 1
        next_provider = provider_index
        while (
            next_source < len(source_atoms)
            and _is_numeric_placeholder_space(source_atoms[next_source])
        ):
            next_source += 1
        while (
            next_provider < len(provider_atoms)
            and _is_numeric_placeholder_space(provider_atoms[next_provider])
        ):
            next_provider += 1
        pending.append(
            (
                next_provider,
                next_source,
                deleted_number or source_atom[0] == "number",
            )
        )
    return False


def _numeric_quality_proof_atoms(
    atoms: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Ignore only layout whitespace at a boundary or beside a numeric atom."""

    return tuple(
        atom
        for index, atom in enumerate(atoms)
        if not (
            _is_numeric_placeholder_space(atom)
            and (
                index == 0
                or index == len(atoms) - 1
                or atoms[index - 1][0] == "number"
                or atoms[index + 1][0] == "number"
            )
        )
    )


def _is_numeric_placeholder_space(atom: tuple[str, str]) -> bool:
    return atom[0] == "text" and atom[1] in {" ", "\t"}


def _has_ambiguous_adjacent_numeric_atoms(
    atoms: tuple[tuple[str, str], ...],
) -> bool:
    """Reject a numeric lexer split that could hide a truncated number.

    A signed atom can legitimately follow another number in a compact range
    such as ``1-3``.  An unsigned adjacent atom has no source delimiter and is
    instead evidence that a malformed grouping such as ``1,2345`` was split at
    the regex boundary.  That observation cannot authorize source repair.
    """

    return any(
        left[0] == "number"
        and right[0] == "number"
        and right[1][0] not in {"+", "-", "−"}
        for left, right in zip(atoms, atoms[1:], strict=False)
    )


def _numeric_atoms(value: str) -> tuple[tuple[str, str], ...]:
    atoms: list[tuple[str, str]] = []
    cursor = 0
    for match in _NUMERIC_TOKEN_RE.finditer(value):
        atoms.extend(("text", char) for char in value[cursor : match.start()])
        atoms.append(("number", match.group(0)))
        cursor = match.end()
    atoms.extend(("text", char) for char in value[cursor:])
    return tuple(atoms)


__all__ = ["ProviderDocumentAdmission"]
