"""Independent acceptance: the publication text backstop and cross-version sealed requests (U+0000).

One combined release (``nul-content-preservation-design-r1/decision.md``): a fresh actual U+0000 is kept as a
recorded U+FFFD marker by the v24 builder and reaches transaction P; the pure representability check after
``build_or_reopen`` stays as the backstop for requests sealed by older code (and any other request that still
carries U+0000 in content). No database, provider, model or network; every tree is a temporary directory.

What runs for real: the materialization canonical encoder, ``ProviderDocumentAdmission``,
``build_provider_units``, ``ProductionAtomicPublicationRequestBuilderV4``,
``RecoverableAtomicPublicationRequestFactoryV4``, ``FilesystemAtomicPublicationArtifactReadinessV4``
with a real ``ImmutableArtifactStore``, ``PrepareAndPublishWholeDocumentV4``, the contract sealers and
decoders, ``DurableStagedCoordinatorBackendV4.commit``/``_fail_attempt``, the real
``StagedParseCoordinator`` stop latch and the GC preparation-owner functions.

Doubles (existing unit-test doubles): DB authority rows, semantic routing (fallback receipts; every
call is counted as model work), parser-output promotion, the claim guard, and transaction P, which
only records that it was reached. PostgreSQL itself is exercised by the managed scratch integration.

The E1 (v23) worker is reproduced inside the v24 process by exactly its three differences: no text check, no
substitution record from admission, and the ``provider_unit.v23`` stamp in the request builder. Expectations come
from the accepted decision and the Pro plan, not from the candidate:

* a v24 request never carries U+0000 and passes the backstop unchanged; lawful Unicode, controls, JSON null and the
  literal six characters ``\\u0000`` stay byte-identical;
* a v23-sealed NUL request reopens without rebuild, model call or byte change, and the backstop refuses it before
  readiness/P; the legacy decoder still reads the old sealed bytes; a valid v23-sealed request reopens byte-identical
  and reaches P with its own ``provider_unit.v23`` projection;
* the refusal closes through the existing FailureReceipt/cleanup plan as a non-retryable
  ``provider_artifact_contract`` local failure with a bounded ASCII message that never echoes text or keys (object
  keys render only as ``#<ordinal>``); every other error (DataError, integrity, IO, receipt persistence) still stops
  publicly;
* a valid preparation stays a conservative GC owner and a damaged one blocks GC.
"""

from __future__ import annotations

from collections.abc import Iterator
import dataclasses
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from threading import Event
import time
from typing import Any
import unittest
from unittest import mock

import psycopg
from psycopg.adapt import PyFormat, Transformer
import sqlalchemy as sa
import sqlalchemy.exc
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg

from disclosure_anchor.adapters.db.postgres import atomic_document_publisher_v4 as publisher_module
from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.adapters.storage.atomic_publication_artifact_readiness_v4 import (
    FilesystemAtomicPublicationArtifactReadinessV4,
)
from disclosure_anchor.adapters.storage.immutable_artifact_store import ImmutableArtifactStore
from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
    WholeDocumentPublicationV4Error,
    decode_atomic_publication_request_v4,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    ATOMIC_PUBLICATION_PREPARATION_FILENAME,
    ATOMIC_PUBLICATION_READINESS_FILENAME,
    AtomicPublicationArtifactConflict,
    decode_atomic_publication_preparation_v1,
)
from disclosure_anchor.application.contracts.parse_requeue_decision import (
    AUTOMATIC_PARSE_RETRY_BUDGET_CLASSES,
    RELEASABLE_PARSE_RETRY_BUDGET_CLASSES,
)
from disclosure_anchor.application.contracts.provider_document import ProviderPayload
from disclosure_anchor.application.contracts.provider_source_semantics import (
    SourcePdfObservation,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    FailureReceiptV4,
    decode_remote_parse_evidence_v4,
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import LocalCleanupPlanV4
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPT_VERSION,
    SEMANTIC_ROUTE_RECEIPTS_V3_FILENAME,
)
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.ports.staged_provider_parser import V4ClaimWitness
from disclosure_anchor.application.services import publication_text_representability_v4 as gate_module
from disclosure_anchor.application.services.atomic_publication_request_builder_v4 import (
    ProductionAtomicPublicationRequestBuilderV4,
)
from disclosure_anchor.application.services.atomic_publication_request_factory_v4 import (
    RecoverableAtomicPublicationRequestFactoryV4,
)
from disclosure_anchor.application.services.provider_document_admission import ProviderDocumentAdmission
from disclosure_anchor.application.services.publication_text_representability_v4 import (
    PublicationTextUnrepresentableError,
    validate_publication_text_representability,
)
from disclosure_anchor.application.services.semantic_router import SemanticRouteBatchResult
from disclosure_anchor.application.services.staged_coordinator_backend_v4 import ExpectedV4AttemptFailure
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseGuard, StageLeaseLost
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorTerminal,
    StagedParseCoordinator,
)
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch
from disclosure_anchor.application.use_cases.prepare_and_publish_whole_document_v4 import (
    PrepareAndPublishWholeDocumentV4,
)
from scripts.gc_orphan_artifacts import (
    _collect_orphans,
    _merge_expected_owners,
    _scan_old_candidates,
    _snapshot_preparation_owners,
)
import tests.integration._remote_parse_v4_factory as v4_factory
from tests.unit import test_provider_unit_builder as unit_fx
# Module imports: importing a TestCase class by name would re-run its tests here.
from tests.unit import test_f5_coordinator_stop_independent as f5_stop
from tests.unit import test_staged_coordinator_backend_v4 as backend_fx
from tests.unit._publication_text_fixture import (
    as_v23 as _as_v23,
    canonical_json_text,
    single_unit_request,
    without_gate as _without_gate,
)
from tests.unit._semantic_routes import _fallback_receipt
from tests.unit.test_atomic_publication_artifact_readiness_adapter_v4 import (
    _Guard as ClaimGuard,
    _Paths as ReadinessPaths,
    _Promotion,
)
from tests.unit.test_atomic_publication_request_builder_v4 import (
    _Harness as BuilderHarness,
    _Paths as BuilderPaths,
)
from tests.unit.test_prepare_and_publish_whole_document_v4 import _Uow as LockOnlyUow
from tests.unit.test_provider_document_admission import _FakeSource
from tests.unit.test_staged_parse_coordinator import _limits, _work


