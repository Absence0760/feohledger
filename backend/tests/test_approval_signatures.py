"""Digital signatures on approvals (SOX non-repudiation).

Two layers:

  * Pure unit tests of the ``approval_signature`` primitives — sign/verify
    round-trip, tamper detection (amount / actor / timestamp), determinism,
    money exactness, and the no-key fail-closed posture.
  * Real-Postgres endpoint tests of
    ``GET /api/audit/invoice/{id}/verify-signatures`` — a genuine
    signature seeded on an ``invoice.approved`` row verifies valid; a
    post-approval amount tamper flips it invalid; RBAC + access-audit hold.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services import approval_signature as sig
from app.services.approval_signature import (
    build_signature_detail,
    check_approval_row,
    sign_approval,
    verify_approval,
)
from app.services.audit import log_action

KEY = "unit-test-signing-key"


def _facts(**over):
    base = dict(
        invoice_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        amount=Decimal("1234.56"),
        actor_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        decision="approved",
        timestamp=datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC),
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Pure primitives
# ---------------------------------------------------------------------------


def test_sign_then_verify_roundtrips():
    facts = _facts()
    signature = sign_approval(signing_key=KEY, **facts)
    assert len(signature) == 64  # hex sha256
    assert verify_approval(signing_key=KEY, signature=signature, **facts) is True


def test_signature_is_deterministic():
    facts = _facts()
    assert sign_approval(signing_key=KEY, **facts) == sign_approval(signing_key=KEY, **facts)


def test_tampered_amount_fails_verification():
    facts = _facts()
    signature = sign_approval(signing_key=KEY, **facts)
    tampered = _facts(amount=Decimal("9999.99"))
    assert verify_approval(signing_key=KEY, signature=signature, **tampered) is False


def test_tampered_actor_fails_verification():
    facts = _facts()
    signature = sign_approval(signing_key=KEY, **facts)
    tampered = _facts(actor_id=uuid.uuid4())
    assert verify_approval(signing_key=KEY, signature=signature, **tampered) is False


def test_tampered_timestamp_fails_verification():
    facts = _facts()
    signature = sign_approval(signing_key=KEY, **facts)
    tampered = _facts(timestamp=datetime(2026, 6, 17, 12, 0, 1, tzinfo=UTC))
    assert verify_approval(signing_key=KEY, signature=signature, **tampered) is False


def test_wrong_key_fails_verification():
    facts = _facts()
    signature = sign_approval(signing_key=KEY, **facts)
    assert verify_approval(signing_key="other-key", signature=signature, **facts) is False


def test_money_exact_equal_amounts_same_digest():
    """100.00 and 100.0 are the same money → same canonical digest (no float)."""
    a = sign_approval(signing_key=KEY, **_facts(amount=Decimal("100.00")))
    b = sign_approval(signing_key=KEY, **_facts(amount=Decimal("100.0")))
    assert a == b


def test_empty_key_skips_signing():
    assert sign_approval(signing_key="", **_facts()) == ""
    # verify with no key fails closed (nothing to verify against)
    assert verify_approval(signing_key="", signature="anything", **_facts()) is False


def test_empty_signature_fails_closed():
    assert verify_approval(signing_key=KEY, signature=None, **_facts()) is False
    assert verify_approval(signing_key=KEY, signature="", **_facts()) is False


@pytest.mark.parametrize("bad", [[1, 2, 3], {"nested": 1}, 7, True, "sígnature"])
def test_malformed_signature_value_fails_closed_without_raising(bad):
    """The stored digest comes out of an unconstrained JSONB column, and
    `hmac.compare_digest` raises TypeError on a non-str AND on a non-ASCII str.
    A truthy-but-malformed value must read as "does not verify" — otherwise a
    single hand-written row takes down every caller that reads it."""
    assert verify_approval(signing_key=KEY, signature=bad, **_facts()) is False


def test_build_signature_detail_shape():
    block = build_signature_detail(signing_key=KEY, **_facts())
    assert block["alg"] == sig.SIGNATURE_ALG
    assert block["signed_fields"] == sig.SIGNED_FIELDS
    assert block["signed_at"] == _facts()["timestamp"].isoformat()
    assert len(block["value"]) == 64
    # No raw amount / PII in the block — just field NAMES.
    assert "amount" not in {k for k in block if k != "signed_fields"}


def test_build_signature_detail_none_without_key():
    assert build_signature_detail(signing_key="", **_facts()) is None


# ---------------------------------------------------------------------------
# check_approval_row — the shared per-row verdict primitive
# ---------------------------------------------------------------------------


def _row_details(**over) -> dict:
    facts = _facts(**over)
    return {"signature": build_signature_detail(signing_key=KEY, **facts)}


def test_check_row_valid():
    facts = _facts()
    check = check_approval_row(
        details=_row_details(),
        invoice_id=facts["invoice_id"],
        amount=facts["amount"],
        actor_id=facts["actor_id"],
        signing_key=KEY,
    )
    assert check.verdict == sig.VERDICT_VALID
    assert check.signed is True
    assert check.valid is True
    assert check.signed_at == facts["timestamp"].isoformat()


@pytest.mark.parametrize("details", [None, {}, {"signature": None}, {"signature": "not-a-dict"}])
def test_check_row_unsigned(details):
    """No signature block → `unsigned`, never `invalid`: there is nothing to
    verify, which is not the same claim as "this was tampered with"."""
    facts = _facts()
    check = check_approval_row(
        details=details,
        invoice_id=facts["invoice_id"],
        amount=facts["amount"],
        actor_id=facts["actor_id"],
        signing_key=KEY,
    )
    assert check.verdict == sig.VERDICT_UNSIGNED
    assert check.signed is False
    assert check.valid is False
    assert check.signed_at is None


@pytest.mark.parametrize("details", [[1, 2, 3], "a-string", 7, True])
def test_check_row_survives_a_non_object_details_column(details):
    """`audit_log.details` is JSONB with no object-shape constraint, so a
    non-object value is reachable — by exactly the direct-DB tamper this feature
    exists to catch. It must read as "no block", never raise: on the population
    sweep one bad row would otherwise 500 the whole period's control test."""
    facts = _facts()
    check = check_approval_row(
        details=details,
        invoice_id=facts["invoice_id"],
        amount=facts["amount"],
        actor_id=facts["actor_id"],
        signing_key=KEY,
    )
    assert check.verdict == sig.VERDICT_UNSIGNED


