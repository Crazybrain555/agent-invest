"""Independent literal PDF/rectangles for the caller-owned stream boundary.

PDF operators below, not native extraction output, define the expected text.
These synthetic native-PDF fixtures establish no filing or provider admission.
"""
from __future__ import annotations

import hashlib
import json

from disclosure_anchor.application.contracts.provider_document import (
    ProviderBBox, ProviderBlock, ProviderDocument, ProviderPage, ProviderPayload,
)


UPPER_BBOX = ProviderBBox(70, 90, 700, 160)
LOWER_BBOX = ProviderBBox(70, 840, 700, 900)
EMPTY_BUNDLE_SHA = "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


def sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def text_pdf(*, replacement: bool = False, rotation: int = 0) -> bytes:
    """One 600x800 page; Helvetica text at literal upper/lower coordinates."""
    upper = b"REPLACEMENT TOKEN" if replacement else b"ORIGINAL TOKEN"
    content = (b"BT /F1 18 Tf 50 700 Td (" + upper + b") Tj ET\n"
               b"BT /F1 18 Tf 50 100 Td (OUTSIDE TOKEN) Tj ET\n")
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] "
         + f"/Rotate {rotation} ".encode("ascii")
         + b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"endstream",
    )
    raw = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for index, payload in enumerate(objects, 1):
        offsets.append(len(raw))
        raw.extend(f"{index} 0 obj\n".encode("ascii") + payload + b"\nendobj\n")
    xref = len(raw)
    raw.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        raw.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    raw.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(raw)


def provider_block(index: int, bbox: ProviderBBox = UPPER_BBOX, *, kind: str = "text") -> ProviderBlock:
    field = "table_body" if kind == "table" else "text"
    # Deliberately not the native PDF text: copying provider text must fail.
    text = "provider-only body" if kind == "text" else "<table><tr><td>provider-only</td></tr></table>"
    raw = json.dumps({"bbox": list(bbox.as_tuple()), "page_idx": 0,
                      "type": kind, field: text}, sort_keys=True, separators=(",", ":"))
    return ProviderBlock(
        source_index=index, page_index=0, order_in_page=index,
        provider_type=kind, typed_annotation=None, provider_level=None,
        bbox=bbox, payloads=(ProviderPayload(field, None, text),),
        referenced_artifact_roles=(), raw_item_json=raw,
        raw_item_sha256=sha(raw.encode("utf-8")),
    )


def provider_document(raw_pdf: bytes, *, blocks: tuple[ProviderBlock, ...] | None = None) -> ProviderDocument:
    if blocks is None:
        blocks = (provider_block(0),)
    return ProviderDocument(
        source_pdf_sha256=sha(raw_pdf), parser_version="3.4.4",
        backend="hybrid", effort="medium", ocr_enabled=False,
        pages=(ProviderPage(0, (600.0, 800.0), blocks),),
        physical_table_segments=(), artifacts=(), bundle_sha256=EMPTY_BUNDLE_SHA,
    )
