"""Compatibility shim: the structural table parser is a shared contract.

The independent audit re-derives the stored grid from ``table_html``, so
the parser lives in ``application.contracts.table_html_structure``; this
module keeps the historical adapter import path stable.
"""

from disclosure_anchor.application.contracts.table_html_structure import (
    HtmlTableCell,
    HtmlTableMedia,
    ParsedHtmlTable,
    TableHtmlStructureError,
    parse_table_html_structure,
    table_media_artifact_role,
)

__all__ = [
    "HtmlTableCell",
    "HtmlTableMedia",
    "ParsedHtmlTable",
    "TableHtmlStructureError",
    "parse_table_html_structure",
    "table_media_artifact_role",
]