@pytest.mark.parametrize("bad", [[1, 2, 3], {"nested": 1}, 7, True, "sígnature"])
def test_check_row_survives_a_malformed_signature_value(bad):
    """Same JSONB-tamper vector one level deeper: the block is a dict and the
    timestamp parses, but `value` is not a usable digest. It is a finding."""
    facts = _facts()
    details = {"signature": {"value": bad, "signed_at": facts["timestamp"].isoformat()}}
    check = check_approval_row(
        details=details,
        invoice_id=facts["invoice_id"],
        amount=facts["amount"],
        actor_id=facts["actor_id"],
        signing_key=KEY,
    )
    assert check.verdict == sig.VERDICT_INVALID
    assert check.signed is True


def test_check_row_empty_signature_block_is_invalid_not_unsigned():
    """A row that carries the `signature` key but nothing inside it has had its
    signature STRIPPED — that is a finding, not a row predating signing. (This
    is a deliberate tightening of the per-invoice endpoint's old behaviour,
    which read an empty block as `unsigned`.)"""
    facts = _facts()
    check = check_approval_row(
        details={"signature": {}},
        invoice_id=facts["invoice_id"],
        amount=facts["amount"],
        actor_id=facts["actor_id"],
        signing_key=KEY,
    )
    assert check.verdict == sig.VERDICT_INVALID
    assert check.signed is True


def test_check_row_detects_amount_tamper():
    facts = _facts()
    check = check_approval_row(
        details=_row_details(),
        invoice_id=facts["invoice_id"],
        amount=Decimal("9999.99"),  # invoice amount changed after approval
        actor_id=facts["actor_id"],
        signing_key=KEY,
    )
    assert check.verdict == sig.VERDICT_INVALID
    assert check.signed is True


def test_check_row_detects_actor_swap():
    facts = _facts()
    check = check_approval_row(
        details=_row_details(),
        invoice_id=facts["invoice_id"],
        amount=facts["amount"],
        actor_id=uuid.uuid4(),
        signing_key=KEY,
    )
    assert check.verdict == sig.VERDICT_INVALID


