"""A payment records the payee it paid; a void flags a re-payment to anyone else.

Decisions §383. A vendor merge moves `paid` invoices to the canonical vendor
(§379), and `POST /api/payments/{id}/void` sends a paid invoice back to
`approved`, where a re-payment reads whichever vendor it names now. Before the
stamp, a paid invoice merged onto another vendor and voided was re-paid to the
canonical's bank details with no flag. Pinned here:

* the fingerprint is keyed, canonical, tenant-scoped and carries no number;
  its key is its own (a JWT key rotation changes nothing), it rotates through
  the keyring without a false flag, and a stamp nobody can recompute (an
  unknown key id, or no key configured) flags;
* every step that releases money stamps the payee: the rail dispatch, the card
  dispatch, the NACHA export, the single and run-level record of a payment made
  outside FeohLedger, and an ERP-reported payment; an already-stamped payment
  recorded later keeps the payee it was sent to;
* a merge never moves the stamp;
* merge → void raises the blocking `payee_changed_since_paid` flag, raised by the
  merge's actor, who cannot clear it; an independent person can, and the run
  then goes through; the ERP push is refused meanwhile;
* the same vendor with the same bank details raises nothing; with other bank
  details it raises the same flag, raised by the bank change's approver, and
  only that one;
* an unrelated open fraud flag no longer absorbs the payee flag;
* a payment from before the stamp is never compared (nothing is backfilled,
  §141), but a merge into its vendor since it was booked flags
  `payee_unrecorded_after_merge`.

Real Postgres over real HTTP (`realdb`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from app.config import settings
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.services.payee_stamp import (
    PAYEE_CHANGED_SINCE_PAID,
    PAYEE_UNRECORDED_AFTER_MERGE,
    bank_fingerprint,
    fingerprint_key_id,
    fingerprint_matches,
)
from app.utils.dates import utc_today

TENANT = "a"
_MERGE_URL = "/api/enrichment/vendors/consolidation/merge"

BANK_A = {"routing_number": "021000021", "account_number": "12345678", "bank_name": "First"}
BANK_B = {"routing_number": "011000015", "account_number": "87654321", "bank_name": "Second"}


# ---------------------------------------------------------------------------
# The fingerprint
# ---------------------------------------------------------------------------


def test_the_fingerprint_is_keyed_canonical_and_carries_no_number():
    import hashlib
    import json

    org = uuid.uuid4()
    fp = bank_fingerprint(BANK_A, organization_id=org)
    kid = fingerprint_key_id(settings.payee_fingerprint_key)
    assert fp.startswith(f"v2:{kid}:") and len(fp) == len(f"v2:{kid}:") + 64
    # Fits the column migration 0128 made.
    assert len(fp) <= Payment.__table__.columns["payee_bank_fingerprint"].type.length
    # Not an unkeyed hash of the blob, which a routing + account number
    # brute-forces.
    plain = hashlib.sha256(json.dumps(BANK_A, sort_keys=True).encode()).hexdigest()
    assert plain not in fp
    for value in BANK_A.values():
        assert value not in fp
    # Key order is not a change; any value is.
    assert bank_fingerprint(dict(reversed(list(BANK_A.items()))), organization_id=org) == fp
    assert bank_fingerprint({**BANK_A, "account_number": "12345679"}, organization_id=org) != fp
    assert bank_fingerprint({**BANK_A, "counterparty_id": "cp_1"}, organization_id=org) != fp
    # Tenant-scoped: one account paid by two tenants does not fingerprint alike.
    assert bank_fingerprint(BANK_A, organization_id=uuid.uuid4()) != fp
    # No bank details is one value, however it is spelled.
    assert bank_fingerprint(None, organization_id=org) == bank_fingerprint({}, organization_id=org)
    assert bank_fingerprint(None, organization_id=org) != fp


KEY_1 = "1" * 64
KEY_2 = "2" * 64


def test_the_fingerprint_does_not_depend_on_the_jwt_key(monkeypatch):
    """FEOH_SECRET_KEY rotates every 90 days (docs/secrets-rotation.md). Keyed
    by it, every void after a rotation read as a bank change and opened a false
    fraud flag; the fingerprint has its own key now."""
    org = uuid.uuid4()
    monkeypatch.setattr(settings, "payee_fingerprint_key", KEY_1)
    monkeypatch.setattr(settings, "secret_key", "j" * 64)
    before = bank_fingerprint(BANK_A, organization_id=org)
    monkeypatch.setattr(settings, "secret_key", "k" * 64)
    assert bank_fingerprint(BANK_A, organization_id=org) == before
    assert fingerprint_matches(before, BANK_A, organization_id=org)


def test_a_rotation_through_the_keyring_keeps_old_stamps_equal(monkeypatch):
    org = uuid.uuid4()
    monkeypatch.setattr(settings, "payee_fingerprint_key", KEY_1)
    monkeypatch.setattr(settings, "payee_fingerprint_previous_keys", "")
    old = bank_fingerprint(BANK_A, organization_id=org)

    monkeypatch.setattr(settings, "payee_fingerprint_key", KEY_2)
    monkeypatch.setattr(settings, "payee_fingerprint_previous_keys", f" {KEY_1} ,")
    new = bank_fingerprint(BANK_A, organization_id=org)
    # New stamps name the new key...
    assert new != old
    assert new.split(":")[1] == fingerprint_key_id(KEY_2) != fingerprint_key_id(KEY_1)
    # ...and old ones still compare under the key that made them, both ways.
    assert fingerprint_matches(old, BANK_A, organization_id=org)
    assert fingerprint_matches(new, BANK_A, organization_id=org)
    assert not fingerprint_matches(old, BANK_B, organization_id=org)
    assert not fingerprint_matches(old, BANK_A, organization_id=uuid.uuid4())


def test_a_stamp_that_cannot_be_recomputed_does_not_match(monkeypatch):
    """Fail closed: an unknown key id (a retired key dropped from the keyring
    too early), another scheme, a malformed value or no fingerprint at all."""
    org = uuid.uuid4()
    monkeypatch.setattr(settings, "payee_fingerprint_key", KEY_1)
    monkeypatch.setattr(settings, "payee_fingerprint_previous_keys", "")
    old = bank_fingerprint(BANK_A, organization_id=org)

    # Replaced, not rotated: the old key is in neither setting.
    monkeypatch.setattr(settings, "payee_fingerprint_key", KEY_2)
    assert not fingerprint_matches(old, BANK_A, organization_id=org)

    monkeypatch.setattr(settings, "payee_fingerprint_key", KEY_1)
    digest = old.split(":")[2]
    for stored in (
        None,
        "",
        f"v1:{digest}",
        f"v3:{fingerprint_key_id(KEY_1)}:{digest}",
        "v2:",
        "v2::" + digest,
    ):
        assert not fingerprint_matches(stored, BANK_A, organization_id=org), stored


def test_no_key_means_no_fingerprint(monkeypatch):
    """No hardcoded fallback: with FEOH_PAYEE_FINGERPRINT_KEY empty nothing is
    fingerprinted, and nothing matches (a deployed env refuses to boot)."""
    org = uuid.uuid4()
    monkeypatch.setattr(settings, "payee_fingerprint_key", KEY_1)
    stored = bank_fingerprint(BANK_A, organization_id=org)
    monkeypatch.setattr(settings, "payee_fingerprint_key", "  ")
    assert bank_fingerprint(BANK_A, organization_id=org) is None
    # Even with the old key still in the keyring, the stamp compares; without
    # any key at all it cannot.
    monkeypatch.setattr(settings, "payee_fingerprint_previous_keys", KEY_1)
    assert fingerprint_matches(stored, BANK_A, organization_id=org)
    monkeypatch.setattr(settings, "payee_fingerprint_previous_keys", "")
    assert not fingerprint_matches(stored, BANK_A, organization_id=org)


def test_the_stamp_rides_the_order_restored_after_a_database_error():
    """A dispatch whose order reached the processor keeps the payee it was sent
    to even when a database error rolls the attempt back."""
    from app.api.payments import _ORDER_FIELDS

    assert {"payee_vendor_id", "payee_bank_fingerprint", "payee_recorded_at"} <= set(_ORDER_FIELDS)


def test_the_two_findings_are_catalogued_as_fraud_flags():
    from app.services.invoice_warning_catalog import EXCEPTION_PREFIX, exception_finding, spec_for

    for code in (PAYEE_CHANGED_SINCE_PAID, PAYEE_UNRECORDED_AFTER_MERGE):
        finding = exception_finding(code)
        assert finding["code"] == EXCEPTION_PREFIX + code
        assert finding["params"] == {}
        assert spec_for(EXCEPTION_PREFIX + code).type == "fraud_flag"


def test_the_payee_column_is_not_a_merge_target():
    """A merge must never move what a payment paid: `payee_vendor_id` is named
    so the `VENDOR_FK_CHILDREN` drift guard does not demand it, and carries no FK."""
    payee = Payment.__table__.columns["payee_vendor_id"]
    assert not payee.foreign_keys
    assert Payment.__table__.columns.get("vendor_id") is None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _vendor(mk, org_id, *, name: str, bank: dict | None) -> uuid.UUID:
    async with mk() as s:
        v = Vendor(organization_id=org_id, name=name, status="active", bank_details=bank)
        s.add(v)
        await s.commit()
        return v.id


async def _invoice(
    mk, org_id, vendor_id, *, status: InvoiceStatus = InvoiceStatus.approved
) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=f"PAYEE-{uuid.uuid4().hex[:8]}",
                vendor_name="Payee Stamp Vendor",
                vendor_id=vendor_id,
                amount=Decimal("250.00"),
                currency="USD",
                status=status,
                approved_by="Original Approver",
            )
        )
        await s.commit()
    return inv_id


async def _payment(mk, invoice_id) -> Payment:
    async with mk() as s:
        return (
            (
                await s.execute(
                    select(Payment)
                    .where(Payment.invoice_id == invoice_id)
                    .order_by(Payment.created_at.desc())
                )
            )
            .scalars()
            .first()
        )


def _assert_stamped(payment: Payment, *, vendor_id, bank, org_id) -> None:
    assert payment.payee_recorded_at is not None
    assert payment.payee_vendor_id == vendor_id
    assert payment.payee_bank_fingerprint == bank_fingerprint(bank, organization_id=org_id)


async def _record_outside(realdb, invoice_id, *, role: str = "ap_manager") -> uuid.UUID:
    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.post(
            "/api/payments/record-outside",
            json={
                "invoice_id": str(invoice_id),
                "method": "check",
                "reference": f"CHK-{uuid.uuid4().hex[:6]}",
                "paid_on": utc_today().isoformat(),
            },
        )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _merge(realdb, canonical, *dups, role: str = "ap_manager") -> None:
    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(d) for d in dups]}
    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.post(_MERGE_URL, json=body)
    assert resp.status_code == 200, resp.text


async def _void(realdb, payment_id, *, role: str = "admin") -> None:
    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.post(f"/api/payments/{payment_id}/void", json={"reason": "sent twice"})
    assert resp.status_code == 200, resp.text


async def _fraud_flags(mk, invoice_id) -> list[APException]:
    async with mk() as s:
        return list(
            (
                await s.execute(
                    select(APException)
                    .where(
                        APException.invoice_id == invoice_id,
                        APException.exception_type == "fraud_flag",
                    )
                    .order_by(APException.created_at)
                )
            ).scalars()
        )


async def _stage_run(realdb, invoice_id, *, role: str = "admin", method: str = "ach"):
    async with realdb.client(key=TENANT, role=role) as c:
        return await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(invoice_id), "method": method}]},
        )


# ---------------------------------------------------------------------------
# Every step that releases money stamps the payee
# ---------------------------------------------------------------------------


async def test_a_rail_dispatch_stamps_the_payee_it_sends_to(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _vendor(mk, info.org_id, name="Rail Payee Co", bank=BANK_A)
    inv_id = await _invoice(mk, info.org_id, vendor_id)

    staged = await _stage_run(realdb, inv_id)
    assert staged.status_code == 201, staged.text
    # Booking a run releases nothing, so it stamps nothing.
    assert (await _payment(mk, inv_id)).payee_recorded_at is None
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        executed = await c.post(f"/api/payments/runs/{staged.json()['id']}/execute")
    assert executed.status_code == 200, executed.text

    payment = await _payment(mk, inv_id)
    assert payment.provider_payment_id, "the mock rail took the order"
    _assert_stamped(payment, vendor_id=vendor_id, bank=BANK_A, org_id=info.org_id)


async def test_a_card_dispatch_stamps_the_payee(realdb):
    """Stamped once the card leg's compliance gate has passed, immediately
    before the card is minted (or converged on)."""
    from tests.test_card_generate_gates import _enable_cards

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    await _enable_cards(realdb, org_id=info.org_id)
    vendor_id = await _vendor(mk, info.org_id, name="Card Payee Co", bank=BANK_A)
    inv_id = await _invoice(mk, info.org_id, vendor_id)

    staged = await _stage_run(realdb, inv_id, method="virtual_card")
    assert staged.status_code == 201, staged.text
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        executed = await c.post(f"/api/payments/runs/{staged.json()['id']}/execute")
    assert executed.status_code == 200, executed.text

    payment = await _payment(mk, inv_id)
    assert payment.method == "virtual_card"
    _assert_stamped(payment, vendor_id=vendor_id, bank=BANK_A, org_id=info.org_id)


async def test_a_nacha_export_stamps_each_entrys_payee(realdb):
    from tests.test_nacha import NACHA_CFG, RDFI, _business_day, _seed_run, _settings

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id, inv_id = await _seed_run(realdb, initiated_by=info.users["ap_manager"])
    assert (await _payment(mk, inv_id)).payee_recorded_at is None

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.get(
            f"/api/payments/runs/{run_id}/nacha",
            params={"effective_date": _business_day(2).isoformat()},
        )
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        vendor_id = (await s.get(Invoice, inv_id)).vendor_id
    _assert_stamped(
        await _payment(mk, inv_id),
        vendor_id=vendor_id,
        bank={"routing_number": RDFI, "account_number": "55501234"},
        org_id=info.org_id,
    )


async def test_a_nacha_export_refused_stamps_nothing(realdb):
    """A refusal rolls the request back: no stamp for a file never built."""
    from tests.test_nacha import NACHA_CFG, _seed_run, _settings

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id, inv_id = await _seed_run(
        realdb, initiated_by=info.users["ap_manager"], bank_details={"iban": "GB82WEST1234"}
    )
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.get(f"/api/payments/runs/{run_id}/nacha")
    assert resp.status_code == 409, resp.text
    assert (await _payment(mk, inv_id)).payee_recorded_at is None


async def test_a_record_outside_stamps_the_payee_on_file(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _vendor(mk, info.org_id, name="Outside Payee Co", bank=BANK_A)
    inv_id = await _invoice(mk, info.org_id, vendor_id)

    await _record_outside(realdb, inv_id)

    payment = await _payment(mk, inv_id)
    assert payment.status == "completed"
    _assert_stamped(payment, vendor_id=vendor_id, bank=BANK_A, org_id=info.org_id)


async def test_a_run_recorded_outside_stamps_each_payment(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _vendor(mk, info.org_id, name="Run Outside Payee Co", bank=BANK_A)
    inv_id = await _invoice(mk, info.org_id, vendor_id)
    staged = await _stage_run(realdb, inv_id, role="ap_manager")
    assert staged.status_code == 201, staged.text

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            f"/api/payments/runs/{staged.json()['id']}/record-outside",
            json={"reference": "BATCH-PAYEE-1", "paid_on": utc_today().isoformat()},
        )
    assert resp.status_code == 200, resp.text
    _assert_stamped(
        await _payment(mk, inv_id), vendor_id=vendor_id, bank=BANK_A, org_id=info.org_id
    )


async def _exported_run(realdb):
    """A record-only tenant's run, exported as a NACHA file (stamped)."""
    from tests.test_nacha import NACHA_CFG, _business_day, _seed_run, _settings

    info = realdb.info(TENANT)
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id, inv_id = await _seed_run(realdb, initiated_by=info.users["ap_manager"])
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.get(
            f"/api/payments/runs/{run_id}/nacha",
            params={"effective_date": _business_day(2).isoformat()},
        )
    assert resp.status_code == 200, resp.text
    return run_id, inv_id


