"""Extract embedded CII XML from a Factur-X / ZUGFeRD PDF/A-3."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from app.services.e_invoice import extract_embedded_cii_xml

_CII = (Path(__file__).parent / "fixtures" / "e_invoice" / "cii_invoice.xml").read_bytes()


def _pdf_with_embedded(name: str, data: bytes) -> bytes:
    doc = pymupdf.open()
    doc.new_page()
    doc.embfile_add(name, data, filename=name)
    return doc.tobytes()


def test_extract_returns_embedded_cii_bytes():
    pdf = _pdf_with_embedded("factur-x.xml", _CII)
    extracted = extract_embedded_cii_xml(pdf)
    assert extracted == _CII


def test_extract_matches_zugferd_name():
    pdf = _pdf_with_embedded("zugferd-invoice.xml", _CII)
    assert extract_embedded_cii_xml(pdf) == _CII


def test_extract_falls_back_to_content_sniff_on_odd_name():
    # Unconventional attachment name, but the bytes ARE CII → content sniff.
    pdf = _pdf_with_embedded("invoice_data.xml", _CII)
    assert extract_embedded_cii_xml(pdf) == _CII


def test_plain_pdf_returns_none():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "No embedded XML here")
    assert extract_embedded_cii_xml(doc.tobytes()) is None


def test_corrupt_pdf_returns_none_no_raise():
    assert extract_embedded_cii_xml(b"%PDF-1.7 totally broken") is None


def test_empty_bytes_returns_none():
    assert extract_embedded_cii_xml(b"") is None