GATE_NAME = "validate_publication_text_representability"
MARKER = "\U0000FFFD"
V23 = "provider_unit.v23"
V24 = "provider_unit.v24"
FAILURE_MESSAGE_LIMIT = 4096  # FailureReceiptV4.message bound
REAL_SEALED_ENV = "NUL_RECOVERY_ACTUAL_SEALED_PREPARATION"
REAL_SEALED_SHA_ENV = "NUL_RECOVERY_ACTUAL_SEALED_PREPARATION_SHA256"
# Provider text that must be refused whenever it reaches the gate (production shape first).
UNREPRESENTABLE = {
    "nul_mid": "第\x00节 重要事项",
    "nul_only": "\x00",
}
# Provider text PostgreSQL stores exactly; none of it may be refused or changed.
REPRESENTABLE = {
    "tab": "第一节\t重要事项",
    "lf": "第一节\n重要事项",
    "crlf": "第一节\r\n重要事项",
    "soh": "第\x01节 重要事项",
    "del": "第\x7f节 重要事项",
    "ufffd": "第\ufffd节 重要事项",
    "ufffe": "第\ufffe节 重要事项",
    "uffff": "第\uffff节 重要事项",
    "astral": "第\U00020000节 重要事项",
    "emoji": "第\U0001f600节 重要事项",
    "literal_backslash_u0000": "第\\u0000节 重要事项",
    "json_looking_text": '第{"k":"\\u0000","n":null}节 重要事项',
}
LONE_SURROGATES = {
    "lone_high_surrogate": "第\ud800节 重要事项",
    "lone_low_surrogate": "第\udc00节 重要事项",
}
PLACEMENTS = ("leaf_heading", "ancestor_heading", "body_text")
# Distinctive fragments that must never appear in a diagnostic.
TEXT_MARKERS = ("重要事项", "节", "第", "\\u0000")


# -- the real provider -> request -> readiness -> use-case chain -------------------------------


def _pages(placement: str, value: str) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    block, payload = unit_fx._block, ProviderPayload
    heading = lambda index, text, level: block(  # noqa: E731
        index, 0, "text", (payload("text", None, text),), annotation="title", level=level,
    )
    paragraph = lambda index, text: block(  # noqa: E731
        index, 0, "text", (payload("text", None, text),), annotation="paragraph",
    )
    if placement == "leaf_heading":
        return ((heading(0, value, 1), paragraph(1, "本节正文。")), ())
    if placement == "ancestor_heading":
        return ((heading(0, value, 1), heading(1, "一、概述", 2), paragraph(2, "本节正文。")), ())
    if placement == "body_text":
        return ((heading(0, "第一节 重要事项", 1), paragraph(1, value)), ())
    raise ValueError(placement)


def _many_sections(count: int, *, body: str) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    block, payload = unit_fx._block, ProviderPayload
    blocks = []
    for index in range(count):
        blocks.append(block(2 * index, 0, "text", (payload("text", None, f"第\x00节 事项{index}"),),
                            annotation="title", level=1))
        blocks.append(block(2 * index + 1, 0, "text", (payload("text", None, body),), annotation="paragraph"))
    return (tuple(blocks), ())


def _harness(pages: tuple[tuple[Any, ...], tuple[Any, ...]]) -> BuilderHarness:
    """The builder test harness whose provider document carries exactly ``pages``."""

    real = v4_factory.ProviderDocument

    def with_pages(**kwargs: Any) -> Any:
        replaced = tuple(
            dataclasses.replace(
                page,
                blocks=tuple(
                    dataclasses.replace(item, order_in_page=order)
                    for order, item in enumerate(pages[page.page_index])
                ),
            )
            for page in kwargs["pages"]
        )
        return real(**{**kwargs, "pages": replaced})

    with mock.patch.object(v4_factory, "ProviderDocument", with_pages):
        return BuilderHarness()


class _TransactionPReached(Exception):
    """P is a recorder here: reaching it is the observable success of this chain."""


class _RecordingP:
    def __init__(self) -> None:
        self.requests: list[AtomicPublicationRequestV4] = []

    def commit_whole_document(self, request: AtomicPublicationRequestV4, **_kwargs: Any) -> Any:
        self.requests.append(request)
        raise _TransactionPReached()

    def reload_commit_winner(self, **_kwargs: Any) -> Any:
        raise AssertionError("response-loss recovery is not part of this chain")


class _CountingRouter:
    """Fallback routing; every call stands for model work already spent."""

    def __init__(self) -> None:
        self.calls = 0

    def route(self, *, drafts: tuple[Any, ...], **_kwargs: Any) -> SemanticRouteBatchResult:
        self.calls += 1
        receipts = tuple(
            dataclasses.replace(
                _fallback_receipt(index), contract_version=SEMANTIC_ROUTE_RECEIPT_VERSION,
                semantic_keys=(), evidence=(),
            )
            for index in range(len(drafts))
        )
        return SemanticRouteBatchResult(units=tuple(drafts), receipts=receipts)


class _CountingBuilder:
    def __init__(self, delegate: ProductionAtomicPublicationRequestBuilderV4) -> None:
        self.delegate = delegate
        self.calls = 0

    def build(self, **kwargs: Any) -> AtomicPublicationRequestV4:
        self.calls += 1
        return self.delegate.build(**kwargs)


class _RecordingReadiness:
    def __init__(self, delegate: FilesystemAtomicPublicationArtifactReadinessV4) -> None:
        self.delegate = delegate
        self.reopened: bool | None = None
        self.prepares = 0

    def reopen_prepared_request(self, **kwargs: Any) -> AtomicPublicationRequestV4 | None:
        value = self.delegate.reopen_prepared_request(**kwargs)
        self.reopened = value is not None
        return value

    def prepare_or_replay(self, **kwargs: Any) -> Any:
        self.prepares += 1
        return self.delegate.prepare_or_replay(**kwargs)

    def verify_ready(self, **kwargs: Any) -> Any:
        return self.delegate.verify_ready(**kwargs)


class _CapturingFactory:
    def __init__(self, delegate: RecoverableAtomicPublicationRequestFactoryV4) -> None:
        self.delegate = delegate
        self.request: AtomicPublicationRequestV4 | None = None

    def build_or_reopen(self, **kwargs: Any) -> AtomicPublicationRequestV4:
        self.request = self.delegate.build_or_reopen(**kwargs)
        return self.request


@dataclass
class _Outcome:
    error: BaseException | None
    request: AtomicPublicationRequestV4 | None
    builder_calls: int
    router_calls: int
    reopened: bool | None
    prepares: int
    promotions: int
    p_requests: list[AtomicPublicationRequestV4]
    created: list[str]
    changed: list[str]
    removed: list[str]


