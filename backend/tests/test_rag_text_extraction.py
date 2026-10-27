"""`rag.extract_invoice_text` reads a PDF's text layer, and nothing else.

Every image and every `.xml` e-invoice used to be handed to PyMuPDF as a PDF,
fail, and log `PyMuPDF text extraction failed` — on each upload, describing
nothing wrong. The day-one rehearsal (#575) uploads a UBL invoice and its API
log carried the warning twice for one invoice.
"""

from __future__ import annotations

import logging

import pymupdf
import pytest

from app.services.rag import extract_invoice_text

UBL = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>'
)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.mark.parametrize("payload", [UBL, PNG, b""], ids=["ubl-xml", "png", "empty"])
def test_a_file_with_no_pdf_header_is_empty_and_quiet(payload, caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.rag"):
        assert extract_invoice_text(payload) == ""
    assert caplog.records == []


def test_a_pdf_text_layer_is_still_read():
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Invoice INV-42 from Acme")
    pdf = doc.tobytes()
    doc.close()
    assert "INV-42" in extract_invoice_text(pdf)


def test_a_damaged_pdf_still_warns_with_the_class_only(caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.rag"):
        assert extract_invoice_text(b"%PDF-1.7 truncated secret-content") == ""
    assert any("PyMuPDF text extraction failed" in r.getMessage() for r in caplog.records)
    assert "secret-content" not in caplog.text