async def _rebank_and_record_run(realdb, mk, run_id, inv_id, *, reference: str) -> None:
    async with mk() as s:
        vendor_id = (await s.get(Invoice, inv_id)).vendor_id
        (await s.get(Vendor, vendor_id)).bank_details = BANK_B
        await s.commit()
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            f"/api/payments/runs/{run_id}/record-outside",
            json={"reference": reference, "paid_on": utc_today().isoformat()},
        )
    assert resp.status_code == 200, resp.text


async def test_an_exported_entry_recorded_as_paid_keeps_the_payee_its_file_carried(realdb):
    """The customer's upload of the file is what paid it, so recording the run
    as paid later keeps the file's payee even when the details on file have
    moved since."""
    mk = realdb.sessionmaker(TENANT)
    run_id, inv_id = await _exported_run(realdb)
    sent = await _payment(mk, inv_id)
    assert sent.payee_recorded_at is not None

    await _rebank_and_record_run(realdb, mk, run_id, inv_id, reference="BATCH-PAYEE-2")

    payment = await _payment(mk, inv_id)
    assert payment.status == "completed"
    assert payment.payee_bank_fingerprint == sent.payee_bank_fingerprint
    assert payment.payee_recorded_at == sent.payee_recorded_at


async def test_a_rejected_file_s_stamp_is_replaced_when_the_run_is_recorded(realdb):
    """A file the bank rejected paid nobody: once it is voided the run is back
    in `draft`, and a record of the run outside FeohLedger stamps the payee on
    file now."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    run_id, inv_id = await _exported_run(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        voided = await c.post(
            f"/api/payments/runs/{run_id}/nacha/void", json={"reason": "the bank rejected it"}
        )
    assert voided.status_code == 200, voided.text

    await _rebank_and_record_run(realdb, mk, run_id, inv_id, reference="BATCH-PAYEE-3")

    payment = await _payment(mk, inv_id)
    assert payment.payee_bank_fingerprint == bank_fingerprint(BANK_B, organization_id=info.org_id)


async def test_a_compliance_hold_stamps_nothing(realdb):
    """A held payment instructed nothing, so it records no payee; its release
    re-dispatches and stamps then."""
    from tests.test_payment_compliance_hold import _seed_unscreenable_invoice

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_unscreenable_invoice(mk, info.org_id, number="PAYEE-HOLD", amount="90.00")
    staged = await _stage_run(realdb, inv_id)
    assert staged.status_code == 201, staged.text
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        executed = await c.post(f"/api/payments/runs/{staged.json()['id']}/execute")
    assert executed.status_code == 200, executed.text

    payment = await _payment(mk, inv_id)
    assert payment.status == "pending_compliance"
    assert payment.payee_recorded_at is None
    assert payment.payee_bank_fingerprint is None


async def test_an_erp_reported_payment_stamps_the_payee(realdb):
    from app.services.external_payment import record_invoice_paid_outside

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _vendor(mk, info.org_id, name="ERP Reported Payee Co", bank=BANK_A)
    inv_id = await _invoice(mk, info.org_id, vendor_id)

    async with mk() as s:
        invoice = await s.get(Invoice, inv_id, with_for_update=True)
        await record_invoice_paid_outside(
            s,
            org=SimpleNamespace(id=info.org_id, settings={}),
            invoice=invoice,
            method=None,
            reference="ERP-PAID-1",
            paid_on=utc_today(),
            actor_id=None,
            source="erp",
        )
        await s.commit()

    _assert_stamped(
        await _payment(mk, inv_id), vendor_id=vendor_id, bank=BANK_A, org_id=info.org_id
    )


# ---------------------------------------------------------------------------
# merge → void → re-pay
# ---------------------------------------------------------------------------


async def test_a_merged_invoice_voided_is_held_until_someone_else_clears_it(realdb):
    """The follow-up's case end to end. The invoice was paid to the duplicate's
    account; the merge moved it to the canonical; the void would re-pay the
    canonical's account. The flag blocks the run and the ERP push, the merge's
    actor cannot clear it, an independent person can, and the run then stages."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    duplicate = await _vendor(mk, info.org_id, name="Merged Payee Ltd", bank=BANK_A)
    canonical = await _vendor(mk, info.org_id, name="Merged Payee Limited", bank=BANK_B)
    inv_id = await _invoice(mk, info.org_id, duplicate)
    payment_id = await _record_outside(realdb, inv_id)

    await _merge(realdb, canonical, duplicate, role="ap_manager")
    async with mk() as s:
        assert (await s.get(Invoice, inv_id)).vendor_id == canonical
        # The merge folded the invoice; it never rewrote who was paid.
        assert (await s.get(Payment, payment_id)).payee_vendor_id == duplicate

    await _void(realdb, payment_id, role="admin")

    [flag] = await _fraud_flags(mk, inv_id)
    assert flag.status == "open"
    assert flag.description_code == "exception." + PAYEE_CHANGED_SINCE_PAID
    assert flag.raised_by_user_id == info.users["ap_manager"]
    # PII: neither account number reaches the queue's text.
    for bank in (BANK_A, BANK_B):
        assert bank["account_number"] not in (flag.description or "")

    refused = await _stage_run(realdb, inv_id)
    assert refused.status_code == 409, refused.text
    # And the ERP push, whose bill could be paid from the ERP (decisions §286).
    from app.services.erp_export_gate import erp_export_blocking_exception

    async with mk() as s:
        assert await erp_export_blocking_exception(s, inv_id) == "fraud_flag"

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        self_clear = await c.post(
            f"/api/exceptions/{flag.id}/resolve",
            json={"action": "resolve", "resolution": "Same supplier."},
        )
    assert self_clear.status_code == 403, self_clear.text
    assert self_clear.json()["detail"]["code"] == "segregation_raiser"

    async with realdb.client(key=TENANT, role="admin") as c:
        cleared = await c.post(
            f"/api/exceptions/{flag.id}/resolve",
            json={"action": "resolve", "resolution": "Confirmed the account with the supplier."},
        )
    assert cleared.status_code == 200, cleared.text

    staged = await _stage_run(realdb, inv_id)
    assert staged.status_code == 201, staged.text


