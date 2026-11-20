"""Every PDF render is offloaded off the event loop, and stays that way.

The four branded PDF renderers (`analytics_report_pdf`, `audit_report_pdf`,
`remittance_pdf`, `tax_1099_forms`) are ordinary **sync** functions, and each
one is reached only from an `async def` route handler. Two things inside them
block for a long time:

* `branding.build_logo_flowable` → `fetch_logo_bytes` does a *blocking* DNS
  lookup (the SSRF guard) plus a *blocking* `httpx.Client` GET of the tenant's
  logo, bounded at `LOGO_FETCH_TIMEOUT_SECONDS`; and
* ReportLab lays the document out on the CPU, which for a multi-page landscape
  table is not free.

Called straight from the coroutine, that whole cost is charged to the event
loop and every other in-flight request on the worker waits behind it. So each
call site wraps the renderer in `asyncio.to_thread`. This module is the drift
guard on that: a new export route (or a refactor) that calls a renderer
directly fails here instead of quietly reintroducing the stall.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import patch

import pytest

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

# The sync PDF renderers. Anything here must be reached via asyncio.to_thread
# from a coroutine.
RENDERERS = {
    "render_analytics_report_pdf",
    "render_audit_report_pdf",
    "render_remittance_pdf",
    "render_1099_pdf",
}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _offloaded_renderers(tree: ast.AST) -> set[int]:
    """ids() of the renderer Name nodes passed to `asyncio.to_thread`."""
    ok: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) != "to_thread" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Name) and first.id in RENDERERS:
            ok.add(id(first))
    return ok


def test_no_pdf_renderer_is_called_directly_from_a_coroutine():
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        source = path.read_text()
        if not any(name in source for name in RENDERERS):
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name in RENDERERS:
                offenders.append(f"{path.relative_to(APP_DIR.parent)}:{node.lineno} {name}(...)")

    assert offenders == [], (
        "PDF renderers must be offloaded with `await asyncio.to_thread(render_x, ctx)` — "
        "calling one directly blocks the event loop for the logo fetch + layout: "
        + ", ".join(offenders)
    )


def test_every_renderer_reference_in_the_api_layer_is_a_to_thread_argument():
    """The positive half: each renderer name that appears as a bare reference in
    a route module is there as `asyncio.to_thread`'s first argument."""
    seen = 0
    for path in sorted((APP_DIR / "api").rglob("*.py")):
        source = path.read_text()
        if not any(name in source for name in RENDERERS):
            continue
        tree = ast.parse(source)
        offloaded = _offloaded_renderers(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Name) or node.id not in RENDERERS:
                continue
            if not isinstance(node.ctx, ast.Load):
                continue
            # Import aliases are ast.alias, not ast.Name, so anything reaching
            # here is a real use of the callable.
            assert id(node) in offloaded, (
                f"{path.relative_to(APP_DIR.parent)}:{node.lineno} uses {node.id} "
                "outside `asyncio.to_thread`"
            )
            seen += 1

    # Guard the guard: if the renderers move or get renamed, this test must not
    # silently pass by scanning nothing.
    assert seen >= 6, f"expected every PDF export route to be covered, found {seen}"


def test_logo_embed_really_is_blocking_network_io():
    """Why the offload exists: the "pure, DB-free" renderer fetches over HTTP.

    `fetch_logo_bytes` opens a synchronous `httpx.Client` against the tenant's
    admin-set logo URL. On the event loop that is a stall of up to
    `LOGO_FETCH_TIMEOUT_SECONDS` per export, plus the DNS lookup in front of it.
    """
    from app.services import branding

    with patch("httpx.Client", side_effect=RuntimeError("blocking HTTP")) as client:
        # A public host so the SSRF guard passes and the fetch is attempted.
        assert branding.fetch_logo_bytes("https://8.8.8.8/logo.png") is None
    assert client.called, "the logo embed no longer performs a blocking fetch"


@pytest.mark.asyncio
async def test_remittance_render_runs_off_the_loop_thread():
    """End-to-end on the primitive: the renderer body executes on a worker."""
    import asyncio
    import threading
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.services.remittance_pdf import (
        RemittanceContext,
        RemittanceLine,
        render_remittance_pdf,
    )

    ctx = RemittanceContext(
        payer_name="Acme",
        payer_address=None,
        vendor_name="Supplier",
        vendor_address=None,
        payment_date=datetime.now(UTC),
        payment_method="ach",
        payment_reference="REF-1",
        payment_amount=Decimal("100.00"),
        currency="USD",
        lines=[RemittanceLine(invoice_number="INV-1", description=None, amount=Decimal("100.00"))],
    )
    loop_thread = threading.current_thread().ident
    seen: list[int | None] = []
    real = render_remittance_pdf

    def _recording(c):
        seen.append(threading.current_thread().ident)
        return real(c)

    pdf = await asyncio.to_thread(_recording, ctx)
    assert pdf.startswith(b"%PDF")
    assert seen and all(tid != loop_thread for tid in seen)