def _digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _run_worker(root: Path, harness: BuilderHarness, *, stage_guard: StageLeaseGuard | None = None) -> _Outcome:
    """One worker incarnation: fresh objects over the durable tree at ``root``."""

    paths = ReadinessPaths(root)
    promotions: list[str] = []
    readiness = _RecordingReadiness(FilesystemAtomicPublicationArtifactReadinessV4(
        paths=paths,  # type: ignore[arg-type]
        immutable_store=ImmutableArtifactStore(paths),  # type: ignore[arg-type]
        output_promotion=_Promotion(promotions),  # type: ignore[arg-type]
    ))
    fixture = harness.fixture
    source = _FakeSource(
        record=b"",
        rebuilt=harness.materialized.provider_envelope.provider_document,
        observation=SourcePdfObservation(
            sha256=fixture.source_pdf_sha256,
            byte_count=fixture.local_materialized.source_byte_count,
            page_count=fixture.local_materialized.source_page_count,
        ),
        text_observations=(),
    )
    router = _CountingRouter()
    builder = _CountingBuilder(ProductionAtomicPublicationRequestBuilderV4(
        path_builder=BuilderPaths(),  # type: ignore[arg-type]
        uow_factory=harness.builder._uow_factory,
        admission=ProviderDocumentAdmission(path_builder=BuilderPaths(), source=source),  # type: ignore[arg-type]
        semantic_router=router,  # type: ignore[arg-type]
    ))
    factory = _CapturingFactory(RecoverableAtomicPublicationRequestFactoryV4(
        readiness=readiness, new_request_builder=builder,  # type: ignore[arg-type]
    ))
    publisher = _RecordingP()
    use_case = PrepareAndPublishWholeDocumentV4(
        uow_factory=lambda: LockOnlyUow([]),  # type: ignore[arg-type,return-value]
        publication_requests=factory,  # type: ignore[arg-type]
        readiness=readiness,  # type: ignore[arg-type]
        publisher=publisher,  # type: ignore[arg-type]
    )
    checkpoint = fixture.local_materialized
    claim = V4ClaimWitness(
        attempt_id=checkpoint.attempt_id, fence_identity=checkpoint.fence_identity, state=checkpoint.state,
        lifecycle_version=checkpoint.lifecycle_version, checkpoint_sha256=checkpoint.sha256,
        claim_owner_identity="worker-independent-acceptance", claim_generation=1,
    )
    before = _digest(root)
    error: BaseException | None = None
    try:
        use_case.execute(
            checkpoint=checkpoint, materialized=harness.materialized, claim=claim,
            claim_guard=ClaimGuard(),  # type: ignore[arg-type]
            stage_guard=stage_guard or _live_guard(),
        )
    except Exception as exc:  # noqa: BLE001 - the observed outcome is the assertion subject
        error = exc
    after = _digest(root)
    return _Outcome(
        error=error, request=factory.request, builder_calls=builder.calls, router_calls=router.calls,
        reopened=readiness.reopened, prepares=readiness.prepares, promotions=promotions.count("parser-output"),
        p_requests=publisher.requests, created=sorted(set(after) - set(before)),
        changed=sorted(name for name in before if name in after and before[name] != after[name]),
        removed=sorted(set(before) - set(after)),
    )


def _live_guard() -> StageLeaseGuard:
    return StageLeaseGuard(deadline_monotonic=60.0, _revoked=Event(), _monotonic=lambda: 0.0)


def _builder_version(request: AtomicPublicationRequestV4) -> str:
    return str(json.loads(request.processing_run_projection_json)["builder_rules_version"])


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _landings(request: AtomicPublicationRequestV4, needle: str) -> set[str]:
    """Where ``needle`` actually landed, by this author's own one-decode reading."""

    found = set()
    for unit in request.units:
        if unit.title is not None and needle in unit.title:
            found.add("title")
        if any(needle in item for item in unit.heading_path):
            found.add("heading_path")
        if any(needle in text for text in _strings(strict_json_loads(unit.canonical_payload_json.encode()))):
            found.add("payload")
    return found


def _nul_units(request: AtomicPublicationRequestV4) -> dict[int, set[str]]:
    """Independent map of 1-based Unit ordinal -> projected fields holding a decoded U+0000."""

    found: dict[int, set[str]] = {}
    for unit in request.units:
        fields = set()
        if unit.title is not None and "\x00" in unit.title:
            fields.add("title")
        for depth, item in enumerate(unit.heading_path):
            if "\x00" in item:
                fields.add(f"heading_path/{depth}")
        if any("\x00" in text for text in _strings(strict_json_loads(unit.canonical_payload_json.encode()))):
            fields.add("payload")
        if fields:
            found[unit.unit_index] = fields
    return found


def _assert_safe_diagnostic(test: unittest.TestCase, message: str, *, forbidden: tuple[str, ...] = ()) -> None:
    test.assertIsInstance(message, str)
    test.assertGreaterEqual(len(message), 1)
    test.assertLessEqual(len(message), FAILURE_MESSAGE_LIMIT)
    test.assertTrue(all(0x20 <= ord(char) <= 0x7E for char in message), ascii(message[:200]))
    test.assertIn("U+0000", message)
    for fragment in (*TEXT_MARKERS, *forbidden):
        test.assertNotIn(fragment, message)
    message.encode("ascii")


def _code_point_counts(text: str) -> list[tuple[str, int]]:
    counts = [("U+0000", text.count("\x00"))] if "\x00" in text else []
    surrogates = sorted({ord(char) for char in text if 0xD800 <= ord(char) <= 0xDFFF})
    counts.extend((f"U+{point:04X}", sum(1 for char in text if ord(char) == point)) for point in surrogates)
    return counts


def _expected_findings(request: AtomicPublicationRequestV4) -> list[tuple[int, str, tuple[str, ...], bool, str, int]]:
    """This author's reading of every content finding, in the documented deterministic order.

    Units ascending; per Unit the title, the heading path in order, then the payload decoded exactly
    once (object keys sorted, each key before its value, arrays in order); per string U+0000 first,
    then surrogates ascending. Object keys render only as ``#<ordinal>`` in sorted key order and array
    indexes as decimals (Opus consolidation §6.1); no key text or key hash is ever rendered.
    """

    found: list[tuple[int, str, tuple[str, ...], bool, str, int]] = []

    def walk(value: object, unit: int, path: tuple[str, ...]) -> None:
        if isinstance(value, str):
            found.extend((unit, "payload", path, False, point, count) for point, count in _code_point_counts(value))
        elif isinstance(value, dict):
            for ordinal, key in enumerate(sorted(value)):
                found.extend((unit, "payload", path + (f"#{ordinal}",), True, point, count)
                             for point, count in _code_point_counts(key))
                walk(value[key], unit, path + (f"#{ordinal}",))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, unit, path + (str(index),))

    for unit in request.units:
        if unit.title is not None:
            found.extend((unit.unit_index, "title", (), False, point, count)
                         for point, count in _code_point_counts(unit.title))
        for depth, item in enumerate(unit.heading_path):
            found.extend((unit.unit_index, "heading_path", (str(depth),), False, point, count)
                         for point, count in _code_point_counts(item))
        walk(strict_json_loads(unit.canonical_payload_json.encode()), unit.unit_index, ())
    return found


