"""Unit tests for semantic duplicate detection.

Covers the pure-function contracts (matches_to_warning / DuplicateMatch) and
the find_semantic_duplicates threshold/ordering logic via a mocked pgvector
query (see the section comment lower down for what stays DB-level).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


def test_matches_to_warning_none_when_empty():
    from app.services.duplicate_detection import matches_to_warning

    assert matches_to_warning([]) is None


def test_matches_to_warning_contract():
    """Lock the shape the invoice-list UI + exception queue depend on."""
    from app.services.duplicate_detection import DuplicateMatch, matches_to_warning

    matches = [
        DuplicateMatch(
            invoice_id=uuid.uuid4(),
            similarity=0.9823,
            vendor_name="Northwind Suppliers Ltd",
            invoice_number="INV-2026-00418",
            amount="12480.00",
        ),
        DuplicateMatch(
            invoice_id=uuid.uuid4(),
            similarity=0.9541,
            vendor_name="Northwind Suppliers Ltd",
            invoice_number="INV-2026-00401",
            amount="12480.00",
        ),
    ]
    warning = matches_to_warning(matches)
    assert warning["type"] == "duplicate_similar"
    assert warning["severity"] == "warning"
    # Message surfaces the top match
    assert "98%" in warning["message"]
    assert "INV-2026-00418" in warning["message"]
    assert "Northwind" in warning["message"]

    # All matches preserved in related_invoices for UI display
    assert len(warning["related_invoices"]) == 2
    ri = warning["related_invoices"][0]
    assert set(ri.keys()) == {
        "invoice_id",
        "invoice_number",
        "vendor_name",
        "amount",
        "similarity",
        "cross_entity",
    }
    assert ri["similarity"] == 0.9823  # 4-decimal rounding preserves precision
    assert ri["cross_entity"] is False


def test_matches_to_warning_handles_missing_fields():
    """Embeddings without corrected_fields (unlikely but possible) don't crash."""
    from app.services.duplicate_detection import DuplicateMatch, matches_to_warning

    m = DuplicateMatch(
        invoice_id=uuid.uuid4(),
        similarity=0.96,
        vendor_name=None,
        invoice_number=None,
        amount=None,
    )
    warning = matches_to_warning([m])
    # Message falls back gracefully
    assert "another invoice" in warning["message"]
    assert warning["related_invoices"][0]["vendor_name"] is None


def test_duplicate_threshold_tighter_than_rag_top_k_default():
    """Duplicate detection MUST be stricter than RAG retrieval — otherwise
    every RAG neighbor fires a duplicate warning, which defeats both."""
    from app.config import settings

    assert settings.duplicate_similarity_threshold >= 0.9


def test_max_candidates_bounded():
    """Guard against pathological cases where the whole table could come back."""
    from app.services.duplicate_detection import MAX_CANDIDATES

    assert 1 <= MAX_CANDIDATES <= 20


# ---------------------------------------------------------------------------
# find_semantic_duplicates — the threshold/ordering logic the module exists for
#
# Mock-based: the pgvector query is mocked to return crafted
# (invoice_id, corrected_fields, distance) rows so we exercise the Python
# discrimination/threshold/break logic. The cosine_distance ranking and the
# exclude_invoice_id WHERE are enforced in SQL (a real-DB harness, which this
# suite doesn't have, would be needed to assert those two).
# ---------------------------------------------------------------------------


def _db_returning(rows):
    """Mock AsyncSession whose execute().all() yields the given rows.

    The real query selects four columns — `(invoice_id, corrected_fields,
    distance, entity_id)`; the last is the outer-joined `Invoice.entity_id` that
    classifies a match as same- or cross-entity. Threshold/ordering tests don't
    care about it, so a 3-tuple is padded with `None` (= unstamped, which
    `_is_cross_entity` treats as same-entity). Cross-entity tests pass the full
    4-tuple explicitly.
    """
    padded = [r if len(r) == 4 else (*r, None) for r in rows]
    db = MagicMock()
    result = MagicMock()
    result.all = MagicMock(return_value=padded)
    db.execute = AsyncMock(return_value=result)
    return db