# --------------------------------------------------------------------------- #
# Reading an uploaded PDF: `services/pdf_reader` is the one owner of PyMuPDF.
#
# Rasterizing a scan (up to 20 pages at 200 dpi, plus a Tesseract orientation
# pass per page) and walking a text layer are synchronous C-library work. The
# vendor-statement upload, the extraction adapters and the RAG embed at review
# time all reach it from a coroutine, so `pdf_reader`'s awaitable API hands its
# sync cores to `asyncio.to_thread`, and nothing else opens a PDF.
# --------------------------------------------------------------------------- #

PDF_READER = APP_DIR / "services" / "pdf_reader.py"

# The sync cores. A coroutine awaits `pdf_reader.pdf_text` /
# `pdf_reader.rasterize_pages` (or `rag.read_invoice_text`) instead; these may
# only be passed to `asyncio.to_thread` or called from sync code.
SYNC_PDF_CORES = {
    "open_pdf",
    "document_text_sync",
    "text_layer_sync",
    "rasterize_pages_sync",
    "extract_invoice_text",
    "extract_embedded_cii_xml",
    "_detect_structured_format",
    # The structured e-invoice entry points: an XML mime is only a hint, and
    # `detect_format` trusts the `%PDF` magic, so either one can open a hybrid
    # Factur-X with PyMuPDF. PEPPOL receive and the `einvoice` adapter both
    # take arbitrary sender bytes.
    "detect_format",
    "parse_e_invoice",
}

# Known coroutine callers still on a sync core: none. `run_extraction` awaits
# `rag.read_invoice_text` and hands `_detect_structured_format` to
# `asyncio.to_thread`. Keep it empty — a new entry is a regression.
KNOWN_SYNC_CORE_CALLERS: set[tuple[str, str]] = set()


def _calls_in_coroutine_body(fn: ast.AsyncFunctionDef):
    """Calls made directly by ``fn``, not by a nested def / lambda it defines
    (those are what gets handed to ``to_thread``)."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def test_only_pdf_reader_opens_a_pdf_with_pymupdf():
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path == PDF_READER:
            continue
        tree = ast.parse(path.read_text())
        rel = path.relative_to(APP_DIR.parent)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in {"pymupdf", "fitz"}:
                        offenders.append(f"{rel}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in {"pymupdf", "fitz"}:
                    offenders.append(f"{rel}:{node.lineno} from {node.module} import ...")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "open"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"pymupdf", "fitz"}
            ):
                offenders.append(f"{rel}:{node.lineno} {node.func.value.id}.open(...)")

    assert offenders == [], (
        "PyMuPDF is owned by app/services/pdf_reader.py (offloaded, closes every "
        "document); go through it instead: " + ", ".join(offenders)
    )


def test_no_coroutine_calls_a_sync_pdf_core_directly():
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        source = path.read_text()
        if not any(name in source for name in SYNC_PDF_CORES):
            continue
        rel = str(path.relative_to(APP_DIR.parent))
        for fn in ast.walk(ast.parse(source)):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for call in _calls_in_coroutine_body(fn):
                name = _call_name(call)
                if name in SYNC_PDF_CORES and (rel, name) not in KNOWN_SYNC_CORE_CALLERS:
                    offenders.append(f"{rel}:{call.lineno} {fn.name} -> {name}(...)")

    assert offenders == [], (
        "A coroutine must await pdf_reader.pdf_text / rasterize_pages (or pass the "
        "sync core to asyncio.to_thread): " + ", ".join(offenders)
    )


def test_no_coroutine_is_allowlisted_onto_a_sync_pdf_core():
    """The allowlist emptied when `run_extraction` moved onto the awaitable
    forms; it may not grow back."""
    assert KNOWN_SYNC_CORE_CALLERS == set()


def test_an_xml_mime_does_not_stop_detect_format_opening_a_pdf(monkeypatch):
    """Why `parse_e_invoice` is a sync PDF core even where the caller passes an
    XML mime (PEPPOL receive): the `%PDF` magic wins, and the Factur-X probe
    opens the bytes."""
    from app.services.e_invoice import detect

    probed: list[bytes] = []
    monkeypatch.setattr(detect, "extract_embedded_cii_xml", lambda b: probed.append(b) or None)
    payload = b"%PDF-1.7\n not really a pdf"
    fmt = detect.detect_format(payload, mime_type="application/xml", filename="inbound.xml")
    assert fmt is detect.DetectedFormat.NONE
    assert probed == [payload]


def test_peppol_receive_hands_parse_e_invoice_to_to_thread():
    """Pin the PEPPOL shape: the payload parse is `asyncio.to_thread`'s first
    argument, not a call on the public webhook's loop."""
    tree = ast.parse((APP_DIR / "services" / "peppol_receive.py").read_text())
    offloaded = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and _call_name(call) == "to_thread"
        and call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "parse_e_invoice"
    ]
    assert offloaded, "peppol_receive must parse the inbound payload via asyncio.to_thread"