def _assert_refusal_matches(
    test: unittest.TestCase, error: BaseException | None, request: AtomicPublicationRequestV4,
) -> None:
    """The typed refusal names exactly this request's findings, content-free and bounded."""

    test.assertIsInstance(error, PublicationTextUnrepresentableError, error)
    assert isinstance(error, PublicationTextUnrepresentableError)
    expected = _expected_findings(request)
    test.assertTrue(expected, "the refusal must correspond to a real decoded finding")
    test.assertEqual(error.policy, "publication_text_representability.v1")
    test.assertEqual(error.request_sha256, request.request_sha256)
    test.assertEqual(error.finding_count, len(expected))
    test.assertEqual(error.occurrence_count, sum(item[5] for item in expected))
    test.assertEqual(str(error), error.safe_summary())
    shown = error.findings
    test.assertGreaterEqual(len(shown), 1)
    test.assertLessEqual(len(shown), len(expected))
    for finding, (unit, field, path, in_key, point, count) in zip(shown, expected):
        test.assertEqual((finding.unit_index, finding.field, finding.path, finding.in_key, finding.codepoint,
                          finding.count), (unit, field, path, in_key, point, count))
    _assert_safe_diagnostic(test, str(error))
    test.assertIn(f"unit={shown[0].unit_index}", str(error))
    test.assertIn(f"field={shown[0].field}", str(error))


def _refused(test: unittest.TestCase, request: AtomicPublicationRequestV4) -> PublicationTextUnrepresentableError:
    with test.assertRaises(PublicationTextUnrepresentableError) as caught:
        validate_publication_text_representability(request)
    _assert_refusal_matches(test, caught.exception, request)
    return caught.exception


# -- D1: the actual builder path, fresh and after restart --------------------------------------


