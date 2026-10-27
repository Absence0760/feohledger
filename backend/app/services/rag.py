"""Retrieval-Augmented Generation for invoice extraction.

Three entry points:

  - extract_invoice_text(file_bytes) — PyMuPDF text extraction for embedding.
  - store_embedding(db, invoice, file_bytes) — embed + upsert at correction time.
  - retrieve_similar(db, query_text, k) — cosine-nearest search for few-shot context.

See backend/docs/ai-extraction.md § RAG with pgvector.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.invoice import Invoice
from app.models.invoice_embedding import InvoiceEmbedding
from app.services.embedding_adapters import get_embedding_adapter

logger = logging.getLogger(__name__)

# Fields snapshotted onto InvoiceEmbedding.corrected_fields and fed back
# as few-shot context. Keeping it narrow so the prompt stays cheap.
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "vendor_name",
    "invoice_number",
    "amount",
    "currency",
    "invoice_date",
    "due_date",
    "payment_terms",
    "tax_rate",
    "payment_method",
    "gl_account",
    "cost_center",
    "po_number",
)


@dataclass
class Neighbor:
    invoice_id: uuid.UUID
    similarity: float
    vendor_name: str | None
    corrected_fields: dict[str, Any]


def extract_invoice_text(file_bytes: bytes) -> str:
    """Pull the text layer out of a PDF (or return empty for scanned docs).

    Anything that is not a PDF — an image, a UBL / CII ``.xml`` e-invoice —
    has no text layer to pull, so it is empty without asking PyMuPDF. Opening
    those as a PDF failed every time, and logged a parser warning for every
    image or e-invoice upload that described nothing wrong (#575).
    """
    # The header may follow a little leading junk; readers look in the first KiB.
    if not file_bytes or b"%PDF-" not in file_bytes[:1024]:
        return ""
    try:
        import pymupdf  # PyMuPDF
    except ImportError:
        logger.warning("PyMuPDF not available — skipping text extraction for RAG.")
        return ""

    text_chunks: list[str] = []
    try:
        with pymupdf.open(stream=file_bytes, filetype="pdf") as doc:
            for page in doc:
                text_chunks.append(page.get_text("text"))
    except Exception as exc:
        # Class only, never the message — a parser error on an uploaded invoice
        # PDF could otherwise echo document content into the log (PII-out-of-logs).
        logger.warning("PyMuPDF text extraction failed: %s", exc.__class__.__name__)
        return ""

    return "\n".join(text_chunks).strip()


def _snapshot(invoice: Invoice) -> dict[str, Any]:
    snap: dict[str, Any] = {}
    for name in SNAPSHOT_FIELDS:
        value = getattr(invoice, name, None)
        if value is None:
            continue
        # Coerce types that JSON can't serialize directly.
        snap[name] = str(value) if not isinstance(value, (str, int, float, bool)) else value
    return snap


async def store_embedding(
    db: AsyncSession,
    invoice: Invoice,
    file_bytes: bytes | None = None,
    text_content: str | None = None,
) -> InvoiceEmbedding | None:
    """Embed an invoice's text and upsert it into the tenant's RAG store.

    Called at correction time (from services/review.py) with the invoice's
    FINAL corrected fields. Silently no-ops when RAG is disabled or the
    document has no text layer to embed.
    """
    if not settings.rag_enabled:
        return None

    text = text_content
    if text is None:
        text = extract_invoice_text(file_bytes or b"")
    if not text:
        logger.debug("No text available for invoice %s — skipping embedding.", invoice.id)
        return None

    adapter = get_embedding_adapter()
    result = await adapter.embed(text)

    existing = (
        await db.execute(select(InvoiceEmbedding).where(InvoiceEmbedding.invoice_id == invoice.id))
    ).scalar_one_or_none()

    snapshot = _snapshot(invoice)

    if existing is None:
        row = InvoiceEmbedding(
            invoice_id=invoice.id,
            vendor_id=invoice.vendor_id,
            embedding=result.vector,
            corrected_fields=snapshot,
            model=result.model,
        )
        db.add(row)
        return row

    existing.embedding = result.vector
    existing.vendor_id = invoice.vendor_id
    existing.corrected_fields = snapshot
    existing.model = result.model
    return existing


async def retrieve_similar(
    db: AsyncSession,
    query_text: str,
    *,
    k: int | None = None,
    exclude_invoice_id: uuid.UUID | None = None,
    entity_id: uuid.UUID | None = None,
) -> list[Neighbor]:
    """Find the top-k most semantically similar past invoices.

    Uses pgvector's cosine distance (`<=>`). Empty query text → no results.

    ``entity_id`` (when not ``None``) narrows the search to a single
    subsidiary by joining each embedding back to its ``Invoice.entity_id`` —
    the same multi-entity boundary every other read honors. ``None`` keeps the
    consolidated (all-entities) view, which the extraction few-shot caller
    relies on (it has no entity context and cross-entity examples are fine).
    ``InvoiceEmbedding`` carries no ``entity_id`` of its own, so we resolve it
    through the owning invoice rather than denormalising the column.
    """
    if not settings.rag_enabled or not query_text:
        return []

    k = k or settings.rag_top_k

    adapter = get_embedding_adapter()
    query = await adapter.embed(query_text)

    # cosine_distance returns 0 for identical vectors, 2 for opposite;
    # similarity = 1 - distance / 2 for normalized vectors, but pgvector's
    # cosine_distance already returns a value in [0, 2]. We expose
    # similarity = 1 - distance which is the standard cosine similarity
    # when inputs are unit-normalized.
    distance = InvoiceEmbedding.embedding.cosine_distance(query.vector).label("distance")

    stmt = select(
        InvoiceEmbedding.invoice_id,
        InvoiceEmbedding.vendor_id,
        InvoiceEmbedding.corrected_fields,
        distance,
    )
    if entity_id is not None:
        # Scope to the selected subsidiary via the owning invoice. The join key
        # is the embedding's unique invoice_id FK, so it can't widen results.
        stmt = stmt.join(Invoice, Invoice.id == InvoiceEmbedding.invoice_id).where(
            Invoice.entity_id == entity_id
        )
    if exclude_invoice_id is not None:
        stmt = stmt.where(InvoiceEmbedding.invoice_id != exclude_invoice_id)
    stmt = stmt.order_by(distance).limit(k)

    rows = (await db.execute(stmt)).all()

    neighbors: list[Neighbor] = []
    for row in rows:
        invoice_id, vendor_id, corrected_fields, dist = row
        similarity = max(0.0, 1.0 - float(dist))
        vendor_name = (corrected_fields or {}).get("vendor_name")
        neighbors.append(
            Neighbor(
                invoice_id=invoice_id,
                similarity=similarity,
                vendor_name=vendor_name,
                corrected_fields=dict(corrected_fields or {}),
            )
        )
    return neighbors


def build_few_shot_prompt(neighbors: list[Neighbor]) -> str:
    """Render neighbors as a string block the extraction adapter can prepend.

    Returns empty string when there are no neighbors so the adapter can
    cheaply check `if few_shot: ...` before deciding to change the prompt.
    """
    if not neighbors:
        return ""

    lines = [
        "Here are similar past invoices that were extracted correctly. "
        "Use them as hints where the current invoice is ambiguous, but do "
        "NOT copy values blindly — the current document is still the source "
        "of truth.",
        "",
    ]
    for i, n in enumerate(neighbors, 1):
        lines.append(f"Example {i} (similarity {n.similarity:.2f}):")
        for k, v in n.corrected_fields.items():
            lines.append(f"  {k}: {v}")
        lines.append("")
    return "\n".join(lines)


def neighbors_to_metadata(neighbors: list[Neighbor]) -> list[dict[str, Any]]:
    """Shape for persistence on InvoiceExtractionResult.priors_metadata."""
    return [
        {
            "invoice_id": str(n.invoice_id),
            "similarity": round(n.similarity, 4),
            "vendor_name": n.vendor_name,
            "invoice_number": n.corrected_fields.get("invoice_number"),
            "amount": n.corrected_fields.get("amount"),
        }
        for n in neighbors
    ]