def test_the_awaitable_pdf_api_is_a_to_thread_hand_off():
    """Pin the shape: `pdf_text` / `rasterize_pages` each pass their sync core
    as `asyncio.to_thread`'s first argument."""
    tree = ast.parse(PDF_READER.read_text())
    handed_off: dict[str, str] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for call in _calls_in_coroutine_body(fn):
            if _call_name(call) == "to_thread" and call.args:
                first = call.args[0]
                if isinstance(first, ast.Name):
                    handed_off[fn.name] = first.id
    assert handed_off == {"pdf_text": "text_layer_sync", "rasterize_pages": "rasterize_pages_sync"}


def _pdf(pages: list[str]) -> bytes:
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page(width=300, height=300)
        if text:
            page.insert_text((20, 40), text, fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


_LONG_TEXT = "Statement of account INV-1001 2026-01-15 1,200.00 and more words"


@pytest.fixture
def opened_docs(monkeypatch):
    """Record every document `open_pdf` hands out, and the thread it ran on."""
    import contextlib
    import threading

    from app.services import pdf_reader

    real = pdf_reader.open_pdf
    seen: list[tuple[object, int | None]] = []

    @contextlib.contextmanager
    def _recording(pdf_bytes):
        with real(pdf_bytes) as doc:
            seen.append((doc, threading.current_thread().ident))
            yield doc

    monkeypatch.setattr(pdf_reader, "open_pdf", _recording)
    return seen


def test_open_pdf_closes_the_document_even_when_the_reader_raises():
    from app.services.pdf_reader import open_pdf

    with pytest.raises(RuntimeError), open_pdf(_pdf([_LONG_TEXT])) as doc:
        held = doc
        raise RuntimeError("reader failed mid-page")
    assert held.is_closed


def test_every_sync_core_closes_what_it_opened(opened_docs):
    from app.services import pdf_reader

    pdf = _pdf([_LONG_TEXT, ""])
    assert "INV-1001" in pdf_reader.text_layer_sync(pdf)
    assert pdf_reader.text_layer_sync(_pdf([""])) is None
    assert len(pdf_reader.rasterize_pages_sync(pdf, max_pages=1)) == 1
    assert len(opened_docs) == 3
    assert all(doc.is_closed for doc, _ in opened_docs)


def test_unreadable_bytes_are_none_and_empty_not_an_error():
    from app.services import pdf_reader

    assert pdf_reader.text_layer_sync(b"not a pdf") is None
    assert pdf_reader.rasterize_pages_sync(b"not a pdf") == []


@pytest.mark.asyncio
async def test_pdf_text_and_rasterize_run_off_the_loop_thread(opened_docs):
    import threading

    from app.services import pdf_reader

    loop_thread = threading.current_thread().ident
    pdf = _pdf([_LONG_TEXT, _LONG_TEXT])
    assert "INV-1001" in (await pdf_reader.pdf_text(pdf))
    images = await pdf_reader.rasterize_pages(pdf)
    assert len(images) == 2 and all(img[:4] == b"\x89PNG" for img in images)
    assert len(opened_docs) == 2
    assert all(tid != loop_thread for _, tid in opened_docs)
    assert all(doc.is_closed for doc, _ in opened_docs)


@pytest.mark.asyncio
async def test_statement_upload_reads_the_pdf_off_the_loop_thread(opened_docs, monkeypatch):
    """The route's whole read — `extract_statement_lines` through the default
    (mock) adapter — never opens the PDF on the event loop thread."""
    import threading
    import uuid

    from app.services import vendor_statement_extraction as extraction
    from app.services.extraction_adapters.mock_adapter import MockExtractionAdapter

    async def _mock_adapter(*_a, **_kw):
        return MockExtractionAdapter({})

    monkeypatch.setattr(extraction, "resolve_statement_adapter", _mock_adapter)
    statement = "Invoice     Date          Amount\nINV-1001    2026-01-15    1,200.00\n"
    loop_thread = threading.current_thread().ident

    lines, _meta = await extraction.extract_statement_lines(
        org_settings={},
        organization_id=uuid.uuid4(),
        file_bytes=_pdf([statement]),
        file_key="statement.pdf",
    )

    assert [ln.invoice_number for ln in lines] == ["INV-1001"]
    assert opened_docs and all(tid != loop_thread for _, tid in opened_docs)
    assert all(doc.is_closed for doc, _ in opened_docs)