def _patched(*, rag_enabled=True, threshold=0.95):
    """Patch settings + the embedding adapter for find_semantic_duplicates."""
    from app.services import duplicate_detection as dd

    adapter = MagicMock()
    adapter.embed = AsyncMock(return_value=SimpleNamespace(vector=[0.1, 0.2, 0.3]))
    return (
        patch.object(dd.settings, "rag_enabled", rag_enabled),
        patch.object(dd.settings, "duplicate_similarity_threshold", threshold),
        patch.object(dd, "get_embedding_adapter", MagicMock(return_value=adapter)),
    )


async def test_find_semantic_duplicates_returns_empty_when_rag_disabled():
    from app.services.duplicate_detection import find_semantic_duplicates

    p1, p2, p3 = _patched(rag_enabled=False)
    with p1, p2, p3:
        db = _db_returning([(uuid.uuid4(), {}, 0.0)])
        assert await find_semantic_duplicates(db, "some text") == []
    db.execute.assert_not_called()  # short-circuits before any query


async def test_find_semantic_duplicates_returns_empty_for_empty_text():
    from app.services.duplicate_detection import find_semantic_duplicates

    p1, p2, p3 = _patched()
    with p1, p2, p3:
        assert await find_semantic_duplicates(_db_returning([]), "") == []


async def test_find_semantic_duplicates_flags_near_identical_not_recurring():
    """A 0.98 near-identical match is flagged; a 0.90 recurring-but-distinct
    invoice (below the 0.95 duplicate threshold) is not."""
    from app.services.duplicate_detection import find_semantic_duplicates

    identical = (uuid.uuid4(), {"invoice_number": "INV-1", "vendor_name": "Acme"}, 0.02)
    recurring = (uuid.uuid4(), {"invoice_number": "INV-2", "vendor_name": "Acme"}, 0.10)
    p1, p2, p3 = _patched(threshold=0.95)
    with p1, p2, p3:
        matches = await find_semantic_duplicates(_db_returning([identical, recurring]), "text")

    assert [m.invoice_id for m in matches] == [identical[0]]
    assert matches[0].similarity == 0.98
    assert matches[0].invoice_number == "INV-1"
    assert matches[0].vendor_name == "Acme"


async def test_find_semantic_duplicates_breaks_at_first_subthreshold_row():
    """Rows arrive sorted ascending by distance; the loop must stop at the
    first sub-threshold row (and not resurrect a later high-similarity one)."""
    from app.services.duplicate_detection import find_semantic_duplicates

    rows = [
        (uuid.uuid4(), {}, 0.01),  # 0.99 — keep
        (uuid.uuid4(), {}, 0.04),  # 0.96 — keep
        (uuid.uuid4(), {}, 0.20),  # 0.80 — below 0.95 → break here
        (uuid.uuid4(), {}, 0.30),  # never considered
    ]
    p1, p2, p3 = _patched(threshold=0.95)
    with p1, p2, p3:
        matches = await find_semantic_duplicates(_db_returning(rows), "text")

    assert [round(m.similarity, 2) for m in matches] == [0.99, 0.96]


async def test_find_semantic_duplicates_honours_per_call_threshold_override():
    from app.services.duplicate_detection import find_semantic_duplicates

    rows = [(uuid.uuid4(), {}, 0.10)]  # similarity 0.90
    p1, p2, p3 = _patched(threshold=0.95)
    with p1, p2, p3:
        # Default 0.95 excludes it; an explicit 0.80 includes it.
        assert await find_semantic_duplicates(_db_returning(rows), "t") == []
        got = await find_semantic_duplicates(_db_returning(rows), "t", threshold=0.80)
    assert len(got) == 1
    assert got[0].similarity == 0.90


# ---------------------------------------------------------------------------
# Real-Postgres + pgvector: prove the cosine ranking and the exclude_invoice_id
# WHERE the mock-based tests can't (those are enforced in SQL). The mock
# embedding adapter is deterministic: identical text → cosine 1.0.
# ---------------------------------------------------------------------------


