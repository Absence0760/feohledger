"""Semantic duplicate detection via pgvector.

Complements the rule-based duplicate check in `invoice_warnings.py` (which
matches on exact `vendor_name + invoice_number`). Catches near-duplicates
where text overlap is very high but strings differ slightly — re-uploads,
resends from the same vendor with one field changed, OCR-whitespace drift.

The threshold (FEOH_DUPLICATE_SIMILARITY_THRESHOLD, default 0.95) is
intentionally tighter than the RAG-retrieval threshold. RAG wants
semantically-related-but-distinct invoices for few-shot context;
duplicate detection wants near-identical invoices. A recurring monthly
invoice from the same vendor with a new amount/date typically lands in
the 0.85-0.93 range and should NOT be flagged.

Multi-entity: the SEARCH is deliberately cross-entity, the WARNING is not
------------------------------------------------------------------------
Every sibling reader is entity-scoped — `rag.retrieve_similar` takes an
`entity_id`, the assistant's `find_invoices_by_text` threads it "so 'search'
can't silently widen past the subsidiary the user has selected", and
`vendor_matching._candidate_query` scopes for the same reason. This one is
different **on purpose**.

The same invoice billed to two subsidiaries of one group is a *real* duplicate
and exactly what a group AP team wants caught — it is the intra-group double-bill
the control exists for. Scoping the search would remove a genuine control to fix
a disclosure problem, so we keep the search wide.

What was actually wrong is what the finding *said*. `matches_to_warning` put the
matched invoice's `invoice_number`, `vendor_name` and `amount` into
`invoice.warnings`, which the detail modal renders — data from an entity the
viewer is otherwise scoped away from. So a cross-entity match now reports only
its existence ("a near-identical invoice exists under another entity") with those
three fields **redacted**; a same-entity match is unchanged and still names them,
because there the viewer is entitled to the detail and needs it to act.

The `duplicate` Exception this raises is in
`api/payments.PAYMENT_BLOCKING_EXCEPTION_TYPES`, so a cross-entity hit still
blocks the payment run until a human clears it. That is the intended behaviour
for a suspected group double-bill — the human sign-off is the control — and the
clearing path is unchanged.
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
from app.services.invoice_warning_catalog import warning

logger = logging.getLogger(__name__)

# Upper bound on candidates returned. Near-duplicates are rare; anything
# past a handful usually means the threshold is wrong.
MAX_CANDIDATES = 10


@dataclass
class DuplicateMatch:
    invoice_id: uuid.UUID
    similarity: float
    vendor_name: str | None
    invoice_number: str | None
    amount: str | None
    #: True when the matched invoice belongs to a DIFFERENT entity than the one
    #: being checked. Its identifying fields are then redacted by
    #: :func:`matches_to_warning` — see this module's docstring.
    cross_entity: bool = False


def _is_cross_entity(query_entity_id: uuid.UUID | None, match_entity_id: uuid.UUID | None) -> bool:
    """Only a match with two KNOWN and DIFFERENT entities is cross-entity.

    A NULL on either side means *unstamped* (a pre-multi-entity row, or a
    single-entity tenant that never had one), not "some other entity". Treating
    unknown as cross-entity would redact the useful detail for every
    single-entity tenant in the product — the overwhelming majority — to protect
    a boundary that doesn't exist for them.
    """
    return (
        query_entity_id is not None
        and match_entity_id is not None
        and query_entity_id != match_entity_id
    )


async def find_semantic_duplicates(
    db: AsyncSession,
    invoice_text: str,
    *,
    exclude_invoice_id: uuid.UUID | None = None,
    threshold: float | None = None,
    entity_id: uuid.UUID | None = None,
) -> list[DuplicateMatch]:
    """Find invoices whose stored embedding is near-identical to the query.

    The search spans the whole tenant on purpose (see the module docstring) —
    the same invoice billed to two subsidiaries is a real duplicate. ``entity_id``
    is the *checking* invoice's entity: it does not filter, it classifies, so
    each match can be reported with or without its identifying fields.

    Returns [] when:
      - RAG is disabled
      - text is empty (scanned PDFs without a text layer)
      - no stored embeddings pass the threshold
    """
    if not settings.rag_enabled or not invoice_text:
        return []

    threshold = threshold if threshold is not None else settings.duplicate_similarity_threshold

    adapter = get_embedding_adapter()
    query = await adapter.embed(invoice_text)

    distance_col = InvoiceEmbedding.embedding.cosine_distance(query.vector).label("distance")
    # Outer-join the invoice purely for its `entity_id` — the classification
    # input. LEFT so an embedding whose invoice row is gone still surfaces as a
    # match (it is evidence of a duplicate either way) rather than vanishing.
    stmt = select(
        InvoiceEmbedding.invoice_id,
        InvoiceEmbedding.corrected_fields,
        distance_col,
        Invoice.entity_id,
    ).outerjoin(Invoice, Invoice.id == InvoiceEmbedding.invoice_id)
    if exclude_invoice_id is not None:
        stmt = stmt.where(InvoiceEmbedding.invoice_id != exclude_invoice_id)
    stmt = stmt.order_by(distance_col).limit(MAX_CANDIDATES)

    rows = (await db.execute(stmt)).all()

    matches: list[DuplicateMatch] = []
    for invoice_id, corrected, dist, match_entity_id in rows:
        similarity = max(0.0, 1.0 - float(dist))
        if similarity < threshold:
            # Rows are sorted ascending by distance (= descending by
            # similarity). Once we drop below the threshold, nothing later
            # in the list will match either.
            break
        fields = corrected or {}
        matches.append(
            DuplicateMatch(
                invoice_id=invoice_id,
                similarity=similarity,
                vendor_name=fields.get("vendor_name"),
                invoice_number=fields.get("invoice_number"),
                amount=fields.get("amount"),
                cross_entity=_is_cross_entity(entity_id, match_entity_id),
            )
        )

    if matches:
        logger.info(
            "Duplicate detection: %d near-matches for new invoice (threshold %.2f)",
            len(matches),
            threshold,
        )
    return matches


def matches_to_warning(matches: list[DuplicateMatch]) -> dict[str, Any] | None:
    """Convert a list of DuplicateMatch to an `invoice.warnings` entry.

    **A cross-entity match is reported without its identifying fields.** The
    warning is rendered in the invoice detail modal and its `message` is copied
    verbatim into the `duplicate` Exception's description, so naming the sibling
    entity's `invoice_number` / `vendor_name` / `amount` discloses data from an
    entity the viewer is otherwise scoped away from. Existence is the actionable
    part — "look outside your subsidiary" — and the person who can act on it can
    see both entities.

    The headline is built from the top SAME-entity match when there is one, so a
    within-subsidiary duplicate never loses its detail just because a
    cross-entity match happened to score higher.
    """
    if not matches:
        return None

    same_entity = [m for m in matches if not m.cross_entity]
    cross_entity_count = len(matches) - len(same_entity)

    if same_entity:
        top = same_entity[0]
        # Four base codes rather than one with optional clauses: whether the
        # match's invoice number and vendor name are known are two independent
        # halves of the sentence, and a client cannot concatenate localized
        # fragments. The cross-entity tail is NOT a fifth dimension — it is an
        # ICU plural with a `=0` arm, so one template covers "no siblings" too.
        params: dict = {
            "similarity": f"{top.similarity * 100:.0f}",
            "crossEntityCount": cross_entity_count,
        }
        if top.invoice_number:
            params["invoiceNumber"] = top.invoice_number
        if top.vendor_name:
            params["vendorName"] = top.vendor_name
        code = {
            (True, True): "duplicate_similar",
            (True, False): "duplicate_similar_unnamed_vendor",
            (False, True): "duplicate_similar_unnumbered",
            (False, False): "duplicate_similar_unnumbered_unnamed_vendor",
        }[(bool(top.invoice_number), bool(top.vendor_name))]
        flag = warning(code, "warning", **params)
    else:
        # Cross-entity only — say what it is and nothing more.
        top = matches[0]
        flag = warning(
            "duplicate_similar_cross_entity",
            "warning",
            similarity=f"{top.similarity * 100:.0f}",
        )

    return {
        **flag,
        "related_invoices": [
            {
                # The id too: an entity-scoped GET would 404 on it anyway, so
                # surfacing it buys nothing and is one more identifier crossing
                # the boundary.
                "invoice_id": None if m.cross_entity else str(m.invoice_id),
                "invoice_number": None if m.cross_entity else m.invoice_number,
                "vendor_name": None if m.cross_entity else m.vendor_name,
                "amount": None if m.cross_entity else m.amount,
                "similarity": round(m.similarity, 4),
                "cross_entity": m.cross_entity,
            }
            for m in matches
        ],
    }
