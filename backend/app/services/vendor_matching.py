"""Vendor matching service — fuzzy match vendor names from invoices against existing vendors."""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.tenant import apply_entity_scope

# A vendor in one of these states is a legitimate match target. `inactive` /
# `rejected` rows are deliberately dead and must never absorb an invoice.
_MATCHABLE_STATUSES = ("active", "unverified")


def _normalize(name: str) -> str:
    """Normalize a vendor name for comparison."""
    name = name.lower().strip()
    # Remove common suffixes
    for suffix in (
        " inc",
        " inc.",
        " llc",
        " ltd",
        " ltd.",
        " corp",
        " corp.",
        " co",
        " co.",
        " company",
        " group",
        " plc",
        " gmbh",
        " pty",
        " pty.",
        " limited",
        " incorporated",
    ):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    # Remove punctuation
    name = "".join(c for c in name if c.isalnum() or c == " ")
    # Collapse whitespace
    return " ".join(name.split())


def _similarity(a: str, b: str) -> float:
    """Simple similarity score between two normalized strings (0-1).

    Uses token overlap (Jaccard similarity on words).
    """
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def _candidate_query(entity_id: uuid.UUID | None) -> Select:
    """Base vendor-candidate query for one subsidiary.

    **Entity scoping.** ``vendors`` carries a nullable ``entity_id``
    (``EntityMixin``), so in a multi-entity tenant an unscoped lookup can hand
    an invoice a vendor belonging to a *different* subsidiary. When the caller
    supplies the invoice's entity, candidates are narrowed to that entity **∪
    rows whose ``entity_id`` is NULL**.

    A NULL means something different here than it does on ``gl_accounts``,
    where it is a deliberate "shared chart" marker. On ``vendors`` it means the
    row was never stamped with a subsidiary: a pre-multi-entity row that
    migration ``0029``'s backfill didn't reach, or one auto-created from an
    invoice that itself carried no entity. It must nonetheless stay matchable
    from every entity — a supplier is a real-world counterparty, not
    subsidiary-private data, and dropping unstamped rows from the candidate set
    would not fail loudly: it would silently mint a *duplicate* vendor,
    splitting the supplier's spend rollup and giving it a second, independently
    editable bank-detail record. Under-matching here is the more dangerous
    failure, so NULL is admitted.

    ``entity_id is None`` (an unstamped invoice, or a caller with no entity in
    hand) is a passthrough — exactly the pre-multi-entity behaviour, which is
    also what every single-entity tenant sees, since there all vendors are
    either under the one default entity or NULL.

    The ordering makes the pick deterministic and prefers the caller's own
    subsidiary over an unstamped row when both could match.
    """
    query = select(Vendor).where(Vendor.status.in_(_MATCHABLE_STATUSES))
    query = apply_entity_scope(query, Vendor, entity_id, include_shared=True)
    return query.order_by(Vendor.entity_id.is_(None), Vendor.created_at, Vendor.id)


async def get_vendor_in_scope(
    db: AsyncSession,
    vendor_id: uuid.UUID,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
) -> Vendor:
    """Load a vendor a caller is LINKING to a row in ``entity_id``, or 404.

    The one owner of "may this row point at that supplier?" for every router
    that stores a ``vendor_id`` link (intake, catalogs, contracts). An
    org-only lookup accepted a sibling subsidiary's supplier record, and the
    link then rode onward — onto a requisition and PO, or a contract's spend
    rollup — committing subsidiary A's spend against B's vendor (and its bank
    details). Entity isolation is a data-layer invariant, so it is checked
    here, before the referencing row is written.

    ``entity_id`` is the entity the referencing row belongs to (or will, on
    create) — not necessarily the caller's selector, since a by-id edit may
    come from the consolidated view. Admitted: that entity ∪ unstamped
    (NULL) vendors, the :func:`_candidate_query` rule above — excluding an
    unstamped supplier would push the caller into minting a duplicate.
    ``entity_id is None`` is a passthrough, so single-entity tenants and the
    consolidated view are unchanged.

    An out-of-scope id gets the SAME opaque 404 as an unknown one, so the
    response cannot enumerate another subsidiary's vendors.
    """
    vendor = (
        await db.execute(
            apply_entity_scope(
                select(Vendor).where(Vendor.id == vendor_id, Vendor.organization_id == org_id),
                Vendor,
                entity_id,
                include_shared=True,
            )
        )
    ).scalar_one_or_none()
    if vendor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")
    return vendor