async def test_find_semantic_duplicates_pgvector_discriminates_and_excludes_self(realdb):
    from decimal import Decimal

    from app.models.invoice import Invoice
    from app.models.invoice_embedding import InvoiceEmbedding
    from app.services.duplicate_detection import find_semantic_duplicates
    from app.services.embedding_adapters import get_embedding_adapter

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    adapter = get_embedding_adapter()
    dup_text = "Acme Corp invoice INV-700 amount 12480 March 2026"
    distinct_text = "Globex unrelated consulting services 42 dollars January 2026"

    async with mk() as s:
        dup_inv = Invoice(
            organization_id=org_id,
            invoice_number="INV-700",
            vendor_name="Acme",
            amount=Decimal("12480.00"),
            currency="USD",
        )
        other_inv = Invoice(
            organization_id=org_id,
            invoice_number="INV-900",
            vendor_name="Globex",
            amount=Decimal("42.00"),
            currency="USD",
        )
        s.add_all([dup_inv, other_inv])
        await s.flush()
        s.add(
            InvoiceEmbedding(
                invoice_id=dup_inv.id,
                embedding=(await adapter.embed(dup_text)).vector,
                corrected_fields={"invoice_number": "INV-700", "vendor_name": "Acme"},
            )
        )
        s.add(
            InvoiceEmbedding(
                invoice_id=other_inv.id,
                embedding=(await adapter.embed(distinct_text)).vector,
                corrected_fields={"invoice_number": "INV-900", "vendor_name": "Globex"},
            )
        )
        await s.commit()
        dup_id, other_id = dup_inv.id, other_inv.id

    # Query with the duplicate's exact text: the near-identical row is flagged,
    # the semantically-distinct row is not (below the 0.95 threshold).
    async with mk() as s:
        matches = await find_semantic_duplicates(s, dup_text)
    ids = {m.invoice_id for m in matches}
    assert dup_id in ids
    assert other_id not in ids
    assert next(m for m in matches if m.invoice_id == dup_id).similarity > 0.99

    # exclude_invoice_id drops the only match → empty (the WHERE runs in SQL).
    async with mk() as s:
        excluded = await find_semantic_duplicates(s, dup_text, exclude_invoice_id=dup_id)
    assert all(m.invoice_id != dup_id for m in excluded)


# ---------------------------------------------------------------------------
# Multi-entity: the SEARCH stays cross-entity, the WARNING redacts
#
# The same invoice billed to two subsidiaries of one group IS a duplicate and is
# exactly what a group AP team wants caught — scoping the search would remove a
# real control. What must not happen is the warning (rendered in the detail
# modal, and copied verbatim into the payment-blocking `duplicate` Exception's
# description) carrying the sibling entity's invoice_number / vendor_name /
# amount to a viewer scoped away from that entity.
# ---------------------------------------------------------------------------


def test_is_cross_entity_only_when_both_are_known_and_differ():
    """A NULL on either side means *unstamped*, not "some other entity".

    Treating unknown as cross-entity would redact the useful detail for every
    single-entity tenant — the overwhelming majority — to protect a boundary
    that does not exist for them.
    """
    from app.services.duplicate_detection import _is_cross_entity

    a, b = uuid.uuid4(), uuid.uuid4()
    assert _is_cross_entity(a, b) is True
    assert _is_cross_entity(a, a) is False
    assert _is_cross_entity(None, b) is False
    assert _is_cross_entity(a, None) is False
    assert _is_cross_entity(None, None) is False


async def test_find_semantic_duplicates_classifies_but_never_filters_by_entity():
    """The cross-entity row is STILL returned — the control must catch a group
    double-bill — it is only flagged."""
    from app.services.duplicate_detection import find_semantic_duplicates

    mine, theirs = uuid.uuid4(), uuid.uuid4()
    same = (uuid.uuid4(), {"invoice_number": "INV-SAME"}, 0.01, mine)
    other = (uuid.uuid4(), {"invoice_number": "INV-OTHER"}, 0.02, theirs)

    p1, p2, p3 = _patched(threshold=0.95)
    with p1, p2, p3:
        matches = await find_semantic_duplicates(
            _db_returning([same, other]), "text", entity_id=mine
        )

    assert len(matches) == 2, "a cross-entity near-duplicate must not be dropped"
    by_id = {m.invoice_id: m for m in matches}
    assert by_id[same[0]].cross_entity is False
    assert by_id[other[0]].cross_entity is True


