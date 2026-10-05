"""Shared gate for the S3 file-download proxies — resolve the owning row first.

Four employee routes stream a stored object by its key: the invoice document
(``GET /api/invoices/file/{key}``), a supplier-chat attachment
(``GET /api/invoices/{id}/chat/file/{key}`` and its key-only twin), a contract
document (``GET /api/contracts/file/{key}``) and an expense receipt
(``GET /api/expenses/receipt/{key}``). They used to check only that the key's
first segment was the caller's org, and never opened the tenant DB — so the
key was the whole authorisation, and with subsidiary B selected a caller
holding A's key could still download A's documents after every by-id route over
the owning rows had been closed (``docs/decisions.md`` §222, §226).

The key is not trusted any more. Each layout embeds its owner's id
(``services/storage`` stamps them), so the gate parses that id out, loads the
owning row *within the caller's selected entity*, and — where the row records
exactly one file — requires the key to BE that file. Every refusal is the same
``404 "File not found"`` a missing object gets, so the proxy can't be used to
learn which ids or keys exist in a sibling subsidiary or another tenant.

What this rules out, beyond the entity leak:

- a key that names no owner, or the wrong layout for this route (an
  ``<org>/positive-pay/…`` or ``<org>/tax-forms/…`` key fed to the invoice
  proxy used to stream, because only the org segment was checked);
- a key whose owner row is gone (an orphaned object after a delete);
- a superseded object under a live owner (a replaced invoice document or
  receipt that the bucket still happens to hold).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contract import Contract
from app.models.expense import Expense
from app.models.invoice import Invoice
from app.services.storage import get_file
from app.tenant import apply_entity_scope

FILE_NOT_FOUND = "File not found"


@dataclass(frozen=True)
class _Layout:
    """One key family: ``<org>/[<literal>/]<owner_id>/<tail…>``."""

    literal: str | None
    segments: int
    model: type
    # The row column that holds the ONE current key, or None when an owner may
    # hold many (chat attachments live in message JSON, not on the invoice).
    key_column: str | None


# Mirrors the writers in `services/storage.py` (and the PEPPOL / email-intake
# paths, which stamp the invoice layout directly). `_safe_filename` strips
# every `/`, so each layout has an exact segment count.
_LAYOUTS: dict[str, _Layout] = {
    "invoice": _Layout(None, 3, Invoice, "file_key"),
    "chat": _Layout("chat", 5, Invoice, None),
    "contract": _Layout("contracts", 4, Contract, "file_key"),
    "expense": _Layout("expenses", 4, Expense, "receipt_file_key"),
}


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=FILE_NOT_FOUND)


def owner_id_from_key(file_key: str, *, org_id: uuid.UUID, kind: str) -> uuid.UUID | None:
    """The owning row's id embedded in ``file_key``, or ``None`` if the key is
    not a well-formed ``kind`` key under ``org_id``."""
    layout = _LAYOUTS[kind]
    parts = file_key.split("/")
    if len(parts) != layout.segments or any(p in ("", ".", "..") for p in parts):
        return None
    if parts[0] != str(org_id):
        return None
    idx = 1
    if layout.literal is not None:
        if parts[1] != layout.literal:
            return None
        idx = 2
    try:
        return uuid.UUID(parts[idx])
    except ValueError:
        return None


async def authorize_file_key(
    db: AsyncSession,
    file_key: str,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    kind: str,
    owner_id: uuid.UUID | None = None,
) -> None:
    """404 unless ``file_key`` belongs to a row the caller can see.

    ``owner_id`` binds the key to an id the route already took from its own
    path (the chat route's ``{invoice_id}``): a key under any other owner is
    refused, exactly as the portal's chat route does.
    """
    layout = _LAYOUTS[kind]
    embedded = owner_id_from_key(file_key, org_id=org_id, kind=kind)
    if embedded is None or (owner_id is not None and embedded != owner_id):
        raise _not_found()
    model = layout.model
    cols = [model.id]
    if layout.key_column is not None:
        cols.append(getattr(model, layout.key_column))
    row = (
        await db.execute(
            apply_entity_scope(select(*cols).where(model.id == embedded), model, entity_id)
        )
    ).one_or_none()
    if row is None:
        raise _not_found()
    if layout.key_column is not None and row[1] != file_key:
        raise _not_found()


async def serve_owned_file(
    db: AsyncSession,
    file_key: str,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    kind: str,
    owner_id: uuid.UUID | None = None,
) -> Response:
    """:func:`authorize_file_key`, then stream the object — the whole body of
    each proxy route. A missing object is the same 404 as a refused key."""
    await authorize_file_key(
        db, file_key, org_id=org_id, entity_id=entity_id, kind=kind, owner_id=owner_id
    )
    try:
        content, content_type = await get_file(file_key, expected_prefix=f"{org_id}/")
    except Exception:
        raise _not_found() from None
    return Response(content=content, media_type=content_type)