class RealBuilderAndSealedPathTests(unittest.TestCase):
    def test_a_fresh_actual_nul_is_kept_as_a_marker_and_reaches_p_unrefused(self) -> None:
        for label, value in UNREPRESENTABLE.items():
            for placement in PLACEMENTS:
                with self.subTest(input=label, placement=placement), tempfile.TemporaryDirectory() as raw:
                    outcome = _run_worker(Path(raw), _harness(_pages(placement, value)))
                    self.assertIsInstance(outcome.error, _TransactionPReached, outcome.error)
                    request = outcome.request
                    assert request is not None
                    self.assertIs(outcome.p_requests[0], request)
                    self.assertEqual((outcome.builder_calls, outcome.prepares, len(outcome.p_requests)), (1, 1, 1))
                    self.assertTrue(any(name.endswith(ATOMIC_PUBLICATION_PREPARATION_FILENAME)
                                        for name in outcome.created))
                    self.assertEqual(_nul_units(request), {}, "a v24 request never carries U+0000")
                    self.assertTrue(_landings(request, MARKER), "the marker takes the NUL's place")
                    # The derived text changed; the stored provider source did not.
                    documents = [name for name in outcome.created if name.endswith("provider_document.v1.json")]
                    self.assertEqual(len(documents), 1)
                    stored = strict_json_loads((Path(raw) / documents[0]).read_bytes())
                    self.assertTrue(any("\x00" in text for text in _strings(stored)))
                    self.assertIsNone(validate_publication_text_representability(request))
                    self.assertEqual(_builder_version(request), V24)
                    for unit in request.units:
                        exposed = [unit.title, list(unit.heading_path),
                                   strict_json_loads(unit.canonical_payload_json.encode())]
                        locator = json.loads(unit.canonical_artifact_locator_json)
                        if MARKER in json.dumps(exposed, ensure_ascii=False):
                            self.assertEqual(unit.quality_status, "needs_review")
                            self.assertEqual(locator["contract_version"], "provider_unit_locator.v10")
                            self.assertTrue(locator["text_substitutions"])

    def test_an_old_v23_sealed_nul_request_is_refused_on_reopen_without_rebuild_model_call_or_byte_change(
        self,
    ) -> None:
        for label, value in UNREPRESENTABLE.items():
            for placement in PLACEMENTS:
                with self.subTest(input=label, placement=placement), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    harness = _harness(_pages(placement, value))
                    with _as_v23() as simulation:
                        sealed = _run_worker(root, harness)
                    self.assertGreaterEqual(simulation.builds, 1)
                    # The E1 incident shape: the request was sealed and promoted, then P was entered.
                    self.assertIsInstance(sealed.error, _TransactionPReached)
                    self.assertEqual(len(sealed.p_requests), 1)
                    assert sealed.request is not None
                    self.assertEqual(_builder_version(sealed.request), V23)
                    self.assertTrue(_nul_units(sealed.request))
                    preparation = [name for name in sealed.created
                                   if name.endswith(ATOMIC_PUBLICATION_PREPARATION_FILENAME)]
                    self.assertEqual(len(preparation), 1)
                    self.assertTrue(any(name.endswith(ATOMIC_PUBLICATION_READINESS_FILENAME)
                                        for name in sealed.created))
                    frozen = _digest(root)

                    restarted = _run_worker(root, harness)

                    self.assertIsInstance(restarted.error, PublicationTextUnrepresentableError, restarted.error)
                    self.assertEqual((restarted.builder_calls, restarted.router_calls), (0, 0))
                    self.assertTrue(restarted.reopened)
                    self.assertEqual((restarted.prepares, restarted.promotions, len(restarted.p_requests)),
                                     (0, 0, 0))
                    self.assertEqual((restarted.created, restarted.changed, restarted.removed), ([], [], []))
                    self.assertEqual(_digest(root), frozen)
                    self.assertEqual(restarted.request, sealed.request)
                    assert restarted.request is not None
                    self.assertEqual(restarted.request.canonical_bytes, sealed.request.canonical_bytes)
                    _assert_refusal_matches(self, restarted.error, restarted.request)
                    # The unchanged legacy codec still reads the old sealed bytes, NUL included.
                    raw_preparation = (root / preparation[0]).read_bytes()
                    decoded = decode_atomic_publication_request_v4(
                        decode_atomic_publication_preparation_v1(raw_preparation).canonical_request_json.encode()
                    )
                    self.assertEqual(decoded, sealed.request)
                    self.assertEqual(_nul_units(decoded), _nul_units(sealed.request))

    def test_a_valid_v23_sealed_request_reopens_byte_identically_and_reaches_p_as_v23(self) -> None:
        for label, value in (("plain", "第一节 重要事项"), ("soh", REPRESENTABLE["soh"]),
                             ("literal_escape", REPRESENTABLE["literal_backslash_u0000"]),
                             ("ufffe", REPRESENTABLE["ufffe"])):
            with self.subTest(input=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                harness = _harness(_pages("leaf_heading", value))
                with _as_v23() as simulation:
                    sealed = _run_worker(root, harness)
                self.assertGreaterEqual(simulation.builds, 1)
                self.assertIsInstance(sealed.error, _TransactionPReached)
                assert sealed.request is not None
                self.assertEqual(_builder_version(sealed.request), V23)
                self.assertEqual(_nul_units(sealed.request), {})
                frozen = _digest(root)

                reopened = _run_worker(root, harness)

                self.assertIsInstance(reopened.error, _TransactionPReached, reopened.error)
                self.assertEqual((reopened.builder_calls, reopened.router_calls), (0, 0))
                self.assertTrue(reopened.reopened)
                self.assertEqual(len(reopened.p_requests), 1)
                self.assertEqual(reopened.p_requests[0].canonical_bytes, sealed.request.canonical_bytes)
                self.assertEqual(_builder_version(reopened.p_requests[0]), V23)
                self.assertEqual((reopened.created, reopened.changed, reopened.removed), ([], [], []))
                self.assertEqual(_digest(root), frozen)

    def test_representable_text_reaches_p_unchanged_and_byte_identical_to_the_ungated_chain(self) -> None:
        for label, value in REPRESENTABLE.items():
            for placement in PLACEMENTS:
                with self.subTest(input=label, placement=placement):
                    harness = _harness(_pages(placement, value))
                    with tempfile.TemporaryDirectory() as gated_raw, tempfile.TemporaryDirectory() as plain_raw:
                        gated = _run_worker(Path(gated_raw), harness)
                        with _without_gate():
                            plain = _run_worker(Path(plain_raw), harness)
                        for outcome in (gated, plain):
                            self.assertIsInstance(outcome.error, _TransactionPReached, outcome.error)
                            self.assertEqual((outcome.builder_calls, outcome.prepares, len(outcome.p_requests)),
                                             (1, 1, 1))
                        assert gated.request is not None and plain.request is not None
                        self.assertTrue(_landings(gated.request, value), "the value must land in the request")
                        # P receives the very request the factory produced, byte-identical to the ungated one.
                        self.assertIs(gated.p_requests[0], gated.request)
                        self.assertEqual(gated.request, plain.request)
                        self.assertEqual(gated.request.canonical_bytes, plain.request.canonical_bytes)
                        self.assertEqual(gated.request.request_sha256, plain.request.request_sha256)
                        self.assertEqual(sorted(gated.created), sorted(plain.created))
                        gated_files, plain_files = _digest(Path(gated_raw)), _digest(Path(plain_raw))
                        # Asset IDs are fresh per preparation; every ID-free artifact is byte-identical.
                        for name in gated_files:
                            if name.endswith(("provider_document.v1.json", SEMANTIC_ROUTE_RECEIPTS_V3_FILENAME)):
                                self.assertEqual(gated_files[name], plain_files[name], name)
                        prepared = [decode_atomic_publication_preparation_v1((Path(raw) / name).read_bytes())
                                    for raw, files in ((gated_raw, gated_files), (plain_raw, plain_files))
                                    for name in files if name.endswith(ATOMIC_PUBLICATION_PREPARATION_FILENAME)]
                        self.assertEqual(len(prepared), 2)
                        self.assertEqual(prepared[0].canonical_request_json, prepared[1].canonical_request_json)
                        # The gate itself returns nothing and changes nothing.
                        before = gated.request.canonical_bytes
                        self.assertIsNone(validate_publication_text_representability(gated.request))
                        self.assertEqual(gated.request.canonical_bytes, before)

    def test_lone_surrogates_are_stopped_upstream_and_can_never_be_sealed_into_a_request(self) -> None:
        # Defensive: the JSON decoders admit \ud800, but the materialization canonical encoder (the
        # existing typed provider_artifact_contract path) and the contract sealers reject it, so a
        # lone surrogate never reaches the publication gate. No production incident is claimed.
        for label, value in LONE_SURROGATES.items():
            for placement in PLACEMENTS:
                with self.subTest(input=label, placement=placement):
                    with self.assertRaises(ValueError) as encoder:
                        unit_fx._admitted(unit_fx._document(pages=_pages(placement, value), segments=()))
                    self.assertNotIsInstance(encoder.exception, PublicationTextUnrepresentableError)
                    with self.assertRaises(ValueError) as fixture:
                        _harness(_pages(placement, value))
                    self.assertNotIsInstance(fixture.exception, PublicationTextUnrepresentableError)
            with self.subTest(input=label, boundary="contract sealer"):
                with self.assertRaises(ValueError) as sealed:
                    single_unit_request({"text": value})
                self.assertNotIsInstance(sealed.exception, PublicationTextUnrepresentableError)


# -- D1: one decode of the known JSONB fields, diagnostics, and control identity ----------------


class DecodedJsonAndDiagnosticsTests(unittest.TestCase):
    def test_payload_is_decoded_exactly_once(self) -> None:
        valid = {
            "literal_backslash_value": {"text": "第\\u0000节"},
            "literal_backslash_key": {"text": "x", "k\\u0000": "v"},
            "json_looking_value": {"text": '{"k":"\\u0000"}'},
            "json_null_value": {"text": "x", "note": None},
            "controls_and_noncharacters": {
                "text": "\t\n\r\x01\x7f\U0000FFFD\U0000FFFE\U0000FFFF\U00020000\U0001F600",
            },
        }
        for label, payload in valid.items():
            with self.subTest(payload=label):
                request = single_unit_request(payload)
                before = request.canonical_bytes
                self.assertIsNone(validate_publication_text_representability(request))
                self.assertEqual(request.canonical_bytes, before)
        for label, payload in {
            "decoded_nul_value": {"text": "第\x00节"},
            "decoded_nul_nested_value": {"text": "x", "rows": [["a", "b\x00"]]},
        }.items():
            with self.subTest(payload=label):
                _refused(self, single_unit_request(payload))

    def test_untrusted_payload_keys_are_never_echoed(self) -> None:
        # Defensive helper boundary only: provider payload keys come from the closed field contract,
        # so no provider path is claimed to produce these keys (Pro §4.2, root interface review 1).
        hostile = "k\x00QXJW\n\"QUOTED\"\\SLASH" + "Z" * 10_000
        in_key = _refused(self, single_unit_request({"text": "x", hostile: "value"}))
        self.assertTrue(any(finding.in_key for finding in in_key.findings))
        _assert_safe_diagnostic(self, str(in_key), forbidden=("QXJW", "QUOTED", "SLASH", "ZZZZ"))
        # A printable, regex-safe key is still document content when it is not a known field.
        under_key = _refused(self, single_unit_request({"text": "x", "zqvault_holder_label": "\x00"}))
        self.assertFalse(any(finding.in_key for finding in under_key.findings))
        _assert_safe_diagnostic(self, str(under_key), forbidden=("zqvault", "holder_label"))

    def test_many_findings_and_huge_text_give_one_bounded_deterministic_ascii_summary(self) -> None:
        keys = {f"field{index:05d}": "\x00" for index in range(5_000)}
        huge = {"text": ("\x00" * 5_000) + ("正" * 100_000), **keys}
        request = single_unit_request(huge)
        first, second = _refused(self, request), _refused(self, request)
        self.assertEqual((first.finding_count, first.occurrence_count), (5_001, 10_000))
        self.assertEqual(str(first), str(second))
        _assert_safe_diagnostic(self, str(first), forbidden=("正",))
        # Real builder, sealed by the v23 code: forty headed Units each with a NUL title/path and body.
        with tempfile.TemporaryDirectory() as raw:
            harness = _harness(_many_sections(40, body="\x00" * 20 + "正文"))
            with _as_v23():
                _run_worker(Path(raw), harness)
            outcome = _run_worker(Path(raw), harness)
        assert outcome.request is not None
        self.assertGreaterEqual(len(_nul_units(outcome.request)), 40)
        _assert_refusal_matches(self, outcome.error, outcome.request)
        _assert_safe_diagnostic(self, str(outcome.error), forbidden=("事项", "正文"))
        self.assertEqual((outcome.builder_calls, outcome.prepares, len(outcome.p_requests), outcome.created),
                         (0, 0, 0, []))

    def test_control_identity_and_taxonomy_are_integrity_errors_never_content(self) -> None:
        # The existing contract already refuses to seal a NUL in a closed identity, key or routing field.
        unsealable = {
            "section_key": dict(section_keys=("sec\x00tion",)),
            "semantic_key": dict(semantic_keys=("key\x00",)),
            "locator_identity": dict(locator_edit=lambda value: value.update(contract_version="v\x00")),
            "parser_target_identity": dict(projection_edit=lambda value: value["parser_target_identity"].update(
                name="MinerU\x00")),
            "parser_name": dict(projection_edit=lambda value: value.update(parser_name="MinerU\x00")),
        }
        for label, kwargs in unsealable.items():
            with self.subTest(unsealable=label), self.assertRaises((ValueError, TypeError)) as caught:
                single_unit_request({"text": "x"}, **kwargs)  # type: ignore[arg-type]
            self.assertNotIsInstance(caught.exception, PublicationTextUnrepresentableError)

        # A request corrupted after sealing (in memory) is an integrity stop, checked before content,
        # so even a request that also carries a content NUL is never reported as a bad document.
        def locator_with_nul(unit: Any) -> str:
            locator = json.loads(unit.canonical_artifact_locator_json)
            locator["contract_version"] = "v\x00"
            return canonical_json_text(locator)

        def projection_with_nul(request: AtomicPublicationRequestV4) -> str:
            projection = json.loads(request.processing_run_projection_json)
            projection["parser_target_identity"]["name"] = "MinerU\x00"
            return json.dumps(projection, sort_keys=True, separators=(",", ":"))

        corruptions = {
            "section_keys": lambda request: object.__setattr__(request.units[0], "section_keys", ("sec\x00",)),
            "semantic_keys": lambda request: object.__setattr__(request.units[0], "semantic_keys", ("key\x00",)),
            "artifact_locator": lambda request: object.__setattr__(
                request.units[0], "canonical_artifact_locator_json", locator_with_nul(request.units[0])),
            "processing_run.parser_target_identity": lambda request: object.__setattr__(
                request, "processing_run_projection_json", projection_with_nul(request)),
        }
        for label, corrupt in corruptions.items():
            for content in ({"text": "x"}, {"text": "第\x00节"}):
                with self.subTest(corrupted=label, content_nul="\x00" in content["text"]):
                    request = single_unit_request(content)
                    corrupt(request)
                    with self.assertRaises(WholeDocumentPublicationV4Error) as caught:
                        validate_publication_text_representability(request)
                    self.assertNotIsInstance(caught.exception, PublicationTextUnrepresentableError)
                    message = str(caught.exception)
                    self.assertTrue(message.isascii() and "\x00" not in message and "MinerU" not in message)
        # A non-V4 object is an integrity error too, never a document failure.
        with self.assertRaises(WholeDocumentPublicationV4Error):
            validate_publication_text_representability(mock.Mock(units=()))  # type: ignore[arg-type]

    def test_db_bound_text_projections_stay_inside_the_reviewed_gate_coverage(self) -> None:
        # Small ratchet for actual transaction-P projections: a new TEXT/JSONB Unit column, a new Unit
        # field read by P, or a new persisted run-projection field fails until the gate is reviewed.
        import inspect
        import re

        unit_text_columns = {
            column.name for column in models.DocumentUnit.__table__.columns
            if isinstance(column.type, (sa.Text, JSONB))
        }
        self.assertEqual(unit_text_columns,
                         {"title", "heading_path", "semantic_keys", "section_keys", "payload", "artifact_locator"})
        read_by_p = set(re.findall(r"\bunit\.(\w+)", inspect.getsource(publisher_module._document_unit)))
        declared = {name for name, _column in (*gate_module.CONTENT_UNIT_FIELDS, *gate_module.CONTROL_UNIT_FIELDS)}
        self.assertLessEqual(read_by_p, declared | set(gate_module.CLOSED_UNIT_FIELDS))
        self.assertEqual({column for _name, column in (*gate_module.CONTENT_UNIT_FIELDS,
                                                       *gate_module.CONTROL_UNIT_FIELDS)}, unit_text_columns)
        persisted = set(re.findall(r"projection\[\"(\w+)\"\]", inspect.getsource(
            publisher_module.PostgresAtomicWholeDocumentPublisherV4._apply_processing_projection)))
        self.assertTrue(persisted)
        self.assertLessEqual(persisted, set(gate_module.PERSISTED_PROCESSING_RUN_PROJECTION_FIELDS))
        # The provider-derived content columns are refused behaviourally.
        for placement, columns in (("leaf_heading", {"title", "heading_path"}), ("body_text", {"payload"})):
            with self.subTest(placement=placement), tempfile.TemporaryDirectory() as raw:
                harness = _harness(_pages(placement, UNREPRESENTABLE["nul_mid"]))
                with _as_v23():
                    _run_worker(Path(raw), harness)
                outcome = _run_worker(Path(raw), harness)
                assert outcome.request is not None
                _assert_refusal_matches(self, outcome.error, outcome.request)
                assert isinstance(outcome.error, PublicationTextUnrepresentableError)
                self.assertEqual({finding.field for finding in outcome.error.findings}, columns)


# -- B3/D1: the commit lane keeps typed local failure apart from every stop --------------------


def _driver_nul_data_error() -> sqlalchemy.exc.DataError:
    """The real client-side rejection: SQLAlchemy's psycopg bind of a Text NUL (no connection)."""

    dialect = PGDialect_psycopg()
    processor = sa.Text().dialect_impl(dialect).bind_processor(dialect)
    bound = processor("第\x00节") if processor is not None else "第\x00节"
    try:
        Transformer().get_dumper(bound, PyFormat.AUTO).dump(bound)
    except psycopg.DataError as exc:
        wrapped = sqlalchemy.exc.DBAPIError.instance(
            "INSERT INTO disclosure_core.document_unit (...) VALUES (...)", {}, exc, psycopg.Error,
            hide_parameters=True,
        )
        assert isinstance(wrapped, sqlalchemy.exc.DataError)
        return wrapped
    raise AssertionError("the installed psycopg text dumper accepted U+0000")


class _LookalikeError(ValueError):
    """Named like the typed refusal, but not it: classification is by type, never by name."""


_LookalikeError.__name__ = "PublicationTextUnrepresentableError"
_LookalikeError.__qualname__ = "PublicationTextUnrepresentableError"


def _commit_outcome(error: BaseException) -> tuple[Any, Any, BaseException | None]:
    case = backend_fx.DurableStagedCoordinatorBackendV4Tests("test_commit_semantic_locked_overflow_becomes_local_failure")
    authority, backend, persistence, published = case._commit_with_publisher_error(error)
    try:
        updated = backend.commit(backend_fx._work(authority), credit_allowance=ResourceCreditVector(),
                                 stage_guard=backend_fx._guard())
    except BaseException as raised:  # noqa: BLE001 - the propagated error is the subject
        return (persistence, published, raised)
    return (persistence, (published, updated), None)


def _public_stop_for(error: BaseException) -> Any:
    """Run ``error`` through the real coordinator's commit lane and return its first public cause."""

    backend = f5_stop._StageBackend(recoverable=(_work("attempt-commit", "local_materialized", 6),))
    backend.commit_error["attempt-commit"] = error
    latch = InProcessWorkerStopLatch()
    result = f5_stop._run(StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=latch))
    return result, latch.first_cause()


class CommitLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.refusal = _refused(self, single_unit_request({"text": "第\x00节 重要事项"}))

    def test_typed_refusal_closes_through_the_existing_failure_receipt_and_cleanup_plan(self) -> None:
        persistence, (published, updated), raised = _commit_outcome(self.refusal)
        self.assertIsNone(raised)
        self.assertEqual(updated.state, "cleanup_pending")
        self.assertEqual(len(persistence.appends), 1)
        append = persistence.appends[0]
        self.assertEqual(tuple(item.kind for item in append.new_evidence), ("failure_receipt", "cleanup_plan"))
        failure = append.new_evidence[0].value
        self.assertIsInstance(failure, FailureReceiptV4)
        self.assertEqual(
            (failure.outcome, failure.error_stage, failure.error_code, failure.error_class, failure.retryable,
             failure.retry_budget_class),
            ("local_failure", "commit", "publication_text_unrepresentable", "PublicationTextUnrepresentableError",
             False, "provider_artifact_contract"),
        )
        _assert_safe_diagnostic(self, failure.message)
        self.assertIn(failure.retry_budget_class, RELEASABLE_PARSE_RETRY_BUDGET_CLASSES)
        self.assertNotIn(failure.retry_budget_class, AUTOMATIC_PARSE_RETRY_BUDGET_CLASSES)
        # The persisted evidence is itself representable and round-trips exactly.
        encoded = encode_remote_parse_evidence_v4(failure)
        self.assertNotIn(b"\\u0000", encoded.exact_bytes)
        self.assertEqual(decode_remote_parse_evidence_v4("failure_receipt", encoded.exact_bytes).value, failure)
        plan = append.new_evidence[1].value
        self.assertIsInstance(plan, LocalCleanupPlanV4)
        self.assertEqual((plan.outcome, plan.failure_receipt_sha256), ("local_failure", failure.sha256))
        self.assertIsNone(append.successor.publication_winner_sha256)
        published.assert_not_called()

    def test_every_other_commit_error_still_raises_untouched_and_latches_a_public_stop(self) -> None:
        server_shaped = sqlalchemy.exc.DBAPIError.instance(
            "INSERT INTO disclosure_core.document_unit (...) VALUES (...)", {},
            psycopg.errors.UntranslatableCharacter("unsupported Unicode escape sequence"), psycopg.Error,
            hide_parameters=True,
        )
        unknown: dict[str, BaseException] = {
            "client_nul_data_error": _driver_nul_data_error(),
            "server_jsonb_22p05": server_shaped,
            "data_error_mentioning_nul": sqlalchemy.exc.DataError("stmt", {}, psycopg.DataError("NUL (0x00)")),
            "value_error_subclass": type("DerivedValueError", (ValueError,), {})("U+0000 in title"),
            "lookalike_name": _LookalikeError("U+0000"),
            "unicode_encode": UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed"),
            "integrity_closure": WholeDocumentPublicationV4Error("publication record is not strict JSON"),
            "artifact_conflict": AtomicPublicationArtifactConflict("readiness resource drifted"),
            "permission": PermissionError("preparation not readable"),
            "io": OSError("storage unavailable"),
            "typed_attempt_failure_at_commit": ExpectedV4AttemptFailure(
                error_code="publication_text_unrepresentable", message="U+0000", retryable=False,
                retry_budget_class="provider_artifact_contract",
            ),
        }
        for label, error in unknown.items():
            with self.subTest(error=label):
                persistence, published, raised = _commit_outcome(error)
                self.assertIs(raised, error)
                self.assertEqual(persistence.appends, [])
                published.assert_not_called()
                result, cause = _public_stop_for(error)
                self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
                self.assertEqual(result.termination_kind, "public_stop")
                assert cause is not None
                self.assertEqual((cause.kind, cause.reason_code, cause.lane), ("stage_fault",
                                                                                "commit_unexpected_failure", "commit"))
        # Contrast: the typed refusal leaves the commit lane with a durable successor, never a stop.
        _persistence, _pair, raised = _commit_outcome(self.refusal)
        self.assertIsNone(raised)

    def test_failure_receipt_persistence_error_is_not_swallowed(self) -> None:
        case = backend_fx.DurableStagedCoordinatorBackendV4Tests("test_commit_semantic_locked_overflow_becomes_local_failure")
        authority, backend, persistence, published = case._commit_with_publisher_error(self.refusal)
        broken = sqlalchemy.exc.OperationalError("INSERT evidence", {}, Exception("connection lost"))

        def failing_append(*_args: object, **_kwargs: object) -> None:
            raise broken

        persistence.append_successor = failing_append  # type: ignore[method-assign]
        with self.assertRaises(sqlalchemy.exc.OperationalError) as caught:
            backend.commit(backend_fx._work(authority), credit_allowance=ResourceCreditVector(),
                           stage_guard=backend_fx._guard())
        self.assertIs(caught.exception, broken)
        self.assertEqual(persistence.appends, [])
        published.assert_not_called()
        result, cause = _public_stop_for(caught.exception)
        self.assertEqual(result.termination_kind, "public_stop")
        assert cause is not None
        self.assertEqual((cause.kind, cause.reason_code), ("stage_fault", "commit_unexpected_failure"))

    def test_lease_loss_around_the_refusal_persists_nothing_and_a_later_claim_reproduces_it(self) -> None:
        for provenance in ("operator_cancel", "ownership_lost", "deadline_exhausted"):
            with self.subTest(provenance=provenance):
                case = backend_fx.DurableStagedCoordinatorBackendV4Tests(
                    "test_commit_semantic_locked_overflow_becomes_local_failure")
                authority, backend, persistence, published = case._commit_with_publisher_error(self.refusal)
                guard = backend_fx._guard()

                def revoke_then_refuse(**_kwargs: object) -> None:
                    guard.revoke(provenance)  # type: ignore[arg-type]
                    raise self.refusal

                backend._publisher.execute.side_effect = revoke_then_refuse
                with self.assertRaises(StageLeaseLost):
                    backend.commit(backend_fx._work(authority), credit_allowance=ResourceCreditVector(),
                                   stage_guard=guard)
                self.assertEqual(persistence.appends, [])
                published.assert_not_called()
                # The next claim is not locked into anything: the same refusal closes typed.
                backend._publisher.execute.side_effect = self.refusal
                updated = backend.commit(backend_fx._work(authority), credit_allowance=ResourceCreditVector(),
                                         stage_guard=backend_fx._guard())
                self.assertEqual(updated.state, "cleanup_pending")
                self.assertEqual(persistence.appends[0].new_evidence[0].value.error_code,
                                 "publication_text_unrepresentable")