async def match_vendor(
    db: AsyncSession,
    vendor_name: str,
    vendor_tax_id: str | None = None,
    vendor_address: str | None = None,
    organization_id: uuid.UUID | None = None,
    entity_id: uuid.UUID | None = None,
) -> tuple[Vendor | None, float]:
    """Find the best matching vendor for a given name.

    ``entity_id`` is the subsidiary the match is being made *for* — normally
    the invoice's own ``entity_id``. It confines every candidate lookup to that
    entity (plus unstamped rows); see ``_candidate_query``. Passing ``None``
    searches the whole tenant.

    Returns (vendor, confidence) where confidence is 0-1.
    Returns (None, 0) if no reasonable match found.
    """
    if not vendor_name or not vendor_name.strip():
        return None, 0.0

    # First: try exact tax_id match (highest confidence). `first()` over an
    # ordered, limited query rather than `scalar_one_or_none()`: the same
    # supplier legitimately exists once per subsidiary, and a duplicated
    # tax_id must not turn invoice creation into a 500.
    if vendor_tax_id:
        result = await db.execute(
            _candidate_query(entity_id).where(Vendor.tax_id == vendor_tax_id).limit(1)
        )
        tax_match = result.scalars().first()
        if tax_match:
            return tax_match, 1.0

    # Second: try exact name match (case-insensitive)
    result = await db.execute(
        _candidate_query(entity_id)
        .where(func.lower(Vendor.name) == vendor_name.lower().strip())
        .limit(1)
    )
    exact_match = result.scalars().first()
    if exact_match:
        return exact_match, 0.98

    # Third: fuzzy match across the in-scope candidates
    result = await db.execute(_candidate_query(entity_id))
    vendors = result.scalars().all()

    normalized_input = _normalize(vendor_name)
    best_vendor: Vendor | None = None
    best_score = 0.0

    for v in vendors:
        score = _similarity(normalized_input, _normalize(v.name))

        # Address can only ever *boost* confidence — never drag a strong name
        # match down. A non-matching listed address must not penalize a perfect
        # name match (which the old `name*0.8 + addr*0.2` blend did, turning a
        # 1.0 into 0.8). Take the better of the name score and the blend.
        if vendor_address and v.address:
            addr_score = _similarity(
                vendor_address.lower(),
                v.address.lower(),
            )
            score = max(score, score * 0.8 + addr_score * 0.2)

        # Strict `>` keeps the first row seen on a tie, and `_candidate_query`
        # orders the invoice's own entity ahead of unstamped rows — so an
        # equally-good shared candidate never displaces the entity's own.
        if score > best_score:
            best_score = score
            best_vendor = v

    # Only return if score is above threshold
    if best_score >= 0.6:
        return best_vendor, best_score

    return None, 0.0


async def match_and_link_vendor(
    db: AsyncSession,
    invoice: Invoice,
    organization_id: uuid.UUID,
    source: str = "ai_extracted",
) -> tuple[Vendor | None, str]:
    """Match an invoice's vendor_name to an existing vendor and link them.

    ``source`` stamps the provenance of a vendor this call has to create
    (``Vendor.source``): ``ai_extracted`` for the extraction pipeline,
    ``manual`` when a human keyed the invoice in by hand. It does not affect
    matching — only the row written when nothing matches.

    Matching is confined to the invoice's own subsidiary (``invoice.entity_id``)
    plus unstamped vendors — see ``_candidate_query``. Every caller passes the
    invoice, so the entity is derived here rather than threaded through each
    call site; that also means an inter-company *mirror* payable, which sits
    under the counterparty entity, matches against the counterparty's vendors
    without any call-site knowledge of inter-company routing.

    Returns (vendor, action) where action is:
    - "linked" — matched to existing vendor
    - "created" — new unverified vendor created
    - "none" — no vendor name on invoice
    """
    if not invoice.vendor_name:
        return None, "none"

    vendor, confidence = await match_vendor(
        db,
        vendor_name=invoice.vendor_name,
        vendor_tax_id=invoice.vendor_tax_id,
        vendor_address=invoice.vendor_address,
        entity_id=invoice.entity_id,
    )

    if vendor and confidence >= 0.8:
        # High confidence — auto-link
        invoice.vendor_id = vendor.id
        return vendor, "linked"

    if vendor and confidence >= 0.6:
        # Medium confidence — link but flag for review
        invoice.vendor_id = vendor.id
        return vendor, "linked"

    # No match — create unverified vendor from invoice data. It inherits the
    # invoice's entity so the auto-created vendor lands in the same subsidiary
    # as the invoice it came from (multi-entity Phase 2).
    new_vendor = Vendor(
        name=invoice.vendor_name,
        address=invoice.vendor_address,
        tax_id=invoice.vendor_tax_id,
        status="unverified",
        source=source,
        organization_id=organization_id,
        entity_id=invoice.entity_id,
    )
    db.add(new_vendor)
    await db.flush()

    invoice.vendor_id = new_vendor.id
    return new_vendor, "created"
