"""A pre-approval with no currency covers nothing (decisions §473, amended).

`_approved_preapproval_amount` used to read a NULL pre-approval currency as
USD, so a currency-less authorization cleared a USD expense's blocking
`preapproval_required`. An unknown currency matches no expense now.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.api.expenses import _approved_preapproval_amount
from app.models.entity import Entity
from app.models.expense import (
    Expense,
    ExpensePreapproval,
    ExpenseReport,
    PreapprovalStatus,
)


@pytest.mark.asyncio
# `expense_preapprovals.currency` is NOT NULL (migration 0039, model default
# USD), so a NULL cannot be stored; the reachable "no currency" values are the
# blank ones, which the coalesce-to-USD read used to turn into USD as well.
@pytest.mark.parametrize("stored, covered", [("USD", True), ("", False), ("  ", False)])
async def test_a_currency_less_preapproval_covers_nothing(realdb, stored, covered):
    info = realdb.info("a")
    owner = info.users["ap_clerk"]
    async with realdb.sessionmaker("a")() as s:
        entity_id = (
            await s.execute(select(Entity.id).where(Entity.is_default.is_(True)))
        ).scalar_one()
        report = ExpenseReport(
            report_number=f"ER-PRE-{uuid.uuid4().hex[:8]}",
            employee_user_id=owner,
            currency="USD",
            organization_id=info.org_id,
            entity_id=entity_id,
        )
        s.add(report)
        await s.flush()
        expense = Expense(
            report_id=report.id,
            expense_date=date(2026, 6, 1),
            amount=Decimal("500.00"),
            currency="USD",
            category=f"cat-{uuid.uuid4().hex[:6]}",
            organization_id=info.org_id,
            entity_id=entity_id,
        )
        pre = ExpensePreapproval(
            requester_user_id=owner,
            title="Conference",
            estimated_amount=Decimal("5000.00"),
            currency="USD",
            status=PreapprovalStatus.approved,
            expense_report_id=report.id,
            organization_id=info.org_id,
            entity_id=entity_id,
        )
        s.add_all([expense, pre])
        await s.flush()
        # The column default would turn an explicit None back into USD at
        # INSERT, so the unknown value is written afterwards.
        await s.execute(
            update(ExpensePreapproval)
            .where(ExpensePreapproval.id == pre.id)
            .values(currency=stored)
        )
        await s.flush()

        amount = await _approved_preapproval_amount(s, expense)
        await s.rollback()

    assert (amount == Decimal("5000.00")) if covered else (amount is None)