# -- B4/D1: GC keeps valid preparations as owners and stops on damaged ones --------------------


class GcPreparationOwnerTests(unittest.TestCase):
    def _sealed_nul_tree(self) -> tuple[Path, Path]:
        raw = tempfile.mkdtemp(prefix="nul-gc-")
        self.addCleanup(shutil.rmtree, raw, True)
        root = Path(raw)
        with _as_v23():
            sealed = _run_worker(root, _harness(_pages("leaf_heading", UNREPRESENTABLE["nul_mid"])))
        self.assertIsInstance(sealed.error, _TransactionPReached)
        (preparation,) = [root / name for name in sealed.created
                          if name.endswith(ATOMIC_PUBLICATION_PREPARATION_FILENAME)]
        return root, preparation

    def test_nul_sealed_preparation_keeps_every_declared_resource_owned(self) -> None:
        root, preparation_path = self._sealed_nul_tree()
        preparation = decode_atomic_publication_preparation_v1(preparation_path.read_bytes())
        promoted = root / preparation.parser_output_plan.published_relpath.rstrip("/") / "result.json"
        promoted.parent.mkdir(parents=True, exist_ok=True)
        promoted.write_bytes(b"promoted provider output")
        retired = root / "parser_artifacts" / "cninfo" / "000001" / "retired" / "orphan.json"
        retired.parent.mkdir(parents=True, exist_ok=True)
        retired.write_bytes(b"orphan")
        now = time.time()
        for path in root.rglob("*"):
            if path.is_file():
                os.utime(path, (now - 3 * 86400, now - 3 * 86400))
        frozen = _digest(root)

        owners = _snapshot_preparation_owners(root)
        expected = _merge_expected_owners({family: set() for family in owners}, owners)
        candidates, _skipped = _scan_old_candidates(root, now_ts=now)
        orphans, _recheck = _collect_orphans(candidates, data_root=root, expected=expected, now_ts=now)

        self.assertEqual([orphan.path for orphan in orphans], [retired])
        declared = {
            preparation_path, preparation_path.with_name(ATOMIC_PUBLICATION_READINESS_FILENAME),
            root / preparation.document_unit_snapshot_plan.relpath,
            root / preparation.semantic_route_receipts_plan.relpath,
            root / preparation.provider_document_plan.relpath, promoted,
        }
        for path in declared:
            self.assertTrue(path.is_file(), path)
            self.assertNotIn(path, {orphan.path for orphan in orphans})
        self.assertEqual(_digest(root), frozen)

    def test_damaged_or_blindly_rewritten_nul_preparation_blocks_gc(self) -> None:
        _root, preparation_path = self._sealed_nul_tree()
        original = preparation_path.read_bytes()
        escaped = b"\\\\u0000"
        self.assertIn(escaped, original, "the sealed request carries the NUL as a JSON escape")
        damages = {
            "truncated": original[: len(original) // 2],
            "bit_flip": original[:100] + bytes([original[100] ^ 0x01]) + original[101:],
            "blind_ufffd_rewrite": original.replace(escaped, b"\\\\ufffd"),
            "blind_space_rewrite": original.replace(escaped, b" "),
        }
        for label, damaged in damages.items():
            with self.subTest(damage=label):
                root = Path(tempfile.mkdtemp(prefix="nul-gc-damaged-"))
                self.addCleanup(shutil.rmtree, root, True)
                shutil.copytree(_root, root, dirs_exist_ok=True)
                target = root / preparation_path.relative_to(_root)
                target.write_bytes(damaged)
                with self.assertRaisesRegex(RuntimeError, "blocks GC"):
                    _snapshot_preparation_owners(root)
        orphaned = Path(tempfile.mkdtemp(prefix="nul-gc-orphan-readiness-"))
        self.addCleanup(shutil.rmtree, orphaned, True)
        shutil.copytree(_root, orphaned, dirs_exist_ok=True)
        (orphaned / preparation_path.relative_to(_root)).unlink()
        with self.assertRaisesRegex(RuntimeError, "readiness blocks GC"):
            _snapshot_preparation_owners(orphaned)


# -- opt-in: the actual sealed production preparation, read-only ------------------------------


class ActualSealedPreparationTests(unittest.TestCase):
    """Explicit opt-in (paths and hashes stay outside Git; root names them in the command)."""

    def test_actual_target_preparation_is_read_by_the_legacy_codec_refused_and_still_a_gc_owner(self) -> None:
        source = os.environ.get(REAL_SEALED_ENV)
        expected_sha = os.environ.get(REAL_SEALED_SHA_ENV)
        if not source or not expected_sha:
            self.skipTest(f"{REAL_SEALED_ENV} and {REAL_SEALED_SHA_ENV} are not set: the actual sealed "
                          "preparation stays outside Git and is opt-in")
        path = Path(source)
        raw = path.read_bytes()
        self.assertEqual("sha256:" + hashlib.sha256(raw).hexdigest(), expected_sha)
        preparation = decode_atomic_publication_preparation_v1(raw)
        request = decode_atomic_publication_request_v4(preparation.canonical_request_json.encode())
        self.assertEqual(len(request.units), 22)
        self.assertEqual(_nul_units(request), {22: {"title", "heading_path/2"}})
        error = _refused(self, request)
        self.assertEqual([(f.unit_index, f.field, f.path, f.in_key, f.codepoint, f.count) for f in error.findings],
                         [(22, "title", (), False, "U+0000", 1), (22, "heading_path", ("2",), False, "U+0000", 1)])
        self.assertEqual(json.loads(request.processing_run_projection_json)["builder_rules_version"], V23)
        # Read-only: the production bytes are unchanged, and a copy at its declared relpath is an owner.
        self.assertEqual("sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(), expected_sha)
        with tempfile.TemporaryDirectory() as data:
            relpath = Path(preparation.document_unit_snapshot_plan.relpath).with_name(
                ATOMIC_PUBLICATION_PREPARATION_FILENAME)
            copy = Path(data) / relpath
            copy.parent.mkdir(parents=True)
            copy.write_bytes(raw)
            owners = _snapshot_preparation_owners(Path(data))
        self.assertIn(relpath.as_posix(), owners["document_unit_snapshots"])
        self.assertIn(preparation.document_unit_snapshot_plan.relpath, owners["document_unit_snapshots"])
        self.assertIn(preparation.provider_document_plan.relpath, owners["provider_documents"])
        self.assertIn(preparation.parser_output_plan.published_relpath.rstrip("/"), owners["parser_artifacts"])


if __name__ == "__main__":
    unittest.main()
