"""One process-wide lock for every PDFium call."""

from __future__ import annotations

import threading


PDFIUM_LOCK = threading.RLock()
