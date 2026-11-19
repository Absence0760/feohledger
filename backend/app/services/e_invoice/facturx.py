"""Extract embedded CII XML from a Factur-X / ZUGFeRD hybrid PDF.

Factur-X (FR) and ZUGFeRD (DE) are the same hybrid format: a PDF/A-3 with a
UN/CEFACT CII XML attachment. The conventional attachment name is
``factur-x.xml`` (FX), ``zugferd-invoice.xml`` (ZUGFeRD ≤ 2.0), or
``xrechnung.xml`` (German XRechnung-in-PDF). We match those names first, then
fall back to sniffing any attachment whose bytes parse as a CII document.

Opens the PDF through ``services/pdf_reader.open_pdf``, the one owner of
PyMuPDF, which closes the document on exit. Sync: its callers (the e-invoice
detect / parse probe) run in the extraction worker. Never raises: any PyMuPDF /
parse error returns ``None`` so a corrupt or password-protected PDF degrades
cleanly to "no embedded XML" (the caller then falls back to vision OCR on the rendered pages).
"""

from __future__ import annotations

from app.services.pdf_reader import open_pdf

# Conventional embedded-XML filenames, lower-cased for case-insensitive match.
_CII_FILENAMES = {
    "factur-x.xml",
    "zugferd-invoice.xml",
    "zugferd-invoice.xml ",  # defensive: some emitters trail a space
    "xrechnung.xml",
    "cii.xml",
}

# UN/CEFACT CII root local name — used for the content sniff fallback.
_CII_ROOT_LOCALNAME = "CrossIndustryInvoice"


def _looks_like_cii(xml_bytes: bytes) -> bool:
    """Cheap root-tag sniff — does this XML's root look like CII?"""
    try:
        from app.services.e_invoice._xml import local_name, parse_secure

        root = parse_secure(xml_bytes)
        return local_name(root) == _CII_ROOT_LOCALNAME
    except Exception:
        return False


def extract_embedded_cii_xml(pdf_bytes: bytes) -> bytes | None:
    """Return the embedded CII XML bytes from a Factur-X/ZUGFeRD PDF, else None.

    Plain (non-hybrid) PDFs and corrupt PDFs both return ``None``. Never raises.
    """
    if not pdf_bytes:
        return None
    try:
        with open_pdf(pdf_bytes) as doc:
            names = doc.embfile_names()
            if not names:
                return None

            # First pass: match the conventional Factur-X/ZUGFeRD filename.
            for name in names:
                if (name or "").strip().lower() in _CII_FILENAMES:
                    data = doc.embfile_get(name)
                    if data:
                        return bytes(data)

            # Second pass: any attachment whose bytes detect as CII.
            for name in names:
                try:
                    data = doc.embfile_get(name)
                except Exception:
                    continue
                if data and _looks_like_cii(bytes(data)):
                    return bytes(data)

            return None
    except Exception:
        return None