async def test_the_same_vendor_and_bank_details_raise_nothing(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _vendor(mk, info.org_id, name="Unchanged Payee Co", bank=BANK_A)
    inv_id = await _invoice(mk, info.org_id, vendor_id)
    payment_id = await _record_outside(realdb, inv_id)

    await _void(realdb, payment_id)

    async with mk() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.approved
    assert await _fraud_flags(mk, inv_id) == []
    staged = await _stage_run(realdb, inv_id)
    assert staged.status_code == 201, staged.text


async def test_a_fingerprint_key_rotation_through_the_keyring_raises_nothing(realdb, monkeypatch):
    """The rotation docs/secrets-rotation.md prescribes: the old key moves to
    the keyring, and a void of a payment stamped under it is still compared
    under it. The JWT key changing at the same time is irrelevant."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    monkeypatch.setattr(settings, "payee_fingerprint_key", KEY_1)
    monkeypatch.setattr(settings, "payee_fingerprint_previous_keys", "")
    vendor_id = await _vendor(mk, info.org_id, name="Rotated Key Payee Co", bank=BANK_A)
    inv_id = await _invoice(mk, info.org_id, vendor_id)
    payment_id = await _record_outside(realdb, inv_id)
    stamped = (await _payment(mk, inv_id)).payee_bank_fingerprint
    assert stamped.split(":")[1] == fingerprint_key_id(KEY_1)

    monkeypatch.setattr(settings, "payee_fingerprint_key", KEY_2)
    monkeypatch.setattr(settings, "payee_fingerprint_previous_keys", KEY_1)
    await _void(realdb, payment_id)

    async with mk() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.approved
    assert await _fraud_flags(mk, inv_id) == []


async def test_a_stamp_under_a_key_no_longer_held_flags_the_void(realdb, monkeypatch):
    """The key replaced instead of rotated: the void cannot recompute the
    stamp, so it fails closed and flags, as for a bank change."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    monkeypatch.setattr(settings, "payee_fingerprint_key", KEY_1)
    monkeypatch.setattr(settings, "payee_fingerprint_previous_keys", "")
    vendor_id = await _vendor(mk, info.org_id, name="Dropped Key Payee Co", bank=BANK_A)
    inv_id = await _invoice(mk, info.org_id, vendor_id)
    payment_id = await _record_outside(realdb, inv_id)

    monkeypatch.setattr(settings, "payee_fingerprint_key", KEY_2)
    await _void(realdb, payment_id)

    [flag] = await _fraud_flags(mk, inv_id)
    assert flag.description_code == "exception." + PAYEE_CHANGED_SINCE_PAID
    # No bank change on record names a person; anyone outside the invoice's
    # implicated set may clear it.
    assert flag.raised_by_user_id is None
    staged = await _stage_run(realdb, inv_id)
    assert staged.status_code == 409, staged.text


async def test_with_no_fingerprint_key_the_payee_is_recorded_and_the_void_flags(
    realdb, monkeypatch
):
    """Empty key (local dev only; a deployed env refuses to boot): the payment
    still records its payee vendor and when, so a merge is still caught, but no
    fingerprint. A stamp says money moved, so a bank comparison that cannot run
    fails closed and flags; it is not treated as a pre-0128 unstamped payment."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    monkeypatch.setattr(settings, "payee_fingerprint_key", "")
    monkeypatch.setattr(settings, "payee_fingerprint_previous_keys", "")
    vendor_id = await _vendor(mk, info.org_id, name="Keyless Payee Co", bank=BANK_A)
    inv_id = await _invoice(mk, info.org_id, vendor_id)
    payment_id = await _record_outside(realdb, inv_id)

    payment = await _payment(mk, inv_id)
    assert payment.payee_recorded_at is not None
    assert payment.payee_vendor_id == vendor_id
    assert payment.payee_bank_fingerprint is None

    await _void(realdb, payment_id)

    [flag] = await _fraud_flags(mk, inv_id)
    assert flag.description_code == "exception." + PAYEE_CHANGED_SINCE_PAID
    assert flag.raised_by_user_id is None


async def test_other_bank_details_on_the_same_vendor_flag_once_raised_by_the_approver(realdb):
    """The fingerprint answers for every route a bank change takes, so the
    stamped payment raises one flag — not this one and the legacy
    `vendor_bank_changed` re-flag too — raised by the change's approver."""
    from app.models.vendor_change_request import VendorChangeRequest

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _vendor(mk, info.org_id, name="Rebanked Payee Co", bank=BANK_A)
    inv_id = await _invoice(mk, info.org_id, vendor_id)
    payment_id = await _record_outside(realdb, inv_id)

    async with mk() as s:
        (await s.get(Vendor, vendor_id)).bank_details = BANK_B
        s.add(
            VendorChangeRequest(
                vendor_id=vendor_id,
                organization_id=info.org_id,
                requested_by_user_id=uuid.uuid4(),
                change_type="bank_details",
                status="approved",
                proposed_value={"bank_details": {}},
                reviewed_by_user_id=info.users["cfo"],
                reviewed_at=datetime.now(UTC),
            )
        )
        await s.commit()

    await _void(realdb, payment_id)

    [flag] = await _fraud_flags(mk, inv_id)
    assert flag.description_code == "exception." + PAYEE_CHANGED_SINCE_PAID
    assert flag.raised_by_user_id == info.users["cfo"]


async def test_an_unrelated_open_fraud_flag_does_not_absorb_the_payee_flag(realdb):
    """Clearing a detector's fraud flag must not clear a payee change its
    clearer never saw, so the payee flag is raised beside it."""
    from app.services.exception_service import create_exception

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    duplicate = await _vendor(mk, info.org_id, name="Absorb Payee Ltd", bank=BANK_A)
    canonical = await _vendor(mk, info.org_id, name="Absorb Payee Limited", bank=BANK_B)
    inv_id = await _invoice(mk, info.org_id, duplicate)
    payment_id = await _record_outside(realdb, inv_id)
    await _merge(realdb, canonical, duplicate)
    async with mk() as s:
        await create_exception(
            s,
            exception_type="fraud_flag",
            severity="warning",
            description="detector output",
            organization_id=info.org_id,
            invoice=await s.get(Invoice, inv_id),
        )
        await s.commit()

    await _void(realdb, payment_id)

    codes = [f.description_code for f in await _fraud_flags(mk, inv_id)]
    assert codes == [None, "exception." + PAYEE_CHANGED_SINCE_PAID]


async def test_the_payee_flag_is_raised_once_while_open(realdb):
    """De-duplicated on the finding while it is open: raising it again (as a
    second void of a later payment would) adds nothing."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    duplicate = await _vendor(mk, info.org_id, name="Twice Payee Ltd", bank=BANK_A)
    canonical = await _vendor(mk, info.org_id, name="Twice Payee Limited", bank=BANK_B)
    inv_id = await _invoice(mk, info.org_id, duplicate)
    payment_id = await _record_outside(realdb, inv_id)
    await _merge(realdb, canonical, duplicate)
    await _void(realdb, payment_id)

    from app.api.vendors import flag_invoice_for_bank_change

    async with mk() as s:
        await flag_invoice_for_bank_change(
            s,
            invoice=await s.get(Invoice, inv_id),
            actor_id=info.users["ap_manager"],
            code=PAYEE_CHANGED_SINCE_PAID,
        )
        await s.commit()
    assert len(await _fraud_flags(mk, inv_id)) == 1

    # Another person's change is never folded into that row: they are not its
    # raiser, so they could clear it.
    async with mk() as s:
        await flag_invoice_for_bank_change(
            s,
            invoice=await s.get(Invoice, inv_id),
            actor_id=info.users["cfo"],
            code=PAYEE_CHANGED_SINCE_PAID,
        )
        await s.commit()
    assert sorted(str(f.raised_by_user_id) for f in await _fraud_flags(mk, inv_id)) == sorted(
        [str(info.users["ap_manager"]), str(info.users["cfo"])]
    )


async def _resolve(realdb, exc_id, *, role: str):
    async with realdb.client(key=TENANT, role=role) as c:
        return await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "Checked the account with the supplier."},
        )


async def test_the_merge_records_where_each_duplicate_went_and_who_sent_it(realdb):
    """First merge wins: a re-run by someone else never re-attributes it."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    duplicate = await _vendor(mk, info.org_id, name="Pointer Payee Ltd", bank=BANK_A)
    canonical = await _vendor(mk, info.org_id, name="Pointer Payee Limited", bank=BANK_B)

    await _merge(realdb, canonical, duplicate, role="ap_manager")
    await _merge(realdb, canonical, duplicate, role="admin")  # idempotent re-run

    async with mk() as s:
        retired = await s.get(Vendor, duplicate)
        assert retired.merged_into_vendor_id == canonical
        assert retired.merged_by_user_id == info.users["ap_manager"]
        assert retired.merged_at is not None
        assert (await s.get(Vendor, canonical)).merged_into_vendor_id is None


async def test_a_later_unrelated_merge_does_not_take_over_the_flag(realdb):
    """The merge that moved THIS invoice is the one named, not whoever merged
    something into the same canonical afterwards — or re-ran the merge — so its
    steward still cannot clear the flag."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    duplicate = await _vendor(mk, info.org_id, name="Moved Payee Ltd", bank=BANK_A)
    other = await _vendor(mk, info.org_id, name="Moved Payee Co", bank=None)
    canonical = await _vendor(mk, info.org_id, name="Moved Payee Limited", bank=BANK_B)
    inv_id = await _invoice(mk, info.org_id, duplicate)
    payment_id = await _record_outside(realdb, inv_id)

    await _merge(realdb, canonical, duplicate, role="ap_manager")
    await _merge(realdb, canonical, other, role="admin")
    await _merge(realdb, canonical, duplicate, role="admin")
    await _void(realdb, payment_id, role="cfo")

    [flag] = await _fraud_flags(mk, inv_id)
    assert flag.raised_by_user_id == info.users["ap_manager"]
    refused = await _resolve(realdb, flag.id, role="ap_manager")
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "segregation_raiser"


async def test_every_steward_on_a_merge_chain_is_refused_their_own_flag(realdb):
    """D → B by one steward, B → C by another: both moved the payee, so each
    gets a flag the other (or a third person) must clear."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    first = await _vendor(mk, info.org_id, name="Chain Payee D", bank=BANK_A)
    middle = await _vendor(mk, info.org_id, name="Chain Payee B", bank=None)
    last = await _vendor(mk, info.org_id, name="Chain Payee C", bank=BANK_B)
    inv_id = await _invoice(mk, info.org_id, first)
    payment_id = await _record_outside(realdb, inv_id)

    await _merge(realdb, middle, first, role="ap_manager")
    await _merge(realdb, last, middle, role="admin")
    await _void(realdb, payment_id, role="cfo")

    flags = {f.raised_by_user_id: f for f in await _fraud_flags(mk, inv_id)}
    assert set(flags) == {info.users["ap_manager"], info.users["admin"]}
    assert {f.description_code for f in flags.values()} == {"exception." + PAYEE_CHANGED_SINCE_PAID}
    for role in ("ap_manager", "admin"):
        own = await _resolve(realdb, flags[info.users[role]].id, role=role)
        assert own.status_code == 403, own.text
    for role, other in (("ap_manager", "admin"), ("admin", "ap_manager")):
        cleared = await _resolve(realdb, flags[info.users[other]].id, role=role)
        assert cleared.status_code == 200, cleared.text
    staged = await _stage_run(realdb, inv_id)
    assert staged.status_code == 201, staged.text


async def test_two_bank_changes_since_the_payment_flag_once_per_approver(realdb):
    from app.models.vendor_change_request import VendorChangeRequest

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _vendor(mk, info.org_id, name="Twice Rebanked Co", bank=BANK_A)
    inv_id = await _invoice(mk, info.org_id, vendor_id)
    payment_id = await _record_outside(realdb, inv_id)
    async with mk() as s:
        (await s.get(Vendor, vendor_id)).bank_details = BANK_B
        for role in ("cfo", "admin"):
            s.add(
                VendorChangeRequest(
                    vendor_id=vendor_id,
                    organization_id=info.org_id,
                    requested_by_user_id=uuid.uuid4(),
                    change_type="bank_details",
                    status="approved",
                    proposed_value={"bank_details": {}},
                    reviewed_by_user_id=info.users[role],
                    reviewed_at=datetime.now(UTC),
                )
            )
        await s.commit()

    await _void(realdb, payment_id, role="admin")

    assert {f.raised_by_user_id for f in await _fraud_flags(mk, inv_id)} == {
        info.users["cfo"],
        info.users["admin"],
    }


async def test_a_bank_change_racing_the_void_is_flagged(realdb):
    """The approval updates the vendor and flags its payable invoices, which the
    voided one is not until the void commits. The void reads the vendor FOR
    SHARE, so it waits for the approval and then sees the new details."""
    import asyncio

    from sqlalchemy import text

    from app.models.vendor_change_request import VendorChangeRequest

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _vendor(mk, info.org_id, name="Racing Payee Co", bank=BANK_A)
    inv_id = await _invoice(mk, info.org_id, vendor_id)
    payment_id = await _record_outside(realdb, inv_id)

    async with mk() as approval:
        vendor = await approval.get(Vendor, vendor_id, with_for_update=True)
        vendor.bank_details = BANK_B
        approval.add(
            VendorChangeRequest(
                vendor_id=vendor_id,
                organization_id=info.org_id,
                requested_by_user_id=uuid.uuid4(),
                change_type="bank_details",
                status="approved",
                proposed_value={"bank_details": {}},
                reviewed_by_user_id=info.users["cfo"],
                reviewed_at=datetime.now(UTC),
            )
        )
        await approval.flush()

        void = asyncio.create_task(_void(realdb, payment_id, role="admin"))
        # The real signal: a backend on this tenant waiting on a row lock.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        while True:
            # A fresh session each time: pg_stat_activity is snapshotted once
            # per transaction.
            async with mk() as probe:
                waiting = (
                    await probe.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                        )
                    )
                ).scalar()
            if waiting or void.done():
                break
            assert loop.time() < deadline, "the void never reached the vendor lock"
            await asyncio.sleep(0.05)
        assert not void.done(), "the void read the vendor without waiting for the approval"
        await approval.commit()
    await void

    [flag] = await _fraud_flags(mk, inv_id)
    assert flag.description_code == "exception." + PAYEE_CHANGED_SINCE_PAID
    assert flag.raised_by_user_id == info.users["cfo"]