@pytest.mark.parametrize("signed_at", ["not-a-date", 12345, None])
def test_check_row_unparseable_signed_at_is_invalid(signed_at):
    """A block that claims to be signed but carries a corrupt timestamp is a
    finding, not an exception — fail-closed, never a 500."""
    details = _row_details()
    details["signature"]["signed_at"] = signed_at
    facts = _facts()
    check = check_approval_row(
        details=details,
        invoice_id=facts["invoice_id"],
        amount=facts["amount"],
        actor_id=facts["actor_id"],
        signing_key=KEY,
    )
    assert check.verdict == sig.VERDICT_INVALID
    # Only a real string is echoed back.
    assert check.signed_at == (signed_at if isinstance(signed_at, str) else None)


def test_check_row_missing_actor_is_invalid():
    facts = _facts()
    check = check_approval_row(
        details=_row_details(),
        invoice_id=facts["invoice_id"],
        amount=facts["amount"],
        actor_id=None,
        signing_key=KEY,
    )
    assert check.verdict == sig.VERDICT_INVALID


def test_check_row_without_key_is_invalid_not_valid():
    """A signed row can never be declared valid without the key that signed it."""
    facts = _facts()
    check = check_approval_row(
        details=_row_details(),
        invoice_id=facts["invoice_id"],
        amount=facts["amount"],
        actor_id=facts["actor_id"],
        signing_key="",
    )
    assert check.verdict == sig.VERDICT_INVALID


# ---------------------------------------------------------------------------
# Endpoint — real Postgres + ASGI app
# ---------------------------------------------------------------------------


async def _seed_signed_approval(
    mk,
    org_id,
    *,
    amount: Decimal,
    actor_id: uuid.UUID,
    key: str,
    created_at: datetime | None = None,
) -> tuple[uuid.UUID, datetime]:
    """Seed an invoice + an ``invoice.approved`` audit row.

    ``key=""`` seeds the row with NO signature block (the pre-signing shape).
    ``created_at`` stamps the audit row explicitly — the immutability trigger
    forbids UPDATE, so a row that must fall outside a date range has to be
    inserted at that timestamp.
    """
    corr = uuid.uuid4()
    inv_id = uuid.uuid4()
    signed_at = datetime.now(UTC)
    block = build_signature_detail(
        invoice_id=inv_id,
        amount=amount,
        actor_id=actor_id,
        decision="approved",
        timestamp=signed_at,
        signing_key=key,
    )
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                correlation_id=corr,
                invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
                vendor_name="Acme",
                amount=amount,
                currency="USD",
                status=InvoiceStatus.approved,
            )
        )
        if created_at is None:
            await log_action(
                s,
                correlation_id=corr,
                organization_id=org_id,
                actor_id=actor_id,
                action="invoice.approved",
                entity_type="invoice",
                entity_id=inv_id,
                details={"signature": block},
            )
        else:
            s.add(
                AuditLog(
                    correlation_id=corr,
                    organization_id=org_id,
                    actor_id=actor_id,
                    action="invoice.approved",
                    entity_type="invoice",
                    entity_id=inv_id,
                    details={"signature": block},
                    created_at=created_at,
                )
            )
        await s.commit()
    return inv_id, signed_at


