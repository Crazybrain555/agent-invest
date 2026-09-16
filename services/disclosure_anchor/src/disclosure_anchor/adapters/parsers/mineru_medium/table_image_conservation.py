"""Read-only projection of the retained in-table image conservation evidence.

The MinerU container records, on each table block of ``_model.json``, every
original crop token the model output lost, duplicated or failed to restore
exactly once, together with the crop bytes as a JPEG data URI. This module
verifies that evidence against its own digest and projects the
application-owned value type; the bytes themselves stay in the immutable
model artifact and are never copied forward.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import math

from disclosure_anchor.application.contracts.provider_document import ProviderTableImageUnmatched
from disclosure_anchor.application.contracts.provider_document_envelope import ProviderDocumentEnvelopeError

MODEL_TABLE_IMAGE_KEY = "table_image_unmatched"
_DATA_URI_PREFIX = "data:image/jpeg;base64,"
_REQUIRED_KEYS = frozenset({
    "kind", "token", "expected", "actual", "image_sha256", "image_byte_count", "image_data_uri",
})
_UNRESTORED_KEYS = frozenset({"restored_expected", "restored_actual"})


def extract_table_image_unmatched(
    model_json: object, *, expected_total: int | None = None,
) -> tuple[ProviderTableImageUnmatched, ...]:
    """Project verified conservation evidence from one exact ``_model.json`` value.

    A block without the key contributes nothing (legacy or fully conserved);
    a present key with anything but a non-empty list is malformed evidence.
    Any present entry must decode to the bytes it names; ``expected_total``
    when given must equal the number of entries found. Failures raise
    ``ProviderDocumentEnvelopeError`` and never degrade to an empty result.
    """

    if expected_total is not None and (
        isinstance(expected_total, bool) or not isinstance(expected_total, int) or expected_total < 0
    ):
        raise ValueError("expected table image total must be a non-negative integer")
    if not isinstance(model_json, list):
        raise ProviderDocumentEnvelopeError("model_json must be a list of pages")
    items: list[ProviderTableImageUnmatched] = []
    for page_index, page in enumerate(model_json):
        if not isinstance(page, list):
            raise ProviderDocumentEnvelopeError("model_json page must be a list of blocks")
        for block_index, block in enumerate(page):
            if not isinstance(block, dict):
                raise ProviderDocumentEnvelopeError("model_json block must be an object")
            if MODEL_TABLE_IMAGE_KEY not in block:
                continue  # legacy or fully conserved block
            raw = block[MODEL_TABLE_IMAGE_KEY]
            if block.get("type") != "table":
                raise ProviderDocumentEnvelopeError("table image evidence outside a table block")
            if not isinstance(raw, list) or not raw:
                raise ProviderDocumentEnvelopeError("table image evidence must be a non-empty list")
            bbox = _bbox(block.get("bbox"))
            previous_token: str | None = None
            for entry in raw:
                item = _entry(entry, page_index=page_index, block_index=block_index, bbox=bbox)
                if previous_token is not None and item.token <= previous_token:
                    raise ProviderDocumentEnvelopeError("table image evidence tokens must be ordered and unique")
                previous_token = item.token
                items.append(item)
    if expected_total is not None and len(items) != expected_total:
        raise ProviderDocumentEnvelopeError("table image conservation total differs")
    return tuple(items)


def _bbox(value: object) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, list) or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) for item in value)
    ):
        raise ProviderDocumentEnvelopeError("table block bbox must be four finite numbers")
    return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))


def _entry(
    value: object, *, page_index: int, block_index: int, bbox: tuple[float, float, float, float],
) -> ProviderTableImageUnmatched:
    if not isinstance(value, dict):
        raise ProviderDocumentEnvelopeError("table image evidence entry must be an object")
    present = set(value)
    if not _REQUIRED_KEYS <= present <= _REQUIRED_KEYS | _UNRESTORED_KEYS:
        raise ProviderDocumentEnvelopeError("table image evidence entry fields are not closed")
    kind = value["kind"]
    if (present & _UNRESTORED_KEYS) and kind != "unrestored":
        raise ProviderDocumentEnvelopeError("restoration counts belong to unrestored evidence only")
    for name in ("expected", "actual", "image_byte_count", *sorted(present & _UNRESTORED_KEYS)):
        count = value[name]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ProviderDocumentEnvelopeError(f"table image evidence {name} must be a non-negative integer")
    for name in ("kind", "token", "image_sha256", "image_data_uri"):
        if not isinstance(value[name], str):
            raise ProviderDocumentEnvelopeError(f"table image evidence {name} must be text")
    data_uri = value["image_data_uri"]
    if not data_uri.startswith(_DATA_URI_PREFIX):
        raise ProviderDocumentEnvelopeError("table image evidence must carry a JPEG data URI")
    try:
        image = base64.b64decode(data_uri[len(_DATA_URI_PREFIX):], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProviderDocumentEnvelopeError("table image evidence data URI is not valid base64") from exc
    if not image or "sha256:" + hashlib.sha256(image).hexdigest() != value["image_sha256"]:
        raise ProviderDocumentEnvelopeError("table image evidence bytes differ from their declared digest")
    if len(image) != value["image_byte_count"]:
        raise ProviderDocumentEnvelopeError("table image evidence byte count differs from its bytes")
    try:
        return ProviderTableImageUnmatched(
            page_index=page_index, model_block_index=block_index, table_bbox=bbox, kind=kind,
            token=value["token"], expected=value["expected"], actual=value["actual"],
            image_sha256=value["image_sha256"], image_byte_count=value["image_byte_count"],
        )
    except ValueError as exc:
        raise ProviderDocumentEnvelopeError(f"table image evidence is invalid: {exc}") from exc


__all__ = ["MODEL_TABLE_IMAGE_KEY", "extract_table_image_unmatched"]
