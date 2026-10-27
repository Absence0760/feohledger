"""Unit tests for structured e-invoice format detection.

detect_format is pure, never raises, and is the single routing decision for
sending an inbound file to the structured-parse path vs. vision/OCR.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from app.services.e_invoice import detect_format
from app.services.e_invoice.detect import DetectedFormat

_FIX = Path(__file__).parent / "fixtures" / "e_invoice"
_UBL = (_FIX / "ubl_invoice.xml").read_bytes()
_CII = (_FIX / "cii_invoice.xml").read_bytes()


def _facturx_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page()
    doc.embfile_add("factur-x.xml", _CII, filename="factur-x.xml")
    return doc.tobytes()


def _plain_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Scanned invoice, no embedded XML")
    return doc.tobytes()


def test_detect_ubl():
    assert detect_format(_UBL, filename="ubl_invoice.xml") is DetectedFormat.UBL


def test_detect_cii_xml():
    assert detect_format(_CII, filename="cii_invoice.xml") is DetectedFormat.CII_XML


def test_detect_facturx_pdf():
    assert detect_format(_facturx_pdf(), mime_type="application/pdf") is DetectedFormat.FACTURX_PDF


def test_detect_plain_pdf_is_none():
    assert detect_format(_plain_pdf(), mime_type="application/pdf") is DetectedFormat.NONE


def test_detect_junk_bytes_is_none():
    assert detect_format(b"\x00\x01\x02not xml not pdf") is DetectedFormat.NONE


def test_detect_empty_is_none():
    assert detect_format(b"") is DetectedFormat.NONE


def test_detect_unknown_xml_root_is_none():
    other = b'<?xml version="1.0"?><SomethingElse xmlns="urn:x"><a/></SomethingElse>'
    assert detect_format(other, mime_type="application/xml") is DetectedFormat.NONE


def test_detect_is_bom_tolerant():
    # A UTF-8 BOM immediately preceding the XML declaration (a real-world
    # serialization) must still classify as UBL.
    bommed = b"\xef\xbb\xbf" + _UBL
    assert detect_format(bommed) is DetectedFormat.UBL


def test_detect_is_leading_whitespace_tolerant():
    # No XML declaration → leading whitespace before the root element is valid
    # XML, and the sniff + parse must both tolerate it.
    no_decl = _UBL.split(b"?>", 1)[1]  # drop the <?xml ...?> prolog
    assert detect_format(b"\n   " + no_decl) is DetectedFormat.UBL


def test_detect_by_mime_without_filename():
    assert detect_format(_CII, mime_type="text/xml") is DetectedFormat.CII_XML


def test_detect_wrong_mime_still_sniffs_content():
    # A UBL document mislabeled application/octet-stream: leading-< sniff wins.
    assert detect_format(_UBL, mime_type="application/octet-stream") is DetectedFormat.UBL


def test_detect_never_raises_on_truncated_pdf():
    # A %PDF header with garbage after it must not raise.
    assert detect_format(b"%PDF-1.7\nbroken", mime_type="application/pdf") is DetectedFormat.NONE