@pytest.mark.asyncio
async def test_verify_endpoint_reports_valid(realdb, monkeypatch):
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "approval_signing_key", "endpoint-key")
    mk = realdb.sessionmaker("a")
    actor = realdb.info("a").users["ap_manager"]
    inv_id, _ = await _seed_signed_approval(
        mk, realdb.info("a").org_id, amount=Decimal("500.00"), actor_id=actor, key="endpoint-key"
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/audit/invoice/{inv_id}/verify-signatures")

    assert resp.status_code == 200
    body = resp.json()
    assert body["signing_configured"] is True
    assert len(body["approvals"]) == 1
    row = body["approvals"][0]
    assert row["signed"] is True
    assert row["valid"] is True


@pytest.mark.asyncio
async def test_verify_endpoint_detects_amount_tamper(realdb, monkeypatch):
    """Change the invoice amount AFTER approval → signature no longer verifies."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "approval_signing_key", "endpoint-key")
    mk = realdb.sessionmaker("a")
    actor = realdb.info("a").users["ap_manager"]
    inv_id, _ = await _seed_signed_approval(
        mk, realdb.info("a").org_id, amount=Decimal("500.00"), actor_id=actor, key="endpoint-key"
    )

    # Tamper: bump the persisted invoice amount.
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        inv.amount = Decimal("5000.00")
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/audit/invoice/{inv_id}/verify-signatures")

    assert resp.status_code == 200
    row = resp.json()["approvals"][0]
    assert row["signed"] is True
    assert row["valid"] is False  # tamper detected


@pytest.mark.asyncio
async def test_verify_endpoint_writes_access_audit(realdb, monkeypatch):
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "approval_signing_key", "endpoint-key")
    mk = realdb.sessionmaker("a")
    actor = realdb.info("a").users["ap_manager"]
    inv_id, _ = await _seed_signed_approval(
        mk, realdb.info("a").org_id, amount=Decimal("12.00"), actor_id=actor, key="endpoint-key"
    )

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(f"/api/audit/invoice/{inv_id}/verify-signatures")
    assert resp.status_code == 200

    async with mk() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "audit.viewed")))
            .scalars()
            .all()
        )
    assert any((r.details or {}).get("verify_signatures") for r in rows)


@pytest.mark.asyncio
async def test_verify_endpoint_unknown_invoice_404(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/audit/invoice/{uuid.uuid4()}/verify-signatures")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_verify_endpoint_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get(f"/api/audit/invoice/{uuid.uuid4()}/verify-signatures")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_verify_endpoint_clerk_forbidden(realdb):
    """Admin/CFO only — the auditor privilege. A clerk gets 403."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get(f"/api/audit/invoice/{uuid.uuid4()}/verify-signatures")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_real_approval_flow_signs_and_verifies(realdb, monkeypatch):
    """End-to-end: approve through the real service → row carries a signature
    that the verify endpoint confirms valid."""
    from app.config import settings as cfg
    from app.services.review import approve_invoice

    monkeypatch.setattr(cfg, "approval_signing_key", "flow-key")
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor = realdb.info("a").users["ap_manager"]

    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                correlation_id=uuid.uuid4(),
                invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
                vendor_name="Acme",
                amount=Decimal("777.77"),
                currency="USD",
                status=InvoiceStatus.ready_for_review,
            )
        )
        await s.commit()

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await approve_invoice(
            s, inv, actor_id=actor, actor_name="Manager", actor_roles={"ap_manager"}
        )
        await s.commit()

    # The approved row carries a signature block.
    async with mk() as s:
        row = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == inv_id, AuditLog.action == "invoice.approved"
                )
            )
        ).scalar_one()
    assert row.details["signature"]["alg"] == sig.SIGNATURE_ALG

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/audit/invoice/{inv_id}/verify-signatures")
    assert resp.status_code == 200
    assert resp.json()["approvals"][0]["valid"] is True


# ---------------------------------------------------------------------------
# GET /api/audit/verify-signatures — the period-wide population test
# ---------------------------------------------------------------------------

SWEEP = "/api/audit/verify-signatures"


def _today_range() -> dict:
    today = datetime.now(UTC).date().isoformat()
    return {"start": today, "end": today}


@pytest.mark.asyncio
async def test_sweep_clean_period_reports_no_findings(realdb, monkeypatch):
    """The evidence an auditor wants: every approval in the period verifies."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "approval_signing_key", "sweep-key")
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor = realdb.info("a").users["ap_manager"]
    for amount in (Decimal("10.00"), Decimal("20.50"), Decimal("30.00")):
        await _seed_signed_approval(mk, org_id, amount=amount, actor_id=actor, key="sweep-key")

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(SWEEP, params=_today_range())

    assert resp.status_code == 200
    body = resp.json()
    assert body["signing_configured"] is True
    assert body["approvals_checked"] == 3
    assert body["invoices_covered"] == 3
    assert body["valid"] == 3
    assert body["invalid"] == 0
    assert body["unsigned"] == 0
    assert body["findings"] == []
    assert body["findings_truncated"] is False


@pytest.mark.asyncio
async def test_sweep_finds_tampered_row_without_being_told_which_invoice(realdb, monkeypatch):
    """The gap this closes: a post-approval amount tamper is detectable across a
    period, not only when someone already suspects that one invoice."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "approval_signing_key", "sweep-key")
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor = realdb.info("a").users["ap_manager"]
    clean_id, _ = await _seed_signed_approval(
        mk, org_id, amount=Decimal("10.00"), actor_id=actor, key="sweep-key"
    )
    tampered_id, _ = await _seed_signed_approval(
        mk, org_id, amount=Decimal("500.00"), actor_id=actor, key="sweep-key"
    )

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == tampered_id))).scalar_one()
        inv.amount = Decimal("5000.00")
        await s.commit()

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(SWEEP, params=_today_range())

    assert resp.status_code == 200
    body = resp.json()
    assert body["approvals_checked"] == 2
    assert body["valid"] == 1
    assert body["invalid"] == 1
    assert body["unsigned"] == 0
    assert len(body["findings"]) == 1
    finding = body["findings"][0]
    assert finding["invoice_id"] == str(tampered_id)
    assert finding["verdict"] == "invalid"
    assert finding["actor_id"] == str(actor)
    assert finding["actor"]  # resolved from the control plane
    assert finding["signed_at"]
    assert str(clean_id) not in {f["invoice_id"] for f in body["findings"]}


