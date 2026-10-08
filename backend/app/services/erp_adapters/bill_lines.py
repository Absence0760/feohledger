"""Choosing the GL lines a direct adapter posts for an invoice.

Shared by the adapters that post an AP bill as GL-coded lines (Sage Intacct,
SYSPRO). Not an adapter itself — it registers nothing.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.erp_adapters.base import ACCOUNT_NOT_LINKED, InvoicePayload


def bill_lines(payload: InvoicePayload) -> list[tuple[str, Decimal, str]] | str:
    """The ``(gl_account_id, amount, memo)`` lines to post, or a refusal reason.

    Per-line only when every line carries a total, every line resolves an ERP
    account id (its own, else the header's), and the totals sum to exactly
    ``payload.amount``. Otherwise one line for ``payload.amount`` against the
    header account. The header amount is never recomputed from lines — a bill
    whose total differs from the approved amount must not be posted.
    """
    header_gl = payload.gl_account_erp_id
    items = payload.line_items
    if items and all(li.total is not None for li in items):
        resolved = [(li.gl_account_erp_id or header_gl, li) for li in items]
        if all(gl for gl, _ in resolved) and sum(li.total for li in items) == payload.amount:
            return [
                (gl, li.total, li.description or payload.description or "") for gl, li in resolved
            ]
    if not header_gl:
        return ACCOUNT_NOT_LINKED
    return [(header_gl, payload.amount, payload.description or "")]
