"""The one owner of PyMuPDF: reading a PDF's text layer and rasterizing its pages.

Opening a PDF, walking its pages for text and rendering them to PNG are all
synchronous CPU work inside a C library, and rasterizing is the heavy one: up
to :data:`MAX_RASTER_PAGES` pages at :data:`RASTER_DPI` plus a Tesseract
orientation pass per page. Every reader of an uploaded PDF is reached from an
``async def`` (the vendor-statement upload, the extraction adapters, the RAG
embed at review time), so run inline that work stalls the event loop and every
other request on the worker waits behind it.

So this module is the chokepoint, the same shape as ``services/storage`` for
boto3:

* :func:`pdf_text` / :func:`rasterize_pages` are the coroutine API. Each hands
  its sync core to ``asyncio.to_thread``; a coroutine never calls a core.
* :func:`text_layer_sync`, :func:`document_text_sync` and
  :func:`rasterize_pages_sync` are the sync cores, for code already on a worker
  thread (the e-invoice probe, the RAG text read in the extraction worker).
* :func:`open_pdf` is the only place ``pymupdf.open`` is called, and it always
  closes the document. Other modules that need PyMuPDF's object model (the
  Factur-X attachment probe) go through it.

``tests/test_pdf_render_offloaded.py`` pins all three: no ``pymupdf.open`` /
``fitz.open`` outside this module, no sync core called from an ``async def``,
and the cores really run off the loop thread.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# The "basically no text" floor: a scanned page often yields a few stray
# characters from a header stamp, which is not a text layer.
TEXT_LAYER_MIN_CHARS = 50
# Page cap for rasterizing, so an unusually long document can't blow the
# vision model's token budget (or this worker's memory).
MAX_RASTER_PAGES = 20
RASTER_DPI = 200


@contextmanager
def open_pdf(pdf_bytes: bytes) -> Iterator[Any]:
    """Open ``pdf_bytes`` as a PyMuPDF document and close it on exit.

    Raises ``ImportError`` when PyMuPDF is not installed, and PyMuPDF's own
    error when the bytes are not a readable PDF; callers decide what either
    means. Sync: call it only from a worker thread.
    """
    import pymupdf  # PyMuPDF

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        yield doc
    finally:
        doc.close()


def document_text_sync(pdf_bytes: bytes) -> str:
    """Every page's text layer, joined by newlines and stripped.

    Raises on unreadable bytes or a missing PyMuPDF (see :func:`open_pdf`).
    Pages are joined with a newline so the last line of one page and the first
    line of the next never run together into one token.
    """
    with open_pdf(pdf_bytes) as doc:
        return "\n".join(page.get_text("text") for page in doc).strip()


def text_layer_sync(pdf_bytes: bytes, *, min_chars: int = TEXT_LAYER_MIN_CHARS) -> str | None:
    """A PDF's embedded text layer, or ``None`` when there isn't one.

    ``None`` means "this is a scan" (or PyMuPDF is unavailable, or the bytes
    aren't a readable PDF): the caller falls back to a vision model rather than
    treat an empty read as an empty document. Never raises.
    """
    try:
        text = document_text_sync(pdf_bytes)
    except Exception:
        return None
    return text if len(text) > min_chars else None


def rasterize_pages_sync(
    pdf_bytes: bytes, *, max_pages: int = MAX_RASTER_PAGES, dpi: int = RASTER_DPI
) -> list[bytes]:
    """Render up to ``max_pages`` pages to PNG bytes, one buffer per page.

    Pages pass through :func:`image_preprocess.auto_rotate_pages` so a scan
    90/180/270 degrees off upright reaches the vision model the right way up;
    that pass is gated on ``settings.extraction_auto_rotate`` and is a no-op
    when Tesseract is not on the host. Returns ``[]`` when the bytes can't be
    read as a PDF. Never raises.
    """
    images: list[bytes] = []
    try:
        with open_pdf(pdf_bytes) as doc:
            for i, page in enumerate(doc):
                if i >= max_pages:
                    break
                images.append(page.get_pixmap(dpi=dpi).tobytes("png"))
    except Exception:
        return []

    from app.config import settings

    if settings.extraction_auto_rotate and images:
        from app.services.image_preprocess import auto_rotate_pages

        images = auto_rotate_pages(images)
    return images


async def pdf_text(pdf_bytes: bytes, *, min_chars: int = TEXT_LAYER_MIN_CHARS) -> str | None:
    """:func:`text_layer_sync`, run on a worker thread. Never raises."""
    return await asyncio.to_thread(text_layer_sync, pdf_bytes, min_chars=min_chars)


async def rasterize_pages(
    pdf_bytes: bytes, *, max_pages: int = MAX_RASTER_PAGES, dpi: int = RASTER_DPI
) -> list[bytes]:
    """:func:`rasterize_pages_sync`, run on a worker thread. Never raises."""
    return await asyncio.to_thread(rasterize_pages_sync, pdf_bytes, max_pages=max_pages, dpi=dpi)