@pytest.mark.asyncio
async def test_sweep_reports_unsigned_separately_from_invalid(realdb, monkeypatch):
    """A row written before signing was enabled has nothing to verify — it is
    `unsigned`, and must not be miscounted as evidence of tampering."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "approval_signing_key", "sweep-key")
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor = realdb.info("a").users["ap_manager"]
    unsigned_id, _ = await _seed_signed_approval(
        mk, org_id, amount=Decimal("42.00"), actor_id=actor, key=""
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(SWEEP, params=_today_range())

    body = resp.json()
    assert body["invalid"] == 0
    assert body["unsigned"] == 1
    assert body["findings"][0]["invoice_id"] == str(unsigned_id)
    assert body["findings"][0]["verdict"] == "unsigned"
    assert body["findings"][0]["signed_at"] is None


@pytest.mark.asyncio
async def test_sweep_honours_the_date_range(realdb, monkeypatch):
    """An approval outside the window is not part of this period's population."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "approval_signing_key", "sweep-key")
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor = realdb.info("a").users["ap_manager"]
    await _seed_signed_approval(mk, org_id, amount=Decimal("1.00"), actor_id=actor, key="sweep-key")
    old = datetime.now(UTC) - timedelta(days=400)
    await _seed_signed_approval(
        mk, org_id, amount=Decimal("2.00"), actor_id=actor, key="sweep-key", created_at=old
    )

    async with realdb.client(key="a", role="admin") as c:
        today = await c.get(SWEEP, params=_today_range())
        old_day = await c.get(
            SWEEP, params={"start": old.date().isoformat(), "end": old.date().isoformat()}
        )

    assert today.json()["approvals_checked"] == 1
    assert old_day.json()["approvals_checked"] == 1


@pytest.mark.asyncio
async def test_sweep_end_date_is_inclusive_of_the_whole_day(realdb, monkeypatch):
    """A row written late on the `end` day is inside the period (mirrors export)."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "approval_signing_key", "sweep-key")
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor = realdb.info("a").users["ap_manager"]
    day = datetime.now(UTC) - timedelta(days=10)
    late = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    await _seed_signed_approval(
        mk, org_id, amount=Decimal("7.00"), actor_id=actor, key="sweep-key", created_at=late
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(
            SWEEP, params={"start": day.date().isoformat(), "end": day.date().isoformat()}
        )

    assert resp.json()["approvals_checked"] == 1


@pytest.mark.asyncio
async def test_sweep_counts_are_never_truncated_by_the_findings_limit(realdb, monkeypatch):
    """`limit` bounds the response body, never the population test itself."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "approval_signing_key", "sweep-key")
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor = realdb.info("a").users["ap_manager"]
    for _ in range(3):
        await _seed_signed_approval(mk, org_id, amount=Decimal("5.00"), actor_id=actor, key="")

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(SWEEP, params={**_today_range(), "limit": 1})

    body = resp.json()
    assert body["unsigned"] == 3  # full population
    assert len(body["findings"]) == 1  # bounded body
    assert body["findings_truncated"] is True