# ---------------------------------------------------------------------------
# A payment from before the stamp
# ---------------------------------------------------------------------------


async def _unstamped_paid(mk, org_id, vendor_id) -> tuple[uuid.UUID, uuid.UUID]:
    """A `paid` invoice and the completed payment that paid it before 0128."""
    inv_id = await _invoice(mk, org_id, vendor_id, status=InvoiceStatus.paid)
    pay_id = uuid.uuid4()
    async with mk() as s:
        run = PaymentRun(
            organization_id=org_id,
            status="completed",
            total_amount=Decimal("250.00"),
            requires_cfo_approval=False,
        )
        s.add(run)
        await s.flush()
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                payment_run_id=run.id,
                amount=Decimal("250.00"),
                method="ach",
                status="completed",
                correlation_id=uuid.uuid4(),
                created_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
        await s.commit()
    return inv_id, pay_id


async def test_an_unstamped_payment_merged_since_flags_that_its_payee_is_unrecorded(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    duplicate = await _vendor(mk, info.org_id, name="Legacy Payee Ltd", bank=BANK_A)
    canonical = await _vendor(mk, info.org_id, name="Legacy Payee Limited", bank=BANK_B)
    inv_id, pay_id = await _unstamped_paid(mk, info.org_id, duplicate)

    await _merge(realdb, canonical, duplicate, role="ap_manager")
    await _void(realdb, pay_id)

    [flag] = await _fraud_flags(mk, inv_id)
    assert flag.description_code == "exception." + PAYEE_UNRECORDED_AFTER_MERGE
    assert flag.raised_by_user_id == info.users["ap_manager"]
    async with mk() as s:
        # Nothing was backfilled onto the old payment.
        assert (await s.get(Payment, pay_id)).payee_recorded_at is None


async def test_an_unstamped_payment_with_no_merge_since_is_not_compared(realdb):
    """No stamp, no merge on record: nothing honest to compare (§141). The
    bank-change re-flag still answers for its own case
    (`test_erp_export_blocking_exceptions.py`)."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _vendor(mk, info.org_id, name="Legacy Quiet Co", bank=BANK_A)
    inv_id, pay_id = await _unstamped_paid(mk, info.org_id, vendor_id)

    await _void(realdb, pay_id)

    assert await _fraud_flags(mk, inv_id) == []


async def test_a_merge_before_the_payment_was_booked_is_not_a_change(realdb):
    """A merge that predates the payment: the payment already paid the
    canonical, so an unstamped one is not flagged for it."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    duplicate = await _vendor(mk, info.org_id, name="Early Merge Ltd", bank=BANK_A)
    canonical = await _vendor(mk, info.org_id, name="Early Merge Limited", bank=BANK_B)
    await _merge(realdb, canonical, duplicate)
    inv_id = await _invoice(mk, info.org_id, canonical, status=InvoiceStatus.paid)
    pay_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                amount=Decimal("250.00"),
                method="ach",
                status="completed",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()

    await _void(realdb, pay_id)

    assert await _fraud_flags(mk, inv_id) == []
