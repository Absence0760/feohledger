"""NACHA ACH file export — the bank-file path of the no-rail pilot (issue #517).

The pure builder (`services/nacha.py`) is pinned record by record against the
NACHA layout; the endpoint (`GET /api/payments/runs/{id}/nacha`) over a real DB:
record-only tenants only, the run's controls, the per-payment refusals that
name the invoice and never the bank data, and the audit row that carries a
digest rather than the file.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.services.nacha import (
    MAX_ENTRY_AMOUNT,
    NachaEntry,
    NachaError,
    NachaOffset,
    NachaOriginator,
    build_nacha_file,
    carry_offset_account,
    entry_from_bank_details,
    normalize_account_number,
    originator_from_settings,
    originator_problems,
)
from app.services.org_settings_view import settings_for_response
from app.utils.dates import utc_today

ODFI = "021000021"  # a valid ABA (JPMorgan Chase NY)
RDFI = "011000015"  # a valid ABA (FRB Boston)

ORIGINATOR = NachaOriginator(
    company_name="Acme Holdings", company_id="1123456789", odfi_routing=ODFI, bank_name="Chase"
)


def _entry(amount: str, *, account_type="checking", routing=RDFI) -> NachaEntry:
    return NachaEntry(
        routing_number=routing,
        account_number="000123456789",
        account_type=account_type,
        amount=Decimal(amount),
        individual_id="INV-1001",
        name="Global Supplies Ltd",
    )


def _build(entries, **kw) -> list[str]:
    text = build_nacha_file(
        kw.get("originator", ORIGINATOR),
        entries,
        sec_code=kw.get("sec_code", "CCD"),
        effective_date=date(2026, 10, 7),
        created_at=datetime(2026, 10, 6, 14, 30, tzinfo=UTC),
    )
    assert text.endswith("\r\n")
    return text[:-2].split("\r\n")


# ── The builder ─────────────────────────────────────────────────────────


def test_every_record_is_94_chars_and_the_file_fills_whole_blocks():
    lines = _build([_entry("100.00"), _entry("250.55")])
    assert all(len(line) == 94 for line in lines)
    # 1 header + 1 batch header + 2 entries + batch control + file control = 6,
    # padded with all-9 filler to a block of 10.
    assert len(lines) == 10
    assert [line[0] for line in lines[:6]] == ["1", "5", "6", "6", "8", "9"]
    assert lines[6:] == ["9" * 94] * 4


def test_file_header_fields():
    header = _build([_entry("1.00")])[0]
    assert header[1:3] == "01"
    assert header[3:13] == " " + ODFI
    assert header[13:23] == "1123456789"
    assert header[23:29] == "261006"
    assert header[29:33] == "1430"
    assert header[33:40] == "A094101"
    assert header[40:63].rstrip() == "CHASE"
    assert header[63:86].rstrip() == "ACME HOLDINGS"


def test_batch_header_fields():
    batch = _build([_entry("1.00")], sec_code="PPD")[1]
    assert batch[1:4] == "220"  # credits only
    assert batch[4:20].rstrip() == "ACME HOLDINGS"
    assert batch[40:50] == "1123456789"
    assert batch[50:53] == "PPD"
    assert batch[69:75] == "261007"  # effective entry date
    assert batch[78] == "1"
    assert batch[79:87] == ODFI[:8]
    assert batch[87:94] == "0000001"


def test_entry_detail_fields():
    entry = _build([_entry("1234.56")])[2]
    assert entry[1:3] == "22"  # checking credit
    assert entry[3:11] == RDFI[:8]
    assert entry[11] == RDFI[8]
    assert entry[12:29] == "000123456789".ljust(17)
    assert entry[29:39] == "0000123456"
    assert entry[39:54].rstrip() == "INV-1001"
    assert entry[54:76].rstrip() == "GLOBAL SUPPLIES LTD"
    assert entry[78] == "0"
    assert entry[79:94] == ODFI[:8] + "0000001"


def test_savings_accounts_get_the_savings_credit_code():
    assert _build([_entry("1.00", account_type="savings")])[2][1:3] == "32"


def test_controls_carry_counts_hash_and_totals():
    lines = _build([_entry("100.00"), _entry("0.45", routing=ODFI)])
    batch_control, file_control = lines[4], lines[5]
    expected_hash = str(int(RDFI[:8]) + int(ODFI[:8])).rjust(10, "0")
    assert batch_control[4:10] == "000002"
    assert batch_control[10:20] == expected_hash
    assert batch_control[20:32] == "0" * 12  # no debits
    assert batch_control[32:44] == "000000010045"
    assert file_control[1:7] == "000001"  # batches
    assert file_control[7:13] == "000001"  # blocks
    assert file_control[13:21] == "00000002"
    assert file_control[21:31] == expected_hash
    assert file_control[43:55] == "000000010045"


def test_eleven_records_take_two_blocks():
    lines = _build([_entry("1.00")] * 7)  # 4 + 7 = 11 records
    assert len(lines) == 20
    assert lines[10][7:13] == "000002"


def test_names_are_folded_to_nacha_text():
    entry = NachaEntry(
        routing_number=RDFI,
        account_number="1",
        account_type="checking",
        amount=Decimal("1"),
        individual_id="F/2026#7",
        name="Société Générale ✓",
    )
    line = _build([entry])[2]
    assert line[54:76].rstrip() == "SOCIETE GENERALE"
    assert line[39:54].rstrip() == "F/2026 7"


@pytest.mark.parametrize("amount", ["0", "-1.00", "100000000.00", "1.005"])
def test_unrepresentable_amounts_refuse(amount):
    with pytest.raises(NachaError):
        _build([_entry(amount)])


def test_unknown_sec_code_refuses():
    with pytest.raises(NachaError):
        _build([_entry("1.00")], sec_code="WEB")


def test_originator_problems_names_fields_only():
    assert originator_problems(None) == ["company_name", "company_id", "odfi_routing"]
    assert (
        originator_problems(
            {"company_name": "Acme", "company_id": "1123456789", "odfi_routing": ODFI}
        )
        == []
    )
    assert originator_problems(
        {
            "company_name": "A name far too long for it",
            "company_id": "12345",
            "odfi_routing": "021000022",  # bad checksum
            "bank_name": "x" * 24,
        }
    ) == ["company_name", "company_id", "odfi_routing", "bank_name"]


def test_bank_details_to_entry():
    assert normalize_account_number(" 0001-2345 6789 ") == "000123456789"
    assert normalize_account_number("x" * 18) is None
    assert normalize_account_number(None) is None
    ok = entry_from_bank_details(
        {"routing_number": RDFI, "account_number": "12345", "account_type": "Savings"},
        amount=Decimal("5"),
        individual_id="I",
        name="N",
    )
    assert ok is not None and ok.account_type == "savings"
    # Only the ACH routing field is read; an IBAN payee has none.
    assert (
        entry_from_bank_details(
            {"wire_routing_number": RDFI, "account_number": "12345"},
            amount=Decimal("5"),
            individual_id="I",
            name="N",
        )
        is None
    )
    assert (
        entry_from_bank_details(
            {"iban": "GB82WEST12345698765432"}, amount=Decimal("5"), individual_id="I", name="N"
        )
        is None
    )


# ── Balanced files: the offsetting debit (decisions §432) ───────────────

OFFSET_ROUTING = "011000015"  # FRB Boston — a different bank from the ODFI on purpose
OFFSET_ACCOUNT = "9876543210"
BALANCED = replace(
    ORIGINATOR,
    offset=NachaOffset(
        routing_number=OFFSET_ROUTING, account_number=OFFSET_ACCOUNT, account_type="checking"
    ),
)


def test_the_unbalanced_file_is_byte_identical():
    """No offset keys → exactly the credits-only file this builder always wrote.

    The digest was taken from the builder before the offset existed; a change
    to it is a change to every file a customer's bank already accepts.
    """
    text = build_nacha_file(
        ORIGINATOR,
        [
            _entry("100.00"),
            _entry("0.45", routing=ODFI),
            _entry("7.10", account_type="savings"),
        ],
        sec_code="CCD",
        effective_date=date(2026, 10, 7),
        created_at=datetime(2026, 10, 6, 14, 30, tzinfo=UTC),
    )
    assert (
        hashlib.sha256(text.encode("ascii")).hexdigest()
        == "a1c587b5ba5e6443fa5f085fb5db87d9adab8650d86c55fc87eee691ffcd2bdc"
    )


def test_a_balanced_file_debits_the_offset_account_for_the_batch_total():
    lines = _build([_entry("100.00"), _entry("0.45", routing=ODFI)], originator=BALANCED)
    assert all(len(line) == 94 for line in lines)
    # header, batch header, 2 credits + 1 offset debit, batch control, file
    # control = 7, padded with all-9 filler to a block of 10.
    assert [line[0] for line in lines[:7]] == ["1", "5", "6", "6", "6", "8", "9"]
    assert lines[7:] == ["9" * 94] * 3

    batch_header, offset, batch_control, file_control = lines[1], lines[4], lines[5], lines[6]
    assert batch_header[1:4] == "200"  # mixed debits and credits
    assert offset[1:3] == "27"  # checking debit
    assert offset[3:11] == OFFSET_ROUTING[:8]
    assert offset[11] == OFFSET_ROUTING[8]
    assert offset[12:29] == OFFSET_ACCOUNT.ljust(17)
    assert offset[29:39] == "0000010045"  # the batch total
    assert offset[39:54].rstrip() == "OFFSET"
    assert offset[54:76].rstrip() == "ACME HOLDINGS"
    assert offset[79:94] == ODFI[:8] + "0000003"  # the next trace number

    expected_hash = str(int(RDFI[:8]) + int(ODFI[:8]) + int(OFFSET_ROUTING[:8])).rjust(10, "0")
    assert batch_control[1:4] == "200"
    assert batch_control[4:10] == "000003"  # entry/addenda count includes the offset
    assert batch_control[10:20] == expected_hash
    assert batch_control[20:32] == "000000010045"  # total debits
    assert batch_control[32:44] == "000000010045"  # total credits
    assert file_control[1:7] == "000001"  # batches
    assert file_control[7:13] == "000001"  # blocks
    assert file_control[13:21] == "00000003"
    assert file_control[21:31] == expected_hash
    assert file_control[31:43] == "000000010045"
    assert file_control[43:55] == "000000010045"


def test_a_savings_offset_account_gets_the_savings_debit_code():
    savings = replace(BALANCED, offset=replace(BALANCED.offset, account_type="savings"))
    assert _build([_entry("1.00")], originator=savings)[3][1:3] == "37"


def test_the_offset_debit_pads_to_a_new_block_when_it_tips_the_count():
    # 4 + 6 credits = 10 records unbalanced (one block); the offset makes 11.
    assert len(_build([_entry("1.00")] * 6)) == 10
    lines = _build([_entry("1.00")] * 6, originator=BALANCED)
    assert len(lines) == 20
    assert lines[10][0] == "9" and lines[10][7:13] == "000002"
    assert lines[11:] == ["9" * 94] * 9


def test_a_total_beyond_one_entry_is_offset_by_several_debits():
    lines = _build([_entry(str(MAX_ENTRY_AMOUNT)), _entry("10.00")], originator=BALANCED)
    debits = [line for line in lines if line[0] == "6" and line[1:3] == "27"]
    assert [d[29:39] for d in debits] == ["9999999999", "0000001000"]
    assert [d[79:94] for d in debits] == [ODFI[:8] + "0000003", ODFI[:8] + "0000004"]
    batch_control = next(line for line in lines if line[0] == "8")
    assert batch_control[4:10] == "000004"
    assert batch_control[20:32] == batch_control[32:44] == "010000000999"


OFFSET_CFG = {
    "company_name": "Acme",
    "company_id": "1123456789",
    "odfi_routing": ODFI,
    "offset_routing": OFFSET_ROUTING,
    "offset_account": "98-7654 3210",
    "offset_account_type": "checking",
}


def test_offset_settings_are_all_or_none_and_name_fields_only():
    assert originator_problems(OFFSET_CFG) == []
    assert originator_problems({**OFFSET_CFG, "offset_account_type": "savings"}) == []
    # Any one offset key asks for a balanced file; the others are then required.
    partial = {k: v for k, v in OFFSET_CFG.items() if k != "offset_account_type"}
    assert originator_problems(partial) == ["offset_account_type"]
    assert originator_problems(
        {
            **OFFSET_CFG,
            "offset_routing": "011000016",
            "offset_account": "x" * 18,
            "offset_account_type": "money_market",
        }
    ) == ["offset_routing", "offset_account", "offset_account_type"]
    # Blank offset keys are "no offset", not a half-filled one.
    blank = {**OFFSET_CFG, "offset_routing": "", "offset_account": None, "offset_account_type": ""}
    assert originator_problems(blank) == []
    assert originator_from_settings(blank).offset is None


def test_offset_settings_build_the_offset_and_never_print_the_account():
    originator = originator_from_settings(OFFSET_CFG)
    assert originator.offset == NachaOffset(
        routing_number=OFFSET_ROUTING, account_number="9876543210", account_type="checking"
    )
    assert "9876543210" not in repr(originator)
    with pytest.raises(NachaError) as refused:
        originator_from_settings({**OFFSET_CFG, "offset_account_type": "x"})
    assert "9876543210" not in str(refused.value)


def test_a_resave_keeps_the_stored_offset_account():
    stored = {**OFFSET_CFG, "offset_account": "9876543210"}
    # Sent blank, or as the mask the settings page was shown: keep the stored number.
    for sent in ("", None, "****3210"):
        incoming = {**OFFSET_CFG, "offset_account": sent}
        assert carry_offset_account(incoming, stored)["offset_account"] == "9876543210"
    no_key = {k: v for k, v in OFFSET_CFG.items() if k != "offset_account"}
    assert carry_offset_account(no_key, stored)["offset_account"] == "9876543210"
    # A new number replaces it; a mask of some other number is not a keep.
    assert (
        carry_offset_account({**OFFSET_CFG, "offset_account": "111"}, stored)["offset_account"]
        == "111"
    )
    assert (
        carry_offset_account({**OFFSET_CFG, "offset_account": "****9999"}, stored)["offset_account"]
        == "****9999"
    )
    # Clearing the routing and type clears the offset: nothing is carried.
    cleared = {**OFFSET_CFG, "offset_routing": "", "offset_account": "", "offset_account_type": ""}
    assert carry_offset_account(cleared, stored)["offset_account"] == ""
    assert carry_offset_account(None, stored) is None


def test_the_settings_response_masks_the_offset_account():
    stored = {
        "payments": {"mode": "record_only", "nacha": {**OFFSET_CFG, "offset_account": "9876543210"}}
    }
    admin = settings_for_response(stored, is_admin=True)
    assert admin["payments"]["nacha"]["offset_account"] == "****3210"
    assert admin["payments"]["nacha"]["offset_routing"] == OFFSET_ROUTING
    assert stored["payments"]["nacha"]["offset_account"] == "9876543210"  # not mutated
    assert "nacha" not in settings_for_response(stored, is_admin=False).get("payments", {})
    plain = {"payments": {"nacha": {"company_name": "Acme"}}}
    assert settings_for_response(plain, is_admin=True) == plain


# ── The endpoint, over a real DB ────────────────────────────────────────


def _business_day(ahead: int) -> date:
    day = utc_today() + timedelta(days=ahead)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


NACHA_CFG = {"company_name": "PyTest Co", "company_id": "1987654321", "odfi_routing": ODFI}


async def _settings(realdb, org_id, payments: dict) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, org_id)
        settings = dict(org.settings or {})
        settings["payments"] = payments
        org.settings = settings
        await s.commit()


async def _seed_run(
    realdb,
    *,
    initiated_by,
    method="ach",
    currency="USD",
    bank_details: dict | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id, inv_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                organization_id=info.org_id,
                name="NACHA Payee Inc",
                status="active",
                bank_details=(
                    {"routing_number": RDFI, "account_number": "55501234"}
                    if bank_details is None
                    else bank_details
                ),
            )
        )
        s.add(
            Invoice(
                id=inv_id,
                organization_id=info.org_id,
                invoice_number=f"NACHA-{uuid.uuid4().hex[:6]}",
                vendor_name="NACHA Payee Inc",
                vendor_id=vendor_id,
                amount=Decimal("1500.25"),
                currency=currency,
                status=InvoiceStatus.approved,
            )
        )
        s.add(
            PaymentRun(
                id=run_id,
                organization_id=info.org_id,
                status="draft",
                total_amount=Decimal("1500.25"),
                initiated_by=initiated_by,
            )
        )
        await s.flush()
        s.add(
            Payment(
                invoice_id=inv_id,
                payment_run_id=run_id,
                amount=Decimal("1500.25"),
                method=method,
                status="pending",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()
    return run_id, inv_id


@pytest.mark.asyncio
async def test_exports_a_file_and_audits_a_digest(realdb):
    info = realdb.info("a")
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id, _ = await _seed_run(realdb, initiated_by=info.users["ap_manager"])
    effective = _business_day(2).isoformat()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(
            f"/api/payments/runs/{run_id}/nacha",
            params={"effective_date": effective, "sec_code": "CCD"},
        )
    assert resp.status_code == 200, resp.text
    assert "attachment" in resp.headers["content-disposition"]
    lines = resp.text.rstrip("\r\n").split("\r\n")
    assert len(lines) % 10 == 0
    entry = next(line for line in lines if line.startswith("6"))
    assert entry[29:39] == "0000150025"
    assert entry[12:29].rstrip() == "55501234"

    async with realdb.sessionmaker("a")() as s:
        [audit] = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == run_id,
                        AuditLog.action == "payment_run.nacha_exported",
                    )
                )
            )
            .scalars()
            .all()
        )
        run = await s.get(PaymentRun, run_id)
    assert audit.details["payment_count"] == 1
    assert audit.details["total_amount"] == "1500.25"
    assert len(audit.details["file_sha256"]) == 64
    assert "55501234" not in str(audit.details)  # banking data stays out
    assert run.status == "exported"  # the export claims the run (no Execute, no cancel)


@pytest.mark.asyncio
async def test_a_processor_tenant_cannot_export(realdb):
    """The file plus an Execute would pay every supplier twice."""
    info = realdb.info("a")
    await _settings(realdb, info.org_id, {"provider": "mock", "nacha": NACHA_CFG})
    run_id, _ = await _seed_run(realdb, initiated_by=info.users["ap_manager"])
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/payments/runs/{run_id}/nacha")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "nacha_requires_record_only"


@pytest.mark.asyncio
async def test_unconfigured_originator_names_the_missing_fields(realdb):
    info = realdb.info("a")
    await _settings(realdb, info.org_id, {"mode": "record_only"})
    run_id, _ = await _seed_run(realdb, initiated_by=info.users["ap_manager"])
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/payments/runs/{run_id}/nacha")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "nacha_not_configured"
    assert detail["params"]["missing"] == ["company_name", "company_id", "odfi_routing"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "code"),
    [
        ({"method": "check"}, "nacha_payment_not_ach"),
        ({"currency": "EUR"}, "nacha_currency_not_usd"),
        ({"bank_details": {"iban": "DE89370400440532013000"}}, "nacha_vendor_bank_missing"),
        (
            {"bank_details": {"routing_number": "021000022", "account_number": "1"}},
            "nacha_vendor_bank_missing",
        ),
    ],
)
async def test_a_payment_that_cannot_go_in_the_file_refuses_it(realdb, seed, code):
    info = realdb.info("a")
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id, inv_id = await _seed_run(realdb, initiated_by=info.users["ap_manager"], **seed)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/payments/runs/{run_id}/nacha")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code
    assert detail["params"]["invoice_number"].startswith("NACHA-")
    # No routing / account number in the refusal.
    assert "021000022" not in resp.text and "DE89" not in resp.text


@pytest.mark.asyncio
async def test_the_runs_creator_may_not_export_it(realdb):
    info = realdb.info("a")
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id, _ = await _seed_run(realdb, initiated_by=info.users["admin"])
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/payments/runs/{run_id}/nacha")
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_bad_nacha_settings_are_refused_at_save(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"payments": {"nacha": {**NACHA_CFG, "odfi_routing": "123"}}}},
        )
    assert resp.status_code == 422, resp.text
    assert "odfi_routing" in resp.json()["detail"]


async def _run_status(realdb, run_id) -> str:
    async with realdb.sessionmaker("a")() as s:
        return (await s.get(PaymentRun, run_id)).status


@pytest.mark.asyncio
async def test_an_export_claims_the_run_until_recorded_or_voided(realdb):
    """The double-pay guards: once the file is out, a second file, a cancel and
    re-stage, or an Execute (after a switch to processor mode) are all refused."""
    info = realdb.info("a")
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id, _ = await _seed_run(realdb, initiated_by=info.users["ap_manager"])
    url = f"/api/payments/runs/{run_id}/nacha"

    async with realdb.client(key="a", role="admin") as c:
        first = await c.get(url)
        assert first.status_code == 200, first.text
        assert await _run_status(realdb, run_id) == "exported"
        assert first.text.splitlines()[0][33] == "A"

        again = await c.get(url)
        assert again.status_code == 409, again.text
        assert again.json()["detail"]["code"] == "nacha_already_exported"

        cancel = await c.post(f"/api/payments/runs/{run_id}/cancel")
        assert cancel.status_code == 409, cancel.text

        regenerated = await c.get(url, params={"regenerate": "true"})
        assert regenerated.status_code == 200, regenerated.text
        assert regenerated.text.splitlines()[0][33] == "B"  # bank can tell them apart

    await _settings(realdb, info.org_id, {"provider": "mock", "nacha": NACHA_CFG})
    async with realdb.client(key="a", role="admin") as c:
        execute = await c.post(f"/api/payments/runs/{run_id}/execute")
    assert execute.status_code == 409, execute.text
    assert await _run_status(realdb, run_id) == "exported"

    # The bank rejected it: the audited exit back to draft — on a tenant that
    # records payments, as when the file was made (decisions §362).
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    async with realdb.client(key="a", role="admin") as c:
        blank = await c.post(f"/api/payments/runs/{run_id}/nacha/void", json={"reason": " "})
        voided = await c.post(
            f"/api/payments/runs/{run_id}/nacha/void",
            json={"reason": "Bank rejected the file: ODFI mismatch"},
        )
    assert blank.status_code == 422, blank.text
    assert voided.status_code == 200, voided.text
    assert await _run_status(realdb, run_id) == "draft"
    async with realdb.sessionmaker("a")() as s:
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_id == run_id)))
            .scalars()
            .all()
        )
    assert actions.count("payment_run.nacha_exported") == 2
    assert "payment_run.nacha_export_voided" in actions


@pytest.mark.asyncio
async def test_a_file_exported_before_a_switch_to_processor_mode_keeps_its_claim(realdb):
    """Decisions §362. A tenant exports a file, its bank takes it, and the
    tenant then switches `payments.mode` to `processor`. "Bank rejected the
    file" used to release the run to draft there, and one Execute — or a
    cancel and a restage — paid every supplier in the file a second time. The
    void is refused while the tenant sends payments, so the `exported` claim
    that execute, cancel and re-staging refuse stays in place."""
    info = realdb.info("a")
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id, inv_id = await _seed_run(realdb, initiated_by=info.users["ap_manager"])
    async with realdb.client(key="a", role="admin") as c:
        assert (await c.get(f"/api/payments/runs/{run_id}/nacha")).status_code == 200

    await _settings(realdb, info.org_id, {"provider": "mock", "nacha": NACHA_CFG})
    async with realdb.client(key="a", role="admin") as c:
        detail = (await c.get(f"/api/payments/runs/{run_id}")).json()
        void = await c.post(
            f"/api/payments/runs/{run_id}/nacha/void", json={"reason": "the bank rejected it"}
        )
        execute = await c.post(f"/api/payments/runs/{run_id}/execute")
        cancel = await c.post(f"/api/payments/runs/{run_id}/cancel")
        restage = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]},
        )
    assert void.status_code == 409, void.text
    assert void.json()["detail"]["code"] == "nacha_void_requires_record_only"
    assert detail["actions"]["nacha_void"]["code"] == "nacha_void_requires_record_only"
    assert execute.status_code == 409, execute.text
    assert cancel.status_code == 409, cancel.text
    assert restage.status_code == 409, restage.text  # the invoice still has its live payment
    assert await _run_status(realdb, run_id) == "exported"
    async with realdb.sessionmaker("a")() as s:
        payments = (
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_id == run_id)))
            .scalars()
            .all()
        )
    assert [p.status for p in payments] == ["pending"]  # nothing executed, nothing restaged
    assert "payment_run.nacha_export_voided" not in actions

    # The exit for a file the bank really rejected: back to record-only first.
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    async with realdb.client(key="a", role="admin") as c:
        voided = await c.post(
            f"/api/payments/runs/{run_id}/nacha/void", json={"reason": "the bank rejected it"}
        )
    assert voided.status_code == 200, voided.text
    assert await _run_status(realdb, run_id) == "draft"


@pytest.mark.asyncio
async def test_a_fraud_flag_raised_after_staging_refuses_the_file(realdb):
    """A bank-detail swap approved while the run sat in draft raises a
    `fraud_flag`; the file would carry the NEW account. `/execute` refuses this
    via `dispatch_preflight`, and so must the export."""
    from app.models.exception import Exception as APException

    info = realdb.info("a")
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id, inv_id = await _seed_run(realdb, initiated_by=info.users["ap_manager"])
    async with realdb.sessionmaker("a")() as s:
        s.add(
            APException(
                id=uuid.uuid4(),
                organization_id=info.org_id,
                invoice_id=inv_id,
                exception_type="fraud_flag",
                severity="error",
                description="Vendor bank details changed; verify before payment",
                status="open",
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/payments/runs/{run_id}/nacha")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "nacha_payment_not_payable"
    assert detail["params"]["reason"] == "invoice_blocked:fraud_flag"
    assert await _run_status(realdb, run_id) == "draft"


@pytest.mark.asyncio
async def test_a_credit_applied_after_staging_refuses_the_file(realdb):
    """The staged $1,500.25 is no longer what is owed — the file would overpay."""
    info = realdb.info("a")
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id, inv_id = await _seed_run(realdb, initiated_by=info.users["ap_manager"])
    async with realdb.sessionmaker("a")() as s:
        inv = await s.get(Invoice, inv_id)
        inv.amount = Decimal("1400.00")  # re-priced since staging
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/payments/runs/{run_id}/nacha")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["params"]["reason"] == "net_amount_changed"


@pytest.mark.asyncio
async def test_a_weekend_effective_date_is_refused(realdb):
    info = realdb.info("a")
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id, _ = await _seed_run(realdb, initiated_by=info.users["ap_manager"])
    saturday = utc_today() + timedelta(days=(5 - utc_today().weekday()) % 7 or 7)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(
            f"/api/payments/runs/{run_id}/nacha", params={"effective_date": saturday.isoformat()}
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_the_offset_account_saves_reads_back_masked_and_survives_a_resave(realdb):
    """Decisions §432: stored in the settings, shown as a last-4, kept when a
    re-save sends it blank, and named — never valued — in the audit row."""
    info = realdb.info("a")
    offset = {
        "offset_routing": RDFI,
        "offset_account": "44455566677",
        "offset_account_type": "savings",
    }
    async with realdb.client(key="a", role="admin") as c:
        saved = await c.patch(
            "/api/organization",
            json={
                "settings": {"payments": {"mode": "record_only", "nacha": {**NACHA_CFG, **offset}}}
            },
        )
        assert saved.status_code == 200, saved.text
        assert "44455566677" not in saved.text
        assert saved.json()["settings"]["payments"]["nacha"]["offset_account"] == "****6677"

        shown = (await c.get("/api/organization")).json()["settings"]["payments"]
        assert shown["nacha"]["offset_account"] == "****6677"
        # The settings page re-saves what it was shown, with the account blank.
        resaved = await c.patch(
            "/api/organization",
            json={
                "settings": {
                    "payments": {**shown, "nacha": {**shown["nacha"], "offset_account": ""}}
                }
            },
        )
        assert resaved.status_code == 200, resaved.text

    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, info.org_id)
        assert org.settings["payments"]["nacha"]["offset_account"] == "44455566677"
    async with realdb.sessionmaker("a")() as s:
        audits = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "organization.provider_config_updated"
                    )
                )
            )
            .scalars()
            .all()
        )
    # The first save names the NACHA keys it set; the re-save changed nothing.
    [audit] = [a for a in audits if a.details.get("nacha_changed")]
    assert audit.details["nacha_changed"] == sorted({*NACHA_CFG, *offset})
    assert all("44455566677" not in str(a.details) for a in audits)


@pytest.mark.asyncio
async def test_a_half_filled_offset_is_refused_without_echoing_the_account(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={
                "settings": {
                    "payments": {
                        "nacha": {
                            **NACHA_CFG,
                            "offset_routing": "011000016",  # bad checksum
                            "offset_account": "44455566677",
                        }
                    }
                }
            },
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == (
        "payments.nacha has invalid fields: offset_routing, offset_account_type."
    )
    assert "44455566677" not in resp.text


@pytest.mark.asyncio
async def test_a_configured_offset_exports_a_balanced_file(realdb):
    info = realdb.info("a")
    await _settings(
        realdb,
        info.org_id,
        {
            "mode": "record_only",
            "nacha": {
                **NACHA_CFG,
                "offset_routing": ODFI,
                "offset_account": "44455566677",
                "offset_account_type": "checking",
            },
        },
    )
    run_id, _ = await _seed_run(realdb, initiated_by=info.users["ap_manager"])
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(
            f"/api/payments/runs/{run_id}/nacha",
            params={"effective_date": _business_day(2).isoformat()},
        )
    assert resp.status_code == 200, resp.text
    lines = resp.text.rstrip("\r\n").split("\r\n")
    assert lines[1][1:4] == "200"
    [credit, debit] = [line for line in lines if line.startswith("6")]
    assert credit[1:3] == "22" and debit[1:3] == "27"
    assert debit[12:29].rstrip() == "44455566677"
    assert debit[29:39] == credit[29:39] == "0000150025"
    batch_control = next(line for line in lines if line.startswith("8"))
    assert batch_control[20:32] == batch_control[32:44] == "000000150025"

    async with realdb.sessionmaker("a")() as s:
        [audit] = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == run_id,
                        AuditLog.action == "payment_run.nacha_exported",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert audit.details["balanced"] is True
    assert "44455566677" not in str(audit.details)