@pytest.mark.asyncio
async def test_sweep_writes_access_audit_with_counts_only(realdb, monkeypatch):
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "approval_signing_key", "sweep-key")
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor = realdb.info("a").users["ap_manager"]
    await _seed_signed_approval(mk, org_id, amount=Decimal("3.00"), actor_id=actor, key="")

    async with realdb.client(key="a", role="admin") as c:
        assert (await c.get(SWEEP, params=_today_range())).status_code == 200

    async with mk() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "audit.viewed")))
            .scalars()
            .all()
        )
    row = next(r for r in rows if (r.details or {}).get("scope") == "range")
    assert row.details["verify_signatures"] == 1
    assert row.details["unsigned"] == 1
    assert row.details["invalid"] == 0
    # PII-free: counts + scope only, no invoice number / vendor / amount.
    assert set(row.details) == {"scope", "verify_signatures", "invalid", "unsigned"}


async def _seed_corrupt_approval(mk, org_id, *, actor_id: uuid.UUID, details) -> uuid.UUID:
    """Seed an approved invoice + an `invoice.approved` row with hand-written
    `details` — the direct-DB tamper the verification surfaces exist to catch."""
    corr = uuid.uuid4()
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                correlation_id=corr,
                invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
                vendor_name="Acme",
                amount=Decimal("8.00"),
                currency="USD",
                status=InvoiceStatus.approved,
            )
        )
        s.add(
            AuditLog(
                correlation_id=corr,
                organization_id=org_id,
                actor_id=actor_id,
                action="invoice.approved",
                entity_type="invoice",
                entity_id=inv_id,
                details=details,
            )
        )
        await s.commit()
    return inv_id


@pytest.mark.parametrize(
    ("details", "expected_bucket"),
    [
        # `details` itself is not a JSON object.
        ([1, 2, 3], "unsigned"),
        # The block is well-formed except for a digest that isn't a digest.
        ({"signature": {"value": ["forged"], "signed_at": "2026-06-17T12:00:00+00:00"}}, "invalid"),
    ],
    ids=["non_object_details", "malformed_signature_value"],
)
@pytest.mark.asyncio
async def test_one_corrupt_row_does_not_take_down_the_whole_control_test(
    realdb, monkeypatch, details, expected_bucket
):
    """`audit_log.details` is JSONB with no shape constraint at any level. A
    corrupt row must surface as its own finding on BOTH surfaces — not 500 the
    sweep and take the rest of the period's evidence down with it."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "approval_signing_key", "sweep-key")
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor = realdb.info("a").users["ap_manager"]
    await _seed_signed_approval(mk, org_id, amount=Decimal("8.00"), actor_id=actor, key="sweep-key")
    corrupt_inv = await _seed_corrupt_approval(mk, org_id, actor_id=actor, details=details)

    async with realdb.client(key="a", role="admin") as c:
        sweep = await c.get(SWEEP, params=_today_range())
        single = await c.get(f"/api/audit/invoice/{corrupt_inv}/verify-signatures")

    assert sweep.status_code == 200
    body = sweep.json()
    assert body["approvals_checked"] == 2
    assert body["valid"] == 1  # the good row's evidence survives
    assert body[expected_bucket] == 1
    assert body["findings"][0]["invoice_id"] == str(corrupt_inv)
    assert body["findings"][0]["verdict"] == expected_bucket

    assert single.status_code == 200
    approval = single.json()["approvals"][0]
    assert approval["valid"] is False
    assert approval["signed"] is (expected_bucket == "invalid")


@pytest.mark.asyncio
async def test_sweep_requires_a_date(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(SWEEP)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_sweep_rejects_inverted_range(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(SWEEP, params={"start": "2026-02-01", "end": "2026-01-01"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_sweep_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get(SWEEP, params=_today_range())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_sweep_clerk_forbidden(realdb):
    """Admin/CFO only — the auditor privilege, same as the per-invoice check."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get(SWEEP, params=_today_range())
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_sweep_does_not_see_another_tenants_approvals(realdb, monkeypatch):
    """Tenant isolation at the data layer: the sweep reads only its own tenant DB."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "approval_signing_key", "sweep-key")
    await _seed_signed_approval(
        realdb.sessionmaker("b"),
        realdb.info("b").org_id,
        amount=Decimal("99.00"),
        actor_id=realdb.info("b").users["ap_manager"],
        key="sweep-key",
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(SWEEP, params=_today_range())

    assert resp.json()["approvals_checked"] == 0