async def test_a_single_entity_tenant_is_unaffected():
    """No entity on either side (pre-multi-entity rows, or a tenant that never
    used entities) → nothing is classified cross-entity, nothing is redacted."""
    from app.services.duplicate_detection import find_semantic_duplicates, matches_to_warning

    row = (uuid.uuid4(), {"invoice_number": "INV-1", "vendor_name": "Acme"}, 0.01, None)
    p1, p2, p3 = _patched(threshold=0.95)
    with p1, p2, p3:
        matches = await find_semantic_duplicates(_db_returning([row]), "text", entity_id=None)

    assert matches[0].cross_entity is False
    warning = matches_to_warning(matches)
    assert "INV-1" in warning["message"]
    assert warning["related_invoices"][0]["vendor_name"] == "Acme"


def test_cross_entity_warning_redacts_the_sibling_entitys_fields():
    """The disclosure this exists to stop: no invoice number, vendor name, amount
    or id from another entity may reach `invoice.warnings`."""
    from app.services.duplicate_detection import DuplicateMatch, matches_to_warning

    warning = matches_to_warning(
        [
            DuplicateMatch(
                invoice_id=uuid.uuid4(),
                similarity=0.981,
                vendor_name="Sibling Subsidiary Supplier",
                invoice_number="SUB-B-4471",
                amount="98450.00",
                cross_entity=True,
            )
        ]
    )

    blob = str(warning)
    for secret in ("Sibling Subsidiary Supplier", "SUB-B-4471", "98450.00"):
        assert secret not in blob, f"cross-entity field leaked into the warning: {secret}"

    # It still SAYS there is one — existence is the actionable part.
    assert "another entity" in warning["message"]
    assert "98%" in warning["message"]
    ri = warning["related_invoices"][0]
    assert ri["cross_entity"] is True
    assert ri["invoice_id"] is None
    assert ri["invoice_number"] is None
    assert ri["vendor_name"] is None
    assert ri["amount"] is None
    assert ri["similarity"] == 0.981


def test_a_same_entity_match_keeps_the_headline_even_when_outscored():
    """A within-subsidiary duplicate must not lose its detail just because a
    cross-entity match happened to score higher — the viewer can act on the
    former and needs the identifiers to do it."""
    from app.services.duplicate_detection import DuplicateMatch, matches_to_warning

    warning = matches_to_warning(
        [
            DuplicateMatch(
                invoice_id=uuid.uuid4(),
                similarity=0.995,
                vendor_name="Other Entity Vendor",
                invoice_number="OTHER-1",
                amount="1.00",
                cross_entity=True,
            ),
            DuplicateMatch(
                invoice_id=uuid.uuid4(),
                similarity=0.962,
                vendor_name="My Vendor",
                invoice_number="MINE-1",
                amount="500.00",
                cross_entity=False,
            ),
        ]
    )

    assert "MINE-1" in warning["message"]
    assert "My Vendor" in warning["message"]
    assert "OTHER-1" not in warning["message"]
    assert "Other Entity Vendor" not in warning["message"]
    # …and still tells the reviewer to look outside their subsidiary.
    assert "1 near-identical invoice under another entity" in warning["message"]


def test_cross_entity_count_pluralises():
    from app.services.duplicate_detection import DuplicateMatch, matches_to_warning

    def _m(cross: bool, sim: float):
        return DuplicateMatch(
            invoice_id=uuid.uuid4(),
            similarity=sim,
            vendor_name="V",
            invoice_number="N",
            amount="1.00",
            cross_entity=cross,
        )

    warning = matches_to_warning([_m(False, 0.99), _m(True, 0.98), _m(True, 0.97)])
    assert "2 near-identical invoices under another entity" in warning["message"]
