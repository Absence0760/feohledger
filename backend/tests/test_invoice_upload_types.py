"""The invoice file pickers offer exactly the types the upload accepts.

The server's list is `services/storage.ALLOWED_CONTENT_TYPES`; the browser's is
the `accept` attribute every invoice file input takes from
`frontend/src/lib/utils/invoiceFiles.ts`. They were two lists, and they drifted:
the pickers offered PDF and images only, so a UBL / CII `.xml` e-invoice —
parsed deterministically, with no extraction key, on any install — could not be
chosen in the file dialog at all. The day-one rehearsal (#575) uploads exactly
that file on a fresh production-shaped install.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from app.services.storage import ALLOWED_CONTENT_TYPES

REPO_ROOT = Path(__file__).resolve().parents[2]
ACCEPT_TS = REPO_ROOT / "frontend" / "src" / "lib" / "utils" / "invoiceFiles.ts"
INVOICE_PICKERS = (
    "frontend/src/routes/invoices/+page.svelte",
    "frontend/src/lib/components/modals/CreateInvoiceModal.svelte",
    "frontend/src/lib/components/modals/InvoiceModal.svelte",
)

# What a browser sends as the part's Content-Type for each picked extension.
# Stated rather than read from `mimetypes`, whose answer for `.xml` differs by
# platform (`text/xml` on some, `application/xml` on others) — the server takes
# both, so either is right, which is the point of listing both there.
_BROWSER_TYPES = {
    ".pdf": {"application/pdf"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".tiff": {"image/tiff"},
    ".xml": {"application/xml", "text/xml"},
}


def _accept() -> list[str]:
    match = re.search(r"INVOICE_FILE_ACCEPT\s*=\s*'([^']*)'", ACCEPT_TS.read_text())
    assert match, "INVOICE_FILE_ACCEPT not found"
    return match.group(1).split(",")


def test_every_offered_extension_is_one_the_upload_accepts() -> None:
    for ext in _accept():
        assert ext in _BROWSER_TYPES, f"{ext}: add its browser content type above"
        assert _BROWSER_TYPES[ext] <= ALLOWED_CONTENT_TYPES, ext


def test_every_accepted_content_type_can_be_picked() -> None:
    offered = set().union(*(_BROWSER_TYPES[ext] for ext in _accept()))
    assert ALLOWED_CONTENT_TYPES <= offered, sorted(ALLOWED_CONTENT_TYPES - offered)


def test_the_extension_table_agrees_with_the_platform_where_it_is_unambiguous() -> None:
    for ext, types in _BROWSER_TYPES.items():
        guessed, _ = mimetypes.guess_type(f"invoice{ext}")
        if ext != ".xml":
            assert {guessed} == types, ext


def test_every_invoice_picker_uses_the_shared_list() -> None:
    for rel in INVOICE_PICKERS:
        text = (REPO_ROOT / rel).read_text()
        assert "accept={INVOICE_FILE_ACCEPT}" in text, rel
        assert 'accept=".pdf' not in text, f"{rel} restates the accepted types"
